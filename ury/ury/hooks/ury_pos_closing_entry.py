import frappe
from frappe.utils import flt, get_datetime
from frappe import _
from datetime import datetime

from ury.brand import coded_message
from ury.ury.hooks.ury_cash_movement import till_cash_movements, validate_closing_cash
from ury.ury_pos.api import (
	_goal_submitted_since,
	_phase_dependent_rows,
	_role_rank,
)

def _is_pos_invoice_mode():
    return frappe.db.get_single_value("POS Settings", "invoice_type") == "POS Invoice"


def before_validate(doc, method):
    if _is_pos_invoice_mode():
        # Native validation assumes the closing user owns every invoice. URY
        # waiters own them, so the server rebuilds the list after validation.
        doc.set("pos_invoices", [])


def before_submit(doc, method):
    # Either invoice mode: the shift's cash movements are decided and a cash
    # difference has the till's Over/Short account.
    validate_closing_cash(doc)
    if not _is_pos_invoice_mode():
        return

    from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import get_taxes
    from ury.ury_pos.api import _till_invoice_rows

    start, end, inside, before, after = _closing_invoice_rows(doc)

    if after:
        frappe.throw(coded_message("CLOSE-LATE-INVOICE", "Unconsolidated invoices exist after the closing end."))
    if before:
        frappe.throw(coded_message("CLOSE-ORPHAN-INVOICE", "Unconsolidated invoices exist before the opening start."))

    readable = _till_invoice_rows(doc.pos_profile, start, end, frappe.get_list)
    if {row.name for row in inside} != {row.name for row in readable}:
        frappe.throw(coded_message("CLOSE-UNREADABLE-INVOICE", "Not all till invoices are readable by this user."))

    _set_till_invoices(doc, inside)
    doc.set("taxes", [
        {"account_head": tax.account_head, "amount": tax.tax_amount}
        for tax in get_taxes(inside)
    ])
    doc.grand_total = sum(row.grand_total or 0 for row in inside)
    doc.net_total = sum(row.net_total or 0 for row in inside)
    doc.total_quantity = sum(row.total_qty or 0 for row in inside)
    doc.total_taxes_and_charges = sum(row.total_taxes_and_charges or 0 for row in inside)


def _set_till_invoices(doc, invoices):
    doc.set("pos_invoices", [])
    for row in invoices:
        doc.append("pos_invoices", {
            "pos_invoice": row.name,
            "posting_date": row.posting_date,
            "customer": row.customer,
            "grand_total": row.grand_total,
            "is_return": row.is_return,
            "return_against": row.return_against,
        })


def _closing_invoice_rows(doc):
    """Every submitted, unconsolidated invoice of the till, whoever created it,
    split around the linked opening's start and the closing end."""
    start = get_datetime(frappe.db.get_value(
        "POS Opening Entry", doc.pos_opening_entry, "period_start_date"
    ))
    end = get_datetime(doc.period_end_date)
    rows = frappe.get_all(
        "POS Invoice",
        filters={
            "docstatus": 1,
            "pos_profile": doc.pos_profile,
            "consolidated_invoice": ["is", "not set"],
        },
        fields=[
            "name", "posting_date", "posting_time", "customer", "grand_total",
            "net_total", "total_qty", "total_taxes_and_charges", "is_return", "return_against",
            "rounded_total", "change_amount", "account_for_change_amount",
        ],
        order_by="posting_date asc, posting_time asc",
        limit_page_length=0,
    )
    before, inside, after = [], [], []
    for row in rows:
        timestamp = datetime.combine(
            get_datetime(row.posting_date).date(),
            frappe.utils.get_time(row.posting_time),
        )
        if timestamp < start:
            before.append(row)
        elif timestamp > end:
            after.append(row)
        else:
            row.doctype = "POS Invoice"
            inside.append(row)
    return start, end, inside, before, after


