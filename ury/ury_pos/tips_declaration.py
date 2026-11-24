"""G38: Tips Declaration at Shift Close & Optional HR Payroll Bridge.

- Direct cash tips declared by servers on `POS Closing Entry.custom_tip_declarations`
  are for compliance, reporting, and payroll visibility ONLY: they NEVER alter expected
  cash drawer balances or Cash Over/Short variance.
- Reconciles per-server `charged_tips_collected` (from shift `POS Invoice.custom_charged_tip` /
  `custom_tip_server`) minus `tips_paid_from_till` (from shift Tip Payout `Journal Entry`)
  = `net_tips_payable`, respecting multi-currency `0/2/3dp` precision (UGX/USD/KWD).
- Optional HR Payroll Bridge (`create_tip_additional_salaries`): strictly restricted to
  `HR Manager` and `Accounts Manager` (never cashiers/waiters), creates 1 idempotent Draft
  native `Additional Salary` per server with `net_tips_payable > 0` and an active linked
  `Employee`, using `custom_business_date or posting_date` as `payroll_date`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, get_time

from ury.brand import coded_message
from ury.ury.hooks.ury_cash_movement import _drawer_accounts, line_amount_in_profile_currency
from ury.ury_pos import outlet_context

ZERO_DECIMAL_CURRENCIES = frozenset({
    "UGX", "JPY", "KRW", "RWF", "TZS", "XAF", "XOF", "BIF", "GNF", "MGA", "VND",
})
THREE_DECIMAL_CURRENCIES = frozenset({"KWD", "BHD", "OMR", "JOD", "TND", "IQD"})
ALLOWED_PAYROLL_BRIDGE_ROLES = frozenset({"HR Manager", "Accounts Manager"})


def currency_decimal_places(currency: str | None) -> int:
    code = (currency or "").strip().upper()
    if code in ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in THREE_DECIMAL_CURRENCIES:
        return 3
    return 2


def _is_hrms_payroll_available() -> bool:
    try:
        apps = frappe.get_installed_apps() or []
    except Exception:
        apps = []
    if not isinstance(apps, (list, tuple, set)) or "hrms" not in apps:
        return False
    try:
        return bool(frappe.db.exists("DocType", "Additional Salary"))
    except Exception:
        return False


def _user_full_name(user: str) -> str:
    if not user:
        return ""
    return frappe.db.get_value("User", user, "full_name") or user


def _employee_for_user(user: str, company: str) -> str | None:
    if not user or user in ("Administrator", "Guest"):
        return None
    emp = frappe.db.get_value(
        "Employee",
        {"user_id": user, "company": company, "status": "Active"},
        "name",
    )
    if emp:
        return emp
    return frappe.db.get_value("Employee", {"user_id": user, "company": company}, "name")


def _invoice_timestamp(row: dict[str, Any]) -> datetime | None:
    pdate = row.get("posting_date")
    if not pdate:
        return None
    try:
        d = get_datetime(pdate).date()
        ptime = row.get("posting_time")
        t = get_time(ptime) if ptime else datetime.min.time()
        return datetime.combine(d, t)
    except Exception:
        return None


def compute_shift_tip_rows(
    opening: dict[str, Any],
    profile,
    *,
    period_end_date=None,
) -> list[dict[str, Any]]:
    """Aggregate shift servers' charged tips collected and cash tip payouts in till currency."""
    currency = profile.get("currency") or frappe.db.get_value("Company", opening["company"], "default_currency") or ""
    precision = currency_decimal_places(currency)
    company_currency = frappe.get_cached_value("Company", opening["company"], "default_currency") or currency

    start_dt = get_datetime(opening.get("period_start_date")) if opening.get("period_start_date") else None
    end_dt = get_datetime(period_end_date) if period_end_date else None

    inv_filters: dict[str, Any] = {
        "docstatus": 1,
        "pos_profile": opening["pos_profile"],
    }
    if opening.get("status") == "Open":
        inv_filters["consolidated_invoice"] = ["is", "not set"]

    invoices = frappe.get_all(
        "POS Invoice",
        filters=inv_filters,
        fields=[
            "name",
            "posting_date",
            "posting_time",
            "waiter",
            "owner",
            "custom_tip_server",
            "custom_charged_tip",
            "consolidated_invoice",
        ],
        limit_page_length=0,
    )
    if not isinstance(invoices, (list, tuple)):
        invoices = []

    charged_by_user: dict[str, float] = {}
    paid_by_user: dict[str, float] = {}
    servers: set[str] = set()

    for inv in invoices:
        ts = _invoice_timestamp(inv)
        if ts is not None:
            if start_dt is not None and ts < start_dt:
                continue
            if end_dt is not None and ts > end_dt:
                continue

        waiter = (inv.get("waiter") or "").strip()
        owner = (inv.get("owner") or "").strip()
        tip_server = (inv.get("custom_tip_server") or "").strip()
        charged_tip = flt(inv.get("custom_charged_tip"), precision)

        server = waiter or owner
        if server and server not in ("Administrator", "Guest"):
            servers.add(server)
        if charged_tip != 0:
            attributed = tip_server or server
            if attributed and attributed not in ("Administrator", "Guest"):
                servers.add(attributed)
                charged_by_user[attributed] = flt(charged_by_user.get(attributed, 0.0) + charged_tip, precision)

    payouts = frappe.get_all(
        "Journal Entry",
        filters={
            "custom_pos_opening_entry": opening["name"],
            "docstatus": 1,
            "custom_cash_movement_type": "Paid Out",
            "custom_tip_server": ["is", "set"],
        },
        fields=["name", "posting_date", "custom_tip_server", "total_debit"],
        limit_page_length=0,
    )
    if not isinstance(payouts, (list, tuple)):
        payouts = []

    drawers = _drawer_accounts(profile, opening["company"])
    for je in payouts:
        tip_server = (je.get("custom_tip_server") or "").strip()
        if not tip_server or tip_server in ("Administrator", "Guest"):
            continue
        servers.add(tip_server)
        acct_rows = frappe.get_all(
            "Journal Entry Account",
            filters={"parent": je.get("name"), "parenttype": "Journal Entry"},
            fields=[
                "account",
                "account_currency",
                "debit_in_account_currency",
                "credit_in_account_currency",
                "debit",
                "credit",
            ],
            limit_page_length=0,
        )
        drawer_line = next(
            (r for r in (acct_rows or []) if r.get("account") in drawers),
            None,
        )
        if drawer_line is not None:
            paid_amt = flt(
                line_amount_in_profile_currency(
                    drawer_line, True, currency, company_currency, je.get("posting_date")
                ),
                precision,
            )
        else:
            paid_amt = flt(je.get("total_debit"), precision)
        paid_by_user[tip_server] = flt(paid_by_user.get(tip_server, 0.0) + paid_amt, precision)

    rows = []
    for user in sorted(servers):
        charged = flt(charged_by_user.get(user, 0.0), precision)
        paid_out = flt(paid_by_user.get(user, 0.0), precision)
        net = flt(charged - paid_out, precision)
        rows.append(
            {
                "user": user,
                "employee": _employee_for_user(user, opening["company"]),
                "full_name": _user_full_name(user),
                "currency": currency,
                "charged_tips_collected": charged,
                "tips_paid_from_till": paid_out,
                "net_tips_payable": net,
                "declared_cash_tips": 0 if precision == 0 else 0.0,
                "remarks": "",
            }
        )

    rows.sort(key=lambda item: (item["full_name"].lower(), item["user"].lower()))
    return rows


