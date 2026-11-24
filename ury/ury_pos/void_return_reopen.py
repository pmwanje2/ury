"""POS void, waste, return/refund, and reopen/adjust engine (B5a — Simphony/SAP parity).

Matrix rows 5-10, tracker G8 & G34:
- Sent-item void / qty reduction / whole-check cancel with mandatory POS Reason,
  manager PIN override (via B2a `ury.ury_pos.override`), cancel-KOT reason link,
  and native ERPNext Stock Entry (Material Issue / Wastage) for waste reasons.
- Unsent (cart / held) items stay free (Simphony Role Opt 135 Error Correct).
- Closed checks cannot be cancelled directly; voiding a closed check goes through
  a native POS return (or same-day non-fiscal / credit-and-rebill reopen/adjust).
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from typing import Any

import frappe as _default_frappe
from frappe.utils import cint, flt

DEFAULT_MANAGER_ROLES = frozenset({"URY Manager", "URY Admin", "Accounts Manager"})

# Neutral refusal codes (no vendor/brand names).
CODE_REASON_REQUIRED = "POS-REASON-REQUIRED"
CODE_REASON_NOTE_REQUIRED = "POS-REASON-NOTE-REQUIRED"
CODE_REASON_INVALID = "POS-REASON-INVALID"
CODE_AUTH_REQUIRED = "POS-AUTH-REQUIRED"
CODE_OUTLET_MISMATCH = "POS-OUTLET-MISMATCH"
CODE_VOID_CLOSED_USE_RETURN = "POS-VOID-CLOSED-USE-RETURN"
CODE_VOID_FISCALISED_USE_RETURN = "POS-VOID-FISCALISED-USE-RETURN"
CODE_VOID_PARTIAL_PAYMENT = "POS-VOID-PARTIAL-PAYMENT"
CODE_VOID_OFFLINE_REFUSED = "POS-VOID-OFFLINE-REFUSED"


def _f():
    """Return the active `frappe` module (stays isolated across site-less test suites)."""
    return sys.modules.get("frappe") or _default_frappe


def _tr(message: str) -> str:
    fn = getattr(_f(), "_", None)
    return fn(message) if callable(fn) else message


def _now_dt():
    utils = sys.modules.get("frappe.utils")
    fn = getattr(utils, "now_datetime", None) if utils else None
    if callable(fn):
        res = fn()
        if res is not None:
            return res
    return _dt.datetime.now()


def _today_date() -> str:
    utils = sys.modules.get("frappe.utils")
    fn = getattr(utils, "nowdate", None) if utils else None
    if callable(fn):
        res = fn()
        if isinstance(res, str) and res:
            return res
    return _now_dt().strftime("%Y-%m-%d")


def _now_time() -> str:
    utils = sys.modules.get("frappe.utils")
    fn = getattr(utils, "nowtime", None) if utils else None
    if callable(fn):
        res = fn()
        if isinstance(res, str) and res:
            return res
    return _now_dt().strftime("%H:%M:%S")


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field without triggering `.get()` method calls on MagicMock doubles."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _refuse(code: str, message: str, exc=None) -> None:
    fr = _f()
    fr.throw(f"{_tr(message)} [{code}]", exc or fr.ValidationError)


def _override_module():
    """Return `ury.ury_pos.override` when present (B2a or test stub), else None."""
    mod = sys.modules.get("ury.ury_pos.override")
    if mod is not None:
        return mod
    try:
        from ury.ury_pos import override as ov
        return ov
    except ImportError:
        return None


def _has_field(doctype: str, fieldname: str) -> bool:
    try:
        meta = _f().get_meta(doctype)
        return bool(meta and meta.has_field(fieldname) is True)
    except Exception:
        return False


def _user_roles(user: str | None = None) -> set[str]:
    fr = _f()
    try:
        raw = fr.get_roles(user) if user else fr.get_roles()
    except TypeError:
        raw = fr.get_roles()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return {str(r) for r in raw if isinstance(r, str)}
    return set()


def _safe_profile_name(pos_profile: Any) -> str | None:
    if isinstance(pos_profile, str) and pos_profile.strip():
        return pos_profile.strip()
    return None


def is_fiscalised_invoice(doc) -> bool:
    """True when the invoice has been fiscalised (EFRIS FDN / IRN or active E Invoice)."""
    if not doc:
        return False
    irn = _field(doc, "efris_irn")
    if isinstance(irn, str) and irn.strip():
        return True
    status = _field(doc, "efris_status")
    if isinstance(status, str) and status.strip() not in ("", "Cancelled"):
        return True
    name = _field(doc, "name")
    fr = _f()
    if isinstance(name, str) and name and fr.db.exists("DocType", "E Invoice") is True:
        ref = fr.db.get_value(
            "E Invoice",
            {"invoice": name},
            ["irn", "credit_note_application_ref_no"],
            as_dict=True,
        )
        if ref and (_field(ref, "irn") or _field(ref, "credit_note_application_ref_no")):
            return True
    return False


def received_tender_total(invoice) -> float:
    """Sum of confirmed server-marked received tenders on an open check."""
    payments = _field(invoice, "payments")
    if not isinstance(payments, (list, tuple)):
        return 0.0
    total = 0.0
    for row in payments:
        if cint(_field(row, "custom_tender_received", 0)) == 1:
            amt = flt(_field(row, "amount", 0))
            if amt > 0:
                total += amt
    return total


def has_sent_kot_for_invoice(invoice_name: str | None) -> bool:
    """True if an original kitchen/bar ticket (New Order / Order Modified) exists for this check."""
    if not isinstance(invoice_name, str) or not invoice_name:
        return False
    found = _f().db.exists(
        "URY KOT",
        {
            "invoice": invoice_name,
            "type": ["in", ["New Order", "Order Modified"]],
            "docstatus": ["in", [1, 2]],
        },
    )
    return isinstance(found, str) and bool(found)


def has_sent_kitchen_activity(invoice) -> bool:
    """True when an open check has already been sent to kitchen/bar or printed."""
    name = _field(invoice, "name")
    if has_sent_kot_for_invoice(name):
        return True
    printed = _field(invoice, "invoice_printed", 0)
    if isinstance(printed, (int, float, bool)) and cint(printed) == 1:
        return True
    items = _field(invoice, "items")
    if isinstance(items, (list, tuple)):
        positive = [r for r in items if flt(_field(r, "qty", 0)) > 0]
        if positive and any(cint(_field(r, "ury_held", 0)) == 0 for r in positive):
            return True
    return False


def _is_waste_reason(code: str, description: str, is_waste: Any = 0) -> bool:
    if cint(is_waste) == 1:
        return True
    code_up = (code or "").strip().upper()
    if code_up.startswith("WASTE"):
        return True
    desc_low = (description or "").strip().lower()
    return "(waste)" in desc_low or desc_low.startswith("waste")


def _map_wastage_reason(code: str, description: str, note: str = "") -> str:
    """Map POS waste reason to ERPNext/restaurant-controls custom_wastage_reason options."""
    haystack = f"{code} {description} {note}".lower()
    if "broken" in haystack or "break" in haystack or ("drop" in haystack and "spoil" not in haystack):
        return "Broken"
    if "spoil" in haystack or "burn" in haystack or "contaminated" in haystack or "drop" in haystack:
        return "Spoiled"
    if "expir" in haystack:
        return "Expired"
    if "theft" in haystack or "stolen" in haystack:
        return "Theft"
    return "Other"


def _active_pos_reasons(privilege: str) -> list[dict[str, Any]]:
    ov = _override_module()
    if ov is not None and hasattr(ov, "list_reasons"):
        try:
            rows = ov.list_reasons(privilege)
            if isinstance(rows, list):
                valid = [r for r in rows if isinstance(_field(r, "code"), str) and _field(r, "code").strip()]
                if valid:
                    return valid
        except Exception:
            pass
    fr = _f()
    try:
        if fr.db.table_exists("POS Reason") is not True:
            return []
        raw = fr.get_all(
            "POS Reason",
            filters={"disabled": 0},
            fields=["code", "description", "applies_to", "requires_note", "sort_order"],
            order_by="sort_order asc, code asc",
        )
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for row in raw:
        code = _field(row, "code")
        if not isinstance(code, str) or not code.strip():
            continue
        targets = [
            p.strip()
            for p in str(_field(row, "applies_to") or "").replace(",", "\n").splitlines()
            if p.strip()
        ]
        if privilege and targets and privilege not in targets:
            continue
        out.append({
            "code": code,
            "description": _field(row, "description"),
            "requires_note": cint(_field(row, "requires_note", 0)),
            "applies_to": targets,
        })
    return out


def resolve_pos_reason(
    privilege: str,
    pos_profile: str | None,
    reason: Any,
    note: Any = None,
    is_waste: Any = 0,
    *,
    fallback_validator=None,
    invoice_for_fallback=None,
) -> dict[str, Any]:
    """Validate and normalise a POS Reason (with graceful legacy fallback when no POS Reason rows exist)."""
    fr = _f()
    reasons = _active_pos_reasons(privilege)
    clean_note = note.strip() if isinstance(note, str) else ""

    if reasons:
        if not isinstance(reason, str) or not reason.strip():
            _refuse(CODE_REASON_REQUIRED, "Choose a reason before continuing.", fr.ValidationError)
        candidate = reason.strip()

        # Support legacy "Other: <note>" syntax cleanly when OTHER reason exists.
        if candidate.startswith("Other: ") and not clean_note:
            other_row = next((r for r in reasons if str(r.get("code") or "").upper() == "OTHER"), None)
            if other_row is not None:
                clean_note = candidate[len("Other: "):].strip()
                candidate = other_row["code"]

        matched = None
        for row in reasons:
            if candidate == row.get("code") or candidate == row.get("description"):
                matched = row
                break

        if matched is None:
            _refuse(CODE_REASON_INVALID, "Choose an active configured reason.", fr.ValidationError)

        if cint(matched.get("requires_note")) == 1 and len(clean_note) < 4:
            _refuse(
                CODE_REASON_NOTE_REQUIRED,
                "Enter a note of at least 4 characters for this reason.",
                fr.ValidationError,
            )

        code = str(matched["code"])
        description = str(matched.get("description") or code)
        waste_flag = _is_waste_reason(code, description, is_waste=is_waste)
        text = f"{description}: {clean_note}" if clean_note else description
        display = f"[WASTE] {text}" if waste_flag else text
        stored = f"Other: {clean_note}" if code.upper() == "OTHER" and clean_note else text
        return {
            "code": code,
            "description": description,
            "note": clean_note,
            "display": display,
            "stored": stored,
            "is_waste": waste_flag,
        }

    # No POS Reason rows configured on this site yet: keep existing POS Profile / 4-char rule usable.
    if fallback_validator is not None and invoice_for_fallback is not None:
        stored = fallback_validator(invoice_for_fallback, reason)
    else:
        if not isinstance(reason, str) or len(reason.strip()) < 4:
            _refuse(CODE_REASON_REQUIRED, "Enter a reason (at least 4 characters).", fr.ValidationError)
        stored = reason.strip()
        if clean_note:
            stored = f"{stored}: {clean_note}"

    waste_flag = _is_waste_reason(stored, stored, is_waste=is_waste)
    display = f"[WASTE] {stored}" if waste_flag else stored
    return {
        "code": stored,
        "description": stored,
        "note": clean_note,
        "display": display,
        "stored": stored,
        "is_waste": waste_flag,
    }


def write_authorisation_log(
    *,
    requester: str | None,
    approver: str | None,
    privilege: str,
    pos_profile: str | None,
    company: str | None,
    reference_doctype: str,
    reference_name: str,
    claim: dict[str, Any],
    reason: str = "",
    outcome: str = "Consumed",
    refusal_code: str = "",
) -> None:
    """Record a server-managed POS Authorisation Log entry (insert-only, never leaks PINs)."""
    fr = _f()
    try:
        if fr.db.table_exists("POS Authorisation Log") is not True:
            return
    except Exception:
        return
    doc = fr.get_doc(
        {
            "doctype": "POS Authorisation Log",
            "requester": requester,
            "approver": approver,
            "privilege": privilege,
            "pos_profile": pos_profile,
            "company": company,
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "claim_values": json.dumps(claim or {}, sort_keys=True, separators=(",", ":"), default=str),
            "reason": str(reason or "").strip()[:500],
            "outcome": outcome,
            "refusal_code": refusal_code,
            "event_time": _now_dt(),
        }
    )
    doc.flags.pos_override_server_insert = True
    doc.insert(ignore_permissions=True)


def require_pos_override(
    privilege: str,
    pos_profile: str | None,
    reference_doctype: str,
    reference_name: str,
    claim: dict[str, Any],
    *,
    approval_token: str | None = None,
    percent: float | None = None,
    amount: float | None = None,
    privilege_group: int = 0,
    forbid_solo: bool = False,
) -> str:
    """Enforce Simphony/SAP privilege or single-use manager PIN token and always log use."""
    fr = _f()
    ov = _override_module()
    profile_name = _safe_profile_name(pos_profile)
    company = (
        fr.db.get_value("POS Profile", profile_name, "company")
        if profile_name
        else None
    )
    if not isinstance(company, str):
        company = None
    user = getattr(getattr(fr, "session", None), "user", None) or ""

    if approval_token:
        if ov is None:
            _refuse(CODE_AUTH_REQUIRED, "Manager approval service is unavailable.", fr.PermissionError)
        consumed = ov.consume(approval_token, privilege, profile_name, reference_name, claim)
        return str(consumed.approver)

    if forbid_solo:
        _refuse(CODE_AUTH_REQUIRED, "A different manager must approve this action.", fr.PermissionError)

    policy_active = bool(ov is not None and company and ov.is_policy_active(company))
    if policy_active:
        ov.require(
            privilege,
            profile_name,
            reference_name,
            claim,
            token=None,
            percent=percent,
            amount=amount,
            privilege_group=privilege_group,
        )
        write_authorisation_log(
            requester=user,
            approver=user,
            privilege=privilege,
            pos_profile=profile_name,
            company=company,
            reference_doctype=reference_doctype,
            reference_name=reference_name,
            claim=claim,
            reason=str(claim.get("reason") or ""),
            outcome="Consumed",
        )
        return user

    # Fallback when no POS Privilege Rule rows exist for this company yet:
    # today's manager roles may act alone; others require a manager PIN.
    roles = _user_roles()
    if roles & DEFAULT_MANAGER_ROLES:
        write_authorisation_log(
            requester=user,
            approver=user,
            privilege=privilege,
            pos_profile=profile_name,
            company=company,
            reference_doctype=reference_doctype,
            reference_name=reference_name,
            claim=claim,
            reason=str(claim.get("reason") or ""),
            outcome="Consumed",
        )
        return user

    _refuse(CODE_AUTH_REQUIRED, "A manager must approve this action.", fr.PermissionError)


def _expand_stock_waste_items(waste_items: list[dict[str, Any]], default_warehouse: str | None) -> list[dict[str, Any]]:
    """Expand Product Bundles and filter to stock items (`is_stock_item == 1`) only."""
    fr = _f()
    expanded: list[dict[str, Any]] = []
    for entry in waste_items:
        item_code = entry.get("item_code")
        qty = flt(entry.get("qty"))
        if not item_code or qty <= 0:
            continue
        warehouse = entry.get("warehouse") or default_warehouse

        bundle_name = fr.db.exists("Product Bundle", {"new_item_code": item_code})
        if isinstance(bundle_name, str) and bundle_name:
            components = fr.get_all(
                "Product Bundle Item",
                filters={"parent": bundle_name, "parenttype": "Product Bundle"},
                fields=["item_code", "qty", "uom"],
                order_by="idx",
            ) or []
            for comp in components:
                comp_code = _field(comp, "item_code")
                comp_info = fr.db.get_value(
                    "Item", comp_code, ["is_stock_item", "stock_uom"], as_dict=True
                )
                if comp_info is not None and cint(_field(comp_info, "is_stock_item", 0)) == 1:
                    expanded.append({
                        "item_code": comp_code,
                        "qty": qty * flt(_field(comp, "qty") or 1),
                        "uom": _field(comp, "uom") or _field(comp_info, "stock_uom"),
                        "warehouse": warehouse,
                    })
            continue

        item_info = fr.db.get_value("Item", item_code, ["is_stock_item", "stock_uom"], as_dict=True)
        if item_info is not None and cint(_field(item_info, "is_stock_item", 0)) == 1:
            expanded.append({
                "item_code": item_code,
                "qty": qty,
                "uom": entry.get("uom") or _field(item_info, "stock_uom"),
                "warehouse": warehouse,
            })
    return expanded


def _resolve_waste_expense_account(company: str | None) -> str | None:
    if not company:
        return None
    fr = _f()
    accounts = fr.get_all(
        "Account",
        filters={
            "account_name": "Wastage and Spoilage",
            "company": company,
            "is_group": 0,
            "root_type": "Expense",
        },
        pluck="name",
        limit_page_length=2,
    )
    if isinstance(accounts, list) and len(accounts) == 1:
        return accounts[0]
    return fr.db.get_value("Company", company, "stock_adjustment_account")


def _resolve_waste_cost_center(pos_profile, company: str | None, branch: str | None) -> str | None:
    fr = _f()
    cc = _field(pos_profile, "cost_center") if pos_profile else None
    if cc:
        return cc
    if branch:
        cc = fr.db.get_value("Cost Center", {"branch": branch, "is_group": 0}, "name")
        if cc:
            return cc
    if company:
        return fr.db.get_value("Company", company, "cost_center")
    return None


def post_waste_stock_entry(
    pos_invoice,
    pos_profile,
    waste_items: list[dict[str, Any]],
    reason_info: dict[str, Any],
    *,
    cancel_kot_names: list[str] | None = None,
    approver: str | None = None,
) -> str | None:
    """Post a native ERPNext Stock Entry (Material Issue / Wastage) for wasted sent items.

    Skips non-stock items cleanly. Links the Stock Entry onto created cancel KOTs and
    onto the invoice when the link fields exist.
    """
    fr = _f()
    default_wh = _field(pos_profile, "warehouse") if pos_profile else None
    stock_lines = _expand_stock_waste_items(waste_items, default_wh)
    if not stock_lines:
        return None

    company = _field(pos_invoice, "company") or (_field(pos_profile, "company") if pos_profile else None)
    branch = _field(pos_invoice, "branch") or (_field(pos_profile, "branch") if pos_profile else None)

    use_wastage_type = (
        bool(fr.db.exists("Stock Entry Type", "Wastage"))
        and fr.db.get_value("Stock Entry Type", "Wastage", "purpose") == "Material Issue"
    )
    entry_type = "Wastage" if use_wastage_type else "Material Issue"
    expense_account = _resolve_waste_expense_account(company)
    cost_center = _resolve_waste_cost_center(pos_profile, company, branch)

    se = fr.new_doc("Stock Entry")
    se.stock_entry_type = entry_type
    se.purpose = "Material Issue"
    se.company = company
    if branch:
        se.branch = branch
    if default_wh:
        se.from_warehouse = default_wh
    se.posting_date = _today_date()
    se.posting_time = _now_time()

    if _has_field("Stock Entry", "custom_wastage_reason"):
        se.custom_wastage_reason = _map_wastage_reason(
            reason_info.get("code", ""),
            reason_info.get("description", ""),
            reason_info.get("note", ""),
        )

    kot_ref = ", ".join(cancel_kot_names or [])
    requester = getattr(getattr(fr, "session", None), "user", None) or ""
    se.remarks = (
        f"POS Waste | Check: {pos_invoice.name}"
        + (f" | Cancel KOT: {kot_ref}" if kot_ref else "")
        + f" | Reason: {reason_info.get('stored') or reason_info.get('code')}"
        + f" | By: {requester}"
        + (f" | Approved by: {approver}" if approver else "")
    )

    for line in stock_lines:
        row = {
            "item_code": line["item_code"],
            "qty": flt(line["qty"]),
            "s_warehouse": line["warehouse"] or default_wh,
            "allow_zero_valuation_rate": 1,
        }
        if line.get("uom"):
            row["uom"] = line["uom"]
        if expense_account:
            row["expense_account"] = expense_account
        if cost_center:
            row["cost_center"] = cost_center
        se.append("items", row)

    se.insert(ignore_permissions=True)
    se.submit()

    # Link the posted Stock Entry back onto the cancel KOT(s) and invoice when fields exist.
    if cancel_kot_names and _has_field("URY KOT", "custom_waste_stock_entry"):
        for kot_name in cancel_kot_names:
            try:
                kot_doc = fr.get_doc("URY KOT", kot_name)
                kot_doc.custom_waste_stock_entry = se.name
            except Exception:
                pass
            fr.db.set_value("URY KOT", kot_name, "custom_waste_stock_entry", se.name, update_modified=False)

    inv_name = _field(pos_invoice, "name")
    if _has_field("POS Invoice", "custom_waste_stock_entry") and isinstance(inv_name, str) and inv_name:
        if hasattr(pos_invoice, "db_set"):
            pos_invoice.db_set("custom_waste_stock_entry", se.name, update_modified=False)
        else:
            fr.db.set_value("POS Invoice", inv_name, "custom_waste_stock_entry", se.name, update_modified=False)

    return se.name