def _set_expected_amounts(doc):
    """Server expected cash per mode: opening float plus the till's payments,
    less native change; existing rows are updated in place and keep their order."""
    from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
        get_change_payment_row,
        get_payment_rows_by_invoice,
    )

    _, _, invoices, _, _ = _closing_invoice_rows(doc)
    # The list read by later validate entries (closing reconciliation); the
    # submit-time guards in before_submit rebuild it.
    _set_till_invoices(doc, invoices)
    opening = frappe.get_doc("POS Opening Entry", doc.pos_opening_entry)
    profile = frappe.get_doc("POS Profile", doc.pos_profile)
    precision = frappe.get_precision("POS Closing Entry Detail", "expected_amount")

    opening_by_mode = {}
    for detail in opening.balance_details or []:
        mode = detail.mode_of_payment
        opening_by_mode[mode] = _currency_flt(
            opening_by_mode.get(mode, 0) + flt(detail.opening_amount), precision
        )

    till_modes_by_account = {}
    for payment_mode in profile.get("payments") or []:
        account = frappe.db.get_value(
            "Mode of Payment Account",
            {"parent": payment_mode.mode_of_payment, "company": doc.company},
            "default_account",
        )
        till_modes_by_account.setdefault(account, []).append(payment_mode.mode_of_payment)

    rows_by_invoice = get_payment_rows_by_invoice(invoices) if invoices else {}
    all_seen_modes = list(opening_by_mode.keys())
    for inv_rows in rows_by_invoice.values():
        for r in inv_rows:
            if r.mode_of_payment not in all_seen_modes:
                all_seen_modes.append(r.mode_of_payment)
    home_currency = (profile.get("currency") or "").strip().upper()
    early_fx_modes = _fx_tender_currencies_for_modes(all_seen_modes, home_currency)

    amounts_by_mode = {}
    fallback_changes = {}
    for invoice in invoices:
        rows = rows_by_invoice.get(("POS Invoice", invoice.name), [])
        change_amount = flt(invoice.get("change_amount"))
        if change_amount > 0 and not invoice.get("is_return"):
            candidate = get_change_payment_row(rows, invoice.account_for_change_amount)
            # Never deduct local home-currency change from a foreign-currency tender row.
            if candidate and candidate.mode_of_payment not in early_fx_modes:
                candidate.amount = flt(candidate.amount) - change_amount
            else:
                modes = till_modes_by_account.get(invoice.account_for_change_amount, [])
                if len(modes) != 1:
                    frappe.throw(coded_message(
                        "CLOSE-CHANGE-NO-TILL-MODE",
                        "Map exactly one till payment mode "
                        f"to the change account {invoice.account_for_change_amount} before closing.",
                    ))
                fallback_changes[modes[0]] = fallback_changes.get(modes[0], 0) + change_amount
        for row in rows:
            amounts_by_mode[row.mode_of_payment] = _currency_flt(
                amounts_by_mode.get(row.mode_of_payment, 0) + flt(row.amount),
                precision,
            )

    expected_by_mode = dict(opening_by_mode)
    for mode, amount in amounts_by_mode.items():
        expected_by_mode[mode] = _currency_flt(expected_by_mode.get(mode, 0) + amount, precision)
    for mode, change_amount in fallback_changes.items():
        expected_by_mode[mode] = _currency_flt(expected_by_mode.get(mode, 0) - change_amount, precision)
    # Pickups, loans and paid-outs of the shift move the cash tender's drawer.
    for account, amount in till_cash_movements(doc.pos_opening_entry).items():
        modes = [
            mode for mode in till_modes_by_account.get(account, [])
            if frappe.get_cached_value("Mode of Payment", mode, "type") == "Cash"
            and mode not in early_fx_modes
        ]
        if not modes:
            continue
        if len(modes) != 1:
            frappe.throw(coded_message(
                "CLOSE-MOVEMENT-NO-TILL-MODE",
                "Map exactly one cash till payment mode "
                f"to the drawer account {account} before closing.",
            ))
        expected_by_mode[modes[0]] = _currency_flt(expected_by_mode.get(modes[0], 0) + amount, precision)

    client_rows = {row.mode_of_payment: row for row in doc.payment_reconciliation or []}
    # Existing rows keep their place (and their names); only missing modes are
    # appended, in opening, then expected order. idx is renumbered 1..n.
    modes = [
        row.mode_of_payment for row in doc.payment_reconciliation or []
        if client_rows.get(row.mode_of_payment) is row
    ]
    modes.extend(mode for mode in opening_by_mode if mode not in modes)
    modes.extend(mode for mode in expected_by_mode if mode not in modes)

    home_currency = (profile.get("currency") or "").strip().upper()
    fx_currency_by_mode = _fx_tender_currencies_for_modes(modes, home_currency)
    fx_expected_by_mode = (
        _fx_expected_foreign_by_mode(invoices, rows_by_invoice, fx_currency_by_mode)
        if fx_currency_by_mode
        else {}
    )

    ordered_rows = []
    for mode in modes:
        client_row = client_rows.get(mode)
        expected_amount = _currency_flt(expected_by_mode.get(mode, 0), precision)
        tender_curr = fx_currency_by_mode.get(mode)
        if tender_curr:
            from ury.ury_pos.fx import currency_precision, resolve_pos_exchange_rate

            fx_prec = currency_precision(tender_curr)
            expected_fx = _currency_flt(fx_expected_by_mode.get(mode, 0), fx_prec)
            has_client_fx = (
                client_row is not None
                and client_row.get("custom_closing_foreign_amount") is not None
                and str(client_row.get("custom_closing_foreign_amount")).strip() != ""
            )
            closing_fx = _currency_flt(client_row.get("custom_closing_foreign_amount"), fx_prec) if has_client_fx else 0
            foreign_diff = _currency_flt(closing_fx - expected_fx, fx_prec)
            if expected_fx > 0 and expected_amount > 0:
                shift_rate = flt(expected_amount / expected_fx, 9)
                closing_amount = _currency_flt(closing_fx * shift_rate, precision)
            elif closing_fx > 0:
                try:
                    rate_info = resolve_pos_exchange_rate(
                        tender_curr,
                        home_currency,
                        posting_date=doc.get("posting_date"),
                        company=doc.get("company"),
                    )
                    closing_amount = _currency_flt(closing_fx * rate_info["rate"], precision)
                except Exception:
                    closing_amount = _currency_flt(client_row.closing_amount, precision) if client_row else 0
            else:
                closing_amount = 0
            values = {
                "mode_of_payment": mode,
                "opening_amount": _currency_flt(opening_by_mode.get(mode, 0), precision),
                "expected_amount": expected_amount,
                "custom_closing_amount": (
                    _currency_flt(client_row.custom_closing_amount, precision) if client_row else 0
                ),
                "closing_amount": closing_amount,
                "difference": _currency_flt(closing_amount - expected_amount, precision),
                "custom_tender_currency": tender_curr,
                "custom_expected_foreign_amount": expected_fx,
                "custom_closing_foreign_amount": closing_fx,
                "custom_foreign_difference": foreign_diff,
            }
        else:
            closing_amount = _currency_flt(client_row.closing_amount, precision) if client_row else 0
            values = {
                "mode_of_payment": mode,
                "opening_amount": _currency_flt(opening_by_mode.get(mode, 0), precision),
                "expected_amount": expected_amount,
                "custom_closing_amount": (
                    _currency_flt(client_row.custom_closing_amount, precision) if client_row else 0
                ),
                "closing_amount": closing_amount,
                "difference": _currency_flt(closing_amount - expected_amount, precision),
            }
        if client_row:
            for field, value in values.items():
                setattr(client_row, field, value)
            row = client_row
        else:
            row = doc.append("payment_reconciliation", values)
        ordered_rows.append(row)
    doc.payment_reconciliation[:] = ordered_rows
    for position, row in enumerate(ordered_rows, start=1):
        row.idx = position