@frappe.whitelist(methods=["GET"])
def get_shift_tip_declaration_rows(pos_opening_entry: str) -> dict[str, Any]:
    """Return server tip reconciliation rows for the shift closing dialog."""
    opening = frappe.db.get_value(
        "POS Opening Entry",
        pos_opening_entry,
        ["name", "pos_profile", "company", "branch", "user", "period_start_date", "status", "docstatus"],
        as_dict=True,
    )
    if not opening:
        frappe.throw(
            coded_message("TIPS-SHIFT-NOT-FOUND", _("POS shift not found.")),
            frappe.ValidationError,
        )

    outlet_context.assert_outlet_access(opening["pos_profile"], user=frappe.session.user)
    profile = frappe.get_doc("POS Profile", opening["pos_profile"])
    currency = profile.get("currency") or frappe.db.get_value("Company", opening["company"], "default_currency") or ""
    precision = currency_decimal_places(currency)
    rows = compute_shift_tip_rows(opening, profile)

    return {
        "pos_opening_entry": opening["name"],
        "pos_profile": profile.name,
        "company": opening["company"],
        "currency": currency,
        "currency_precision": precision,
        "require_tip_declaration": bool(cint(profile.get("custom_require_tip_declaration")) == 1),
        "rows": rows,
    }