def _fx_tender_currencies_for_modes(modes, home_currency):
    if not modes:
        return {}
    try:
        rows = frappe.db.sql(
            """
            SELECT name, custom_tender_currency
            FROM `tabMode of Payment`
            WHERE name IN %s
              AND COALESCE(custom_tender_currency, '') != ''
            """,
            (tuple(modes),),
            as_dict=True,
        )
    except Exception:
        return {}
    result = {}
    for row in rows or []:
        curr = (row.get("custom_tender_currency") or "").strip().upper()
        if curr and curr != home_currency:
            result[row["name"]] = curr
    return result


def _fx_expected_foreign_by_mode(invoices, rows_by_invoice, fx_currency_by_mode):
    if not invoices or not fx_currency_by_mode:
        return {}
    fx_modes = tuple(fx_currency_by_mode.keys())
    inv_names = tuple(inv.name for inv in invoices if inv.get("name"))
    totals = {}
    # Prefer direct DB sum of Sales Invoice Payment.custom_tender_amount so we
    # never depend on ERPNext's get_payment_rows_by_invoice selecting custom fields.
    try:
        sql_rows = frappe.db.sql(
            """
            SELECT mode_of_payment, COALESCE(SUM(custom_tender_amount), 0) AS total_fx
            FROM `tabSales Invoice Payment`
            WHERE parenttype = 'POS Invoice'
              AND parent IN %s
              AND mode_of_payment IN %s
            GROUP BY mode_of_payment
            """,
            (inv_names, fx_modes),
            as_dict=True,
        )
        if sql_rows:
            for row in sql_rows:
                totals[row["mode_of_payment"]] = flt(row.get("total_fx"))
            return totals
    except Exception:
        pass
    for invoice in invoices:
        for row in rows_by_invoice.get(("POS Invoice", invoice.name), []):
            if row.mode_of_payment in fx_currency_by_mode:
                totals[row.mode_of_payment] = totals.get(row.mode_of_payment, 0.0) + flt(
                    row.get("custom_tender_amount")
                )
    return totals


def post_fx_vault_sweep_journal_entry(doc, method=None):
    from ury.ury_pos.fx import post_fx_vault_sweep_journal_entry as _post_sweep

    return _post_sweep(doc, method)


def cancel_fx_vault_sweep_journal_entry(doc, method=None):
    from ury.ury_pos.fx import cancel_fx_vault_sweep_journal_entry as _cancel_sweep

    return _cancel_sweep(doc, method)


def _currency_flt(value, precision):
    # frappe.utils.flt applies the site's System Settings rounding method.
    return flt(value, precision)


def before_save(doc, method):
    sub_pos_close_check(doc, method)

def validate(doc, method):
    if _is_pos_invoice_mode():
        _set_expected_amounts(doc)
    else:
        populate_pos_transactions(doc, method)
    populate_draft_stock_correction(doc)
    calculate_closing_amount(doc, method)
    validate_cashier(doc, method)
    validate_ported_checks(doc, method)

def update_branch_avg_times(doc, method=None):
    try:
        branch = None
        if doc.get("pos_opening_entry"):
            branch = frappe.db.get_value("POS Opening Entry", doc.pos_opening_entry, "branch")
        if not branch and doc.get("pos_profile"):
            branch = frappe.db.get_value("POS Profile", doc.pos_profile, "branch")
        # Only branches that use reservations keep table-time averages.
        if branch and frappe.db.get_value("Branch", branch, "custom_enable_reservation"):
            from ury.ury.report.average_table_time.average_table_time import update_branch_avg_table_times
            update_branch_avg_table_times(branch)
    except Exception as e:
        frappe.log_error(f"Error updating branch average table times on POS Closing: {str(e)}", "POS Closing Avg Table Time")

def populate_draft_stock_correction(doc):
    if doc.get("draft_stock_correction"):
        return
    submit_stock_correction_val = frappe.get_value("POS Profile", doc.pos_profile, "submit_stock_correction")
    if submit_stock_correction_val == 1:
        branch = frappe.get_value("POS Profile", doc.pos_profile, "branch")
        if not branch: return
        corrections = frappe.get_all(
            "Stock Correction",
            filters={"docstatus": 0, "branch": branch, "period_end_date": ["between", [doc.period_start_date, doc.period_end_date]]},
            fields=["name as stock_correction"]
        )
        if corrections:
            for corr in corrections:
                doc.append("draft_stock_correction", {"stock_correction": corr.stock_correction})
        else:
            frappe.msgprint("No draft stock correction found for this shift.", title="Stock Correction Missing", indicator="orange")