def validate_tip_declarations(doc, method=None) -> None:
    """Validate `POS Closing Entry.custom_tip_declarations` before submit.

    - Recomputes authoritative server-side charged/paid/net tips (never trusts client values).
    - Refuses negative declared cash tips or duplicate server rows.
    - Enforces opt-in `POS Profile.custom_require_tip_declaration` (`[POS-CLOSE-TIP-DECLARATION-REQUIRED]`).
    - Never touches `payment_reconciliation` expected_amount or difference!
    """
    pos_profile = doc.get("pos_profile")
    pos_opening_entry = doc.get("pos_opening_entry")
    if not pos_profile or not pos_opening_entry:
        return

    rows = list(doc.get("custom_tip_declarations") or [])
    try:
        profile = frappe.get_doc("POS Profile", pos_profile)
    except Exception as exc:
        if not rows and exc.__class__.__name__ == "DoesNotExistError":
            return
        raise
    require_declaration = cint(profile.get("custom_require_tip_declaration")) == 1

    if not rows and not require_declaration:
        return

    opening = frappe.db.get_value(
        "POS Opening Entry",
        pos_opening_entry,
        ["name", "pos_profile", "company", "branch", "user", "period_start_date", "status", "docstatus"],
        as_dict=True,
    )
    if not opening:
        return

    currency = profile.get("currency") or frappe.db.get_value("Company", opening["company"], "default_currency") or ""
    precision = currency_decimal_places(currency)
    expected_rows = compute_shift_tip_rows(
        opening,
        profile,
        period_end_date=doc.get("period_end_date"),
    )
    expected_by_user = {r["user"]: r for r in expected_rows}

    seen_users: set[str] = set()
    for row in rows:
        user = (row.get("user") or "").strip()
        if not user:
            frappe.throw(
                coded_message("CLOSE-TIP-DECLARATION-REQUIRED", _("Every tip declaration row must specify a server.")),
                frappe.ValidationError,
            )
        if user in seen_users:
            frappe.throw(
                coded_message(
                    "CLOSE-TIP-DUPLICATE-SERVER",
                    _("Duplicate tip declaration row for server {0}.").format(user),
                ),
                frappe.ValidationError,
            )
        seen_users.add(user)

        raw_declared = row.get("declared_cash_tips")
        if raw_declared is None or raw_declared == "":
            if require_declaration:
                frappe.throw(
                    coded_message(
                        "CLOSE-TIP-DECLARATION-REQUIRED",
                        _("Enter declared cash tips (use 0 if none) for {0} before closing.").format(user),
                    ),
                    frappe.ValidationError,
                )
            declared_val = flt(0, precision)
        else:
            declared_val = flt(raw_declared, precision)

        if declared_val < 0:
            frappe.throw(
                coded_message("CLOSE-TIP-NEGATIVE", _("Declared cash tips cannot be negative.")),
                frappe.ValidationError,
            )

        authoritative = expected_by_user.get(user)
        row.currency = currency
        row.employee = (authoritative or {}).get("employee") or _employee_for_user(user, opening["company"])
        row.charged_tips_collected = flt((authoritative or {}).get("charged_tips_collected", 0.0), precision)
        row.tips_paid_from_till = flt((authoritative or {}).get("tips_paid_from_till", 0.0), precision)
        row.net_tips_payable = flt((authoritative or {}).get("net_tips_payable", 0.0), precision)
        row.declared_cash_tips = declared_val

    if require_declaration:
        missing = [u for u in expected_by_user if u not in seen_users]
        if missing:
            frappe.throw(
                coded_message(
                    "CLOSE-TIP-DECLARATION-REQUIRED",
                    _("Record cash tip declarations for all shift servers before closing: {0}.").format(
                        ", ".join(missing)
                    ),
                ),
                frappe.ValidationError,
            )


def _is_deadlock_or_lock_error(exc: Exception) -> bool:
    cls_name = exc.__class__.__name__
    if cls_name in ("QueryDeadlockError", "QueryTimeoutError", "TimestampMismatchError"):
        return True
    qde = getattr(frappe, "QueryDeadlockError", None)
    if isinstance(qde, type) and issubclass(qde, BaseException) and isinstance(exc, qde):
        return True
    msg = str(exc).lower()
    return "deadlock" in msg or "lock wait timeout" in msg