def validate_ported_checks(doc, method):
    pos_profile = frappe.get_doc("POS Profile", doc.pos_profile)
    branch = pos_profile.branch
    warehouse = pos_profile.warehouse
    errors = []

    start_date = doc.period_start_date
    end_date = doc.period_end_date
    period_start_date = get_datetime(start_date).date()

    if pos_profile.validate_stock_correction:
        stock_correction = frappe.db.get_value(
            "Stock Correction", 
            {"branch": branch, "set_warehouse": warehouse, "period_end_date": ["between", [start_date, end_date]]}, 
            "name"
        )
        if not stock_correction:
            errors.append("Stock Correction: not generated for today")

    if pos_profile.validate_daily_p_and_l:
        daily_p_and_l = frappe.db.get_value(
            "URY Daily P and L", 
            {"docstatus": 1, "branch": branch, "date": period_start_date}, 
            "name"
        )
        if not daily_p_and_l:
            errors.append("Daily P and L: No entries today")

    if pos_profile.validate_attendance:
        attnd_query = """
            SELECT a.name
            FROM `tabAttendance` a
            INNER JOIN `tabEmployee` b ON b.name = a.employee
            WHERE b.branch = %s AND a.docstatus = 1 AND a.attendance_date = %s
        """
        attendance = frappe.db.sql(attnd_query, (branch, period_start_date), as_dict=True)
        if not attendance:
            errors.append("Attendance: Not marked for today")

    if pos_profile.validate_wastage:
        wtg_query = """
            SELECT name
            FROM `tabURY Wastage`
            WHERE docstatus = 1 AND branch = %s AND `datetime` >= %s AND `datetime` <= %s
        """
        wastage = frappe.db.sql(wtg_query, (branch, start_date, end_date), as_dict=True)
        if not wastage:
            errors.append("Wastage: No recording for today")

    if errors:
        frappe.throw(errors, title="Validation Error", as_list=True)



def populate_pos_transactions(doc, method):
    """Rebuild `pos_transactions` server-side from unconsolidated POS
    Invoices for this closing user/profile/period, exactly like
    ``SubPOSClosing.validate()`` does for the sub-cashier close.

    The custom POS frontend's ``createPosClosingEntry`` call (unlike native
    ERPNext's own Desk JS, which builds the child table client-side via
    ``make_closing_entry_from_opening`` before creating the document) never
    sends ``pos_transactions`` -- it only fetches invoices to render the
    on-screen closing totals. Without this, a POS Closing Entry created via
    the custom frontend submits with an empty ``pos_transactions``, so
    ``consolidate_pos_invoices()`` (called from ``on_submit``) has nothing
    to merge and no consolidated Sales Invoice is created for the session,
    even though the closing totals shown to the cashier were correct.

    Only fills the table when it is empty, so an explicit caller-supplied
    ``pos_transactions`` (e.g. a future frontend fix, or native Desk usage)
    is never overwritten.

    ERPNext core's own ``POSClosingEntry.validate_pos_invoices()`` (invoked
    again on submit) requires ``pos_invoice.owner == self.user`` for every
    row in this table -- a check against the *creator* of the invoice, not
    this app's custom ``cashier`` field, which can differ from the creator
    in multi-cashier POS Profiles (see ``ury_order.py``'s ``main_cashier`` /
    ``pos_opened_cashier`` assignment). This function populates exactly the
    child table core's submit-time check validates against ``owner``, so it
    selects candidates by ``owner`` up front -- the query itself guarantees
    every row satisfies core's invariant, rather than selecting by
    ``cashier`` and then dropping rows that fail it after the fact.
    """
    if doc.get("pos_invoices"):
        return
    if not doc.pos_profile or not doc.period_start_date or not doc.period_end_date:
        return

    invoices = frappe.get_all(
        "POS Invoice",
        filters={
            "docstatus": 1,
            "pos_profile": doc.pos_profile,
            "owner": doc.user,
            "posting_date": ["between", [doc.period_start_date, doc.period_end_date]],
        },
        fields=["name", "owner", "posting_date", "posting_time", "customer", "grand_total", "net_total", "total_qty", "consolidated_invoice"],
    )

    period_start = get_datetime(doc.period_start_date)
    period_end = get_datetime(doc.period_end_date)

    for invoice in invoices:
        if invoice.consolidated_invoice:
            continue
        invoice_ts = get_datetime(f"{invoice.posting_date} {invoice.posting_time or '00:00:00'}")
        if not (period_start <= invoice_ts <= period_end):
            continue
        doc.append(
            "pos_invoices",
            {
                "pos_invoice": invoice.name,
                "posting_date": invoice.posting_date,
                "grand_total": invoice.grand_total,
                "customer": invoice.customer,
            },
        )


def sub_pos_close_check(doc,method):
    cashier = None
    multiple_cashier = frappe.db.get_value("POS Profile",doc.pos_profile,"custom_enable_multiple_cashier")
    if multiple_cashier:
        get_cashier = frappe.get_doc("POS Profile", doc.pos_profile)
        for user_details in get_cashier.applicable_for_users:
            if not user_details.custom_main_cashier:
                cashier = user_details.user
        if frappe.session.user != cashier:
            branch=frappe.db.get_value("POS Profile",doc.pos_profile,"branch")
            pos_opening_list = frappe.get_all(
                "POS Opening Entry",
                fields=["name", "docstatus", "status", "posting_date"],
                filters={"branch": branch,"user":cashier},
            )
            flag = 0
            for pos_opening in pos_opening_list:
                if pos_opening.status == "Open" and pos_opening.docstatus == 1:
                    flag = 1
            if flag == 1:
                frappe.throw(("Sub Cashier POS  must be closed"), title=("Sub Cashier POS Closing Required"))
                
            return flag
    else:
        pass

def calculate_closing_amount(doc, method):
    multiple_cashier = frappe.db.get_value("POS Profile",doc.pos_profile,"custom_enable_multiple_cashier")
    if multiple_cashier:  
        sub_pos_closing = frappe.get_all(
            "Sub POS Closing",
            filters=[
                ["posting_date", "<=", doc.posting_date],
                ["period_start_date", ">=", doc.period_start_date],
                ["docstatus", "=", 1]
            ],
            fields=["name"] 
        )
        if sub_pos_closing:
            for closing_details in doc.payment_reconciliation:
                sub_closing_amount = frappe.db.get_value("Sub POS Closing Payment",{"parent":sub_pos_closing[0].name,"mode_of_payment":closing_details.mode_of_payment},"closing_amount") or 0
                main_closing_amount = closing_details.custom_closing_amount or 0
                total_closing_amount = sub_closing_amount + main_closing_amount
                closing_details.closing_amount = total_closing_amount
                closing_details.difference = total_closing_amount - closing_details.expected_amount
        else:
            frappe.throw("No Sub POS Closing entries found between the given dates")
            return None
    else:
        pass
def validate_cashier(doc, method):
    cashier = None
    multiple_cashier = frappe.db.get_value("POS Profile",doc.pos_profile,"custom_enable_multiple_cashier")
    if multiple_cashier:
        get_cashier = frappe.get_doc("POS Profile", doc.pos_profile)
        for user_details in get_cashier.applicable_for_users:
            if not user_details.custom_main_cashier:
                cashier = user_details.user
        if frappe.session.user == cashier:
            frappe.throw("Sub Cashiers are not allowed to make POS Closing Entries.")
    else:
        pass