def _lock_closing_entry_for_payroll(pos_closing_entry: str) -> None:
    """Acquire an InnoDB row lock on POS Closing Entry so concurrent bridge calls serialize cleanly."""
    try:
        frappe.db.get_value(
            "POS Closing Entry",
            pos_closing_entry,
            ["name", "docstatus"],
            as_dict=True,
            for_update=True,
        )
    except TypeError:
        try:
            frappe.db.sql(
                "SELECT name FROM `tabPOS Closing Entry` WHERE name = %s FOR UPDATE",
                (pos_closing_entry,),
            )
        except Exception:
            pass


def _locked_committed_refs_and_salaries(pos_closing_entry: str) -> tuple[dict[str, str], dict[str, str]]:
    """Read latest committed tip-declaration refs & Additional Salary rows via FOR UPDATE.

    Under MariaDB REPEATABLE READ, a plain SELECT after waiting on a row lock would still see
    the pre-lock MVCC snapshot; locking reads bypass the stale snapshot and observe rows
    committed by the winning concurrent worker.
    """
    row_refs: dict[str, str] = {}
    emp_salaries: dict[str, str] = {}
    try:
        td_rows = frappe.db.sql(
            """
            SELECT name, custom_additional_salary_ref
            FROM `tabPOS Tip Declaration`
            WHERE parent = %s
            FOR UPDATE
            """,
            (pos_closing_entry,),
            as_dict=True,
        )
        if isinstance(td_rows, (list, tuple)):
            for r in td_rows:
                if isinstance(r, dict) and r.get("name") and (r.get("custom_additional_salary_ref") or "").strip():
                    row_refs[str(r["name"])] = str(r["custom_additional_salary_ref"]).strip()
    except Exception:
        pass
    try:
        sal_rows = frappe.db.sql(
            """
            SELECT name, employee
            FROM `tabAdditional Salary`
            WHERE ref_doctype = 'POS Closing Entry'
              AND ref_docname = %s
              AND docstatus < 2
            FOR UPDATE
            """,
            (pos_closing_entry,),
            as_dict=True,
        )
        if isinstance(sal_rows, (list, tuple)):
            for r in sal_rows:
                if isinstance(r, dict) and r.get("name") and r.get("employee"):
                    emp_salaries[str(r["employee"])] = str(r["name"])
    except Exception:
        pass
    return row_refs, emp_salaries


@frappe.whitelist(methods=["POST"])
def create_tip_additional_salaries(pos_closing_entry: str, salary_component: str | None = None) -> dict[str, Any]:
    """Create 1 idempotent Draft `Additional Salary` per server with `net_tips_payable > 0`.

    Restricted strictly to `HR Manager` or `Accounts Manager` (never cashiers/waiters) and
    fail-closed on `frappe.has_permission("Additional Salary", "create")`.
    """
    user = getattr(getattr(frappe, "session", None), "user", None)
    user_roles = set(frappe.get_roles(user) or [])
    if not (ALLOWED_PAYROLL_BRIDGE_ROLES & user_roles):
        frappe.throw(
            coded_message(
                "TIPS-PAYROLL-FORBIDDEN",
                _("Only HR Manager or Accounts Manager may create payroll tip entries."),
            ),
            frappe.PermissionError,
        )

    if not _is_hrms_payroll_available():
        frappe.throw(
            coded_message("TIPS-HRMS-UNAVAILABLE", _("HR payroll is not installed on this site.")),
            frappe.ValidationError,
        )

    if not frappe.has_permission("Additional Salary", "create"):
        frappe.throw(
            coded_message(
                "TIPS-PAYROLL-FORBIDDEN",
                _("You do not have permission to create Additional Salary records."),
            ),
            frappe.PermissionError,
        )

    # Serialize concurrent payroll-bridge clicks upfront on POS Closing Entry
    try:
        _lock_closing_entry_for_payroll(pos_closing_entry)
    except Exception as exc:
        if _is_deadlock_or_lock_error(exc):
            frappe.throw(
                coded_message(
                    "TIPS-PAYROLL-ALREADY-CREATED",
                    _("Payroll tip entries have already been created for this shift close."),
                ),
                frappe.ValidationError,
            )
        raise

    try:
        closing = frappe.get_doc("POS Closing Entry", pos_closing_entry, for_update=True)
    except TypeError:
        closing = frappe.get_doc("POS Closing Entry", pos_closing_entry)

    if not frappe.has_permission("POS Closing Entry", "read", doc=closing):
        frappe.throw(
            coded_message("TIPS-PAYROLL-FORBIDDEN", _("Not permitted to read this shift close.")),
            frappe.PermissionError,
        )
    if cint(closing.get("docstatus")) != 1:
        frappe.throw(
            coded_message(
                "TIPS-CLOSING-NOT-SUBMITTED",
                _("Submit the shift closing entry before creating payroll tip entries."),
            ),
            frappe.ValidationError,
        )

    profile = frappe.get_doc("POS Profile", closing.pos_profile) if closing.get("pos_profile") else None
    component = (
        (salary_component or "").strip()
        or (profile.get("custom_tips_salary_component") or "").strip()
        if profile
        else (salary_component or "").strip()
    )
    if not component or not frappe.db.exists("Salary Component", component):
        frappe.throw(
            coded_message(
                "TIPS-SALARY-COMPONENT-REQUIRED",
                _("Configure a valid Tips Payroll Component before creating payroll tip entries."),
            ),
            frappe.ValidationError,
        )

    payroll_date = closing.get("custom_business_date") or closing.get("posting_date")
    default_currency = (
        (profile.get("currency") if profile else None)
        or frappe.db.get_value("Company", closing.company, "default_currency")
        or ""
    )

    committed_row_refs, committed_emp_salaries = _locked_committed_refs_and_salaries(closing.name)

    rows = list(closing.get("custom_tip_declarations") or [])
    created: list[dict[str, Any]] = []
    already_created: list[str] = []
    skipped_no_employee: list[str] = []
    eligible_count = 0

    for row in rows:
        row_currency = row.get("currency") or default_currency
        precision = currency_decimal_places(row_currency)
        net_payable = flt(row.get("net_tips_payable"), precision)
        if net_payable <= 0:
            continue

        employee = (row.get("employee") or "").strip()
        if not employee:
            skipped_no_employee.append(row.get("user") or "")
            continue

        eligible_count += 1

        existing_ref = committed_row_refs.get(str(row.get("name") or "")) or (
            row.get("custom_additional_salary_ref") or ""
        ).strip()
        if existing_ref:
            already_created.append(existing_ref)
            continue

        existing_in_db = committed_emp_salaries.get(employee) or frappe.db.exists(
            "Additional Salary",
            {
                "ref_doctype": "POS Closing Entry",
                "ref_docname": closing.name,
                "employee": employee,
                "docstatus": ["<", 2],
            },
        )
        if existing_in_db:
            frappe.db.set_value(
                "POS Tip Declaration",
                row.name,
                "custom_additional_salary_ref",
                existing_in_db,
                update_modified=False,
            )
            already_created.append(str(existing_in_db))
            continue

        add_sal = frappe.get_doc(
            {
                "doctype": "Additional Salary",
                "employee": employee,
                "company": closing.company,
                "salary_component": component,
                "amount": net_payable,
                "payroll_date": payroll_date,
                "currency": row_currency,
                "ref_doctype": "POS Closing Entry",
                "ref_docname": closing.name,
                "overwrite_salary_structure_amount": 0,
            }
        )
        try:
            add_sal.insert(ignore_permissions=False)
            frappe.db.set_value(
                "POS Tip Declaration",
                row.name,
                "custom_additional_salary_ref",
                add_sal.name,
                update_modified=False,
            )
        except Exception as exc:
            if _is_deadlock_or_lock_error(exc):
                frappe.throw(
                    coded_message(
                        "TIPS-PAYROLL-ALREADY-CREATED",
                        _("Payroll tip entries have already been created for this shift close."),
                    ),
                    frappe.ValidationError,
                )
            raise
        created.append(
            {
                "name": add_sal.name,
                "employee": employee,
                "user": row.get("user"),
                "amount": net_payable,
                "currency": row_currency,
                "payroll_date": str(payroll_date),
            }
        )

    if eligible_count > 0 and not created and already_created:
        frappe.throw(
            coded_message(
                "TIPS-PAYROLL-ALREADY-CREATED",
                _("Payroll tip entries have already been created for this shift close."),
            ),
            frappe.ValidationError,
        )

    return {
        "pos_closing_entry": closing.name,
        "created": created,
        "already_created": already_created,
        "skipped_no_employee": skipped_no_employee,
    }