def validate_daily_checklists(doc, method):
	"""Closing runs Order Taker -> Cashier -> Restaurant Manager, and the
	POS Closing Entry is the FINAL step -- the cashier may close only after
	all three submitted their closing checklist for this shift (each review
	created since the shift opened). Role-level: one submission per role
	suffices no matter how many users hold it. A submitted checklist may
	contain FAIL objectives -- valid response, not a blocker."""
	pos_profile = frappe.get_doc("POS Profile", doc.pos_profile)
	branch = pos_profile.branch

	# Parse start date
	start_date = doc.period_start_date
	if isinstance(start_date, str):
		try:
			start_date = datetime.strptime(start_date, "%Y-%m-%d %H:%M:%S.%f")
		except ValueError:
			start_date = datetime.strptime(start_date, "%Y-%m-%d %H:%M:%S")

	non_completed_checklists = []

	def validate_and_throw(error_messages):
		if error_messages != []:
			error_list = [_("{}".format(msg)) for msg in error_messages]
			frappe.throw(error_list, title=_("Validation Error"), as_list=True)

	for row in _phase_dependent_rows(doc.pos_profile, "Closing"):
		if _goal_submitted_since(row.quality_checklist, branch, start_date):
			doc.append(
				"quality_checklist",
				{"checklist": row.quality_checklist, "check_2": 1},
			)
		elif _role_rank(row.role) is not None:
			# Every ranked role in the closing sequence gates the closing
			# document: Order Taker AND Cashier AND Restaurant Manager.
			non_completed_checklists.append(
				_("Pending checklist: {} ").format(
					frappe.bold(row.quality_checklist)
				)
			)

	validate_and_throw(non_completed_checklists)




def submit_stock_correction(doc, method=None):
	submit_stock_correction_val = frappe.get_value("POS Profile", doc.pos_profile, "submit_stock_correction")
	if submit_stock_correction_val == 1:
		if not doc.get("draft_stock_correction"):
			frappe.throw("Cannot submit POS Closing Entry: No draft stock correction entries found against the branch.", title="Stock Correction Required")
		
		for row in doc.get("draft_stock_correction"):
			if row.stock_correction:
				frappe.enqueue(
					"ury.ury.hooks.ury_pos_closing_entry.process_stock_correction",
					queue="long",
					timeout=1500,
					closing_entry=doc.name,
					stock_correction_name=row.stock_correction,
					pos_profile=doc.pos_profile,
					period_start_date=doc.period_start_date
				)

def process_stock_correction(closing_entry, stock_correction_name, pos_profile, period_start_date):
	from frappe.utils import getdate, get_time
	from datetime import datetime, timedelta
	try:
		wstg_time = frappe.db.get_value("POS Profile", pos_profile, "custom_wastage_time")
		sales_time = frappe.db.get_value("POS Profile", pos_profile, "custom_sales_closing_time")
		p_time = wstg_time if wstg_time else sales_time
		
		p_date = getdate(period_start_date)
		if p_time:
			date_time = datetime.combine(p_date, get_time(p_time))
		else:
			date_time = datetime.combine(p_date, datetime.now().time())
			
		delay_minutes = 10
		new_date_time = date_time + timedelta(minutes=delay_minutes)
		
		date = new_date_time.date()
		time = new_date_time.time()
		
		sc_doc = frappe.get_doc("Stock Correction", stock_correction_name)
		owner = sc_doc.owner
		sc_doc.db_set("edit_posting_date", 1)
		sc_doc.posting_date = date
		sc_doc.posting_time = time
		sc_doc.save()
		sc_doc.submit()
		
		frappe.db.set_value(
			"Stock Correction",
			stock_correction_name,
			"modified_by",
			owner,
			update_modified=False,
		)
		frappe.db.commit()
	except Exception as e:
		error_msg = f"Stock Correction Failed for POS Closing Entry {closing_entry}: {str(e)}"
		sc_doc = frappe.get_doc("Stock Correction", stock_correction_name)
		sc_doc.db_set("status", "Failed")
		sc_doc.db_set("error", str(e))
		frappe.log_error(error_msg, "Stock Correction Failed")
		frappe.db.commit()