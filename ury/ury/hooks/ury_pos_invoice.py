import frappe
from datetime import datetime
from frappe.utils import now_datetime, get_time, now, flt, getdate, get_first_day, get_last_day, get_datetime
from ury.ury.doctype.ury_order.ury_order import release_merge_cluster_tables
from ury.ury.doctype.staff_discount_policy.staff_discount_policy import get_applicable_policy


def before_insert(doc, method):
    pos_invoice_naming(doc, method)
    order_type_update(doc, method)
    restrict_existing_order(doc, method)


def validate(doc, method):
    validate_invoice(doc, method)
    validate_customer(doc, method)
    validate_price_list(doc, method)
    validate_server_only_tender_received(doc, method)
    set_commission_attribution(doc, method)
    apply_staff_discount_policy(doc, method)
    validate_table_reservation_lock(doc, method)


def before_submit(doc, method):
    calculate_and_set_times(doc, method)
    validate_invoice_print(doc, method)
    validate_confirmed_phone_payments(doc, method)
    ro_reload_submit(doc, method)
    set_commission_attribution(doc, method)
    apply_disposable_items(doc, method)


def on_trash(doc, method):
    table_status_delete(doc, method)


def _employee_for_user(user):
    if not user:
        return None
    # Prefer active employees, then oldest joiners, for deterministic selection.
    rows = frappe.db.sql(
        """
        SELECT name, status
        FROM `tabEmployee`
        WHERE user_id = %s
        ORDER BY CASE WHEN status = 'Active' THEN 0 ELSE 1 END,
                 date_of_joining ASC,
                 name ASC
        LIMIT 1
        """,
        (user,),
        as_dict=True,
    )
    return rows[0]["name"] if rows else None


def set_commission_attribution(doc, method=None):
    # Opener is sticky: never overwrite once set, so captain_transfer()
    # reassigning `waiter` does not move historical credit.
    if not doc.get("custom_waiter_employee"):
        doc.custom_waiter_employee = _employee_for_user(doc.waiter)
    doc.custom_closing_employee = _employee_for_user(doc.cashier) or doc.get("custom_closing_employee")
    for item in doc.get("items") or []:
        if not item.get("custom_entered_by_employee"):
            item.custom_entered_by_employee = _employee_for_user(item.owner or frappe.session.user)


def apply_disposable_items(doc, method=None):
    """Auto-append disposable/packaging items as zero-rated POS Invoice Item rows.

    Mirrors the original grillax `pos_disposables` before_submit hook, adapted to
    ury's field names and doctypes:
      - Item.disposable_items (Disposable Items, per source item, split by dine_in)
      - POS Profile.table_disposables / .parcel_disposables (base disposables)
      - POS Invoice Item.disposable_items (POS Disposable Item, per-line record)
      - POS Invoice Item.is_disposable (marks the generated zero-rate line)

    Runs once per submit; skips re-adding if disposable lines already exist
    (e.g. on a resubmit of a doc that already went through this).
    """
    if not doc.get("items"):
        return

    if any(item.get("is_disposable") for item in doc.items):
        return

    pos_profile = frappe.get_cached_doc("POS Profile", doc.pos_profile)
    is_dine_in = doc.order_type == "Dine In"

    disposables_dict = {}

    for item in doc.items:
        if item.get("is_disposable"):
            continue

        item_disposables = frappe.get_all(
            "Disposable Items",
            filters={"parent": item.item_code, "dine_in": 1 if is_dine_in else 0},
            fields=["item", "item_name", "qty"],
        )

        for disposable in item_disposables:
            qty = flt(disposable.qty) * flt(item.qty)
            key = disposable.item
            if key in disposables_dict:
                disposables_dict[key]["qty"] += qty
            else:
                disposables_dict[key] = {
                    "item": disposable.item,
                    "item_name": disposable.item_name,
                    "qty": qty,
                }

            # Record on the source cart item's own disposable_items child table.
            item.append(
                "disposable_items",
                {
                    "item": disposable.item,
                    "item_name": disposable.item_name,
                    "qty": qty,
                },
            )

    if is_dine_in:
        n_pax = flt(doc.no_of_pax) or 1
        for table_disposable in pos_profile.get("table_disposables") or []:
            key = table_disposable.item
            qty = flt(table_disposable.qty) * n_pax
            if key in disposables_dict:
                disposables_dict[key]["qty"] += qty
            else:
                disposables_dict[key] = {
                    "item": table_disposable.item,
                    "item_name": table_disposable.item_name,
                    "qty": qty,
                }
    else:
        for parcel_disposable in pos_profile.get("parcel_disposables") or []:
            key = parcel_disposable.item
            qty = flt(parcel_disposable.qty)
            if key in disposables_dict:
                disposables_dict[key]["qty"] += qty
            else:
                disposables_dict[key] = {
                    "item": parcel_disposable.item,
                    "item_name": parcel_disposable.item_name,
                    "qty": qty,
                }

    for disposable_item in disposables_dict.values():
        item_doc = frappe.get_cached_doc("Item", disposable_item["item"])
        doc.append(
            "items",
            {
                "item_code": item_doc.item_code,
                "item_name": item_doc.item_name,
                "description": item_doc.description,
                "is_disposable": 1,
                "qty": disposable_item["qty"],
                "rate": 0,
                "base_rate": 0,
                "amount": 0,
                "base_amount": 0,
                "conversion_factor": 1.0,
                "uom": item_doc.stock_uom,
                "income_account": pos_profile.get("income_account"),
                "expense_account": pos_profile.get("expense_account"),
                "cost_center": pos_profile.get("cost_center"),
                "warehouse": pos_profile.get("warehouse"),
            },
        )


def _period_window(period):
    """Return (start_date, end_date) for the given policy period, as of today."""
    on_date = getdate(frappe.utils.today())
    if period == "Daily":
        return on_date, on_date
    if period == "Weekly":
        # Week starts Monday.
        start = frappe.utils.add_days(on_date, -on_date.weekday())
        end = frappe.utils.add_days(start, 6)
        return start, end
    if period == "Monthly":
        return get_first_day(on_date), get_last_day(on_date)
    return None, None


def _period_to_date_discount(policy_name, doc_name, customer_group=None, employee=None, period=None):
    """Sum discount_amount already applied under `policy_name` on SUBMITTED
    (docstatus == 1) POS Invoices within the current period window, for the
    same customer group (Customer Group policies) or the same cashier
    employee (Role / Employee Group policies). Excludes the invoice
    currently being validated. Cancelled (docstatus == 2) and draft
    (docstatus == 0) invoices are naturally excluded by the docstatus filter.
    """
    if not period or period == "None":
        return 0

    start, end = _period_window(period)
    if not start:
        return 0

    conditions = ["pi.docstatus = 1", "pi.staff_discount_policy = %(policy)s", "pi.name != %(doc_name)s"]
    conditions.append("pi.posting_date BETWEEN %(start)s AND %(end)s")

    values = {
        "policy": policy_name,
        "doc_name": doc_name or "",
        "start": start,
        "end": end,
    }

    joins = ""
    if customer_group:
        joins = "INNER JOIN `tabCustomer` cust ON cust.name = pi.customer"
        conditions.append("cust.customer_group = %(customer_group)s")
        values["customer_group"] = customer_group
    elif employee:
        conditions.append("pi.custom_closing_employee = %(employee)s")
        values["employee"] = employee
    else:
        return 0

    query = f"""
        SELECT COALESCE(SUM(pi.discount_amount), 0) AS total
        FROM `tabPOS Invoice` pi
        {joins}
        WHERE {' AND '.join(conditions)}
    """
    result = frappe.db.sql(query, values, as_dict=True)
    return flt(result[0]["total"]) if result else 0


def apply_staff_discount_policy(doc, method=None):
    """Resolve and enforce the applicable Staff Discount Policy on validate.

    This is VALIDATION-ONLY: it never assigns doc.discount_amount. By the
    time this runs, ERPNext's own calculate_taxes_and_totals() has already
    computed grand_total/rounded_total/outstanding_amount off of whatever
    discount_amount the cashier/POS UI already put on the doc, so mutating
    discount_amount here (after the fact, in before_submit) would silently
    desync the displayed discount from what was actually charged/posted.
    Instead:

    - Resolves the best-matching enabled Staff Discount Policy for this
      invoice's customer/employee/branch/item context via
      get_applicable_policy().
    - Reads the EXISTING doc.discount_amount (set earlier, before totals
      were calculated) and enforces per_transaction_cap against it (throws
      if this single invoice's discount exceeds it).
    - Computes the period-to-date discount already applied under this SAME
      policy (see _period_to_date_discount) and enforces period_cap (throws
      if adding this invoice's existing discount would exceed it).
    - If within caps, records the resolved policy on
      doc.staff_discount_policy so future period-sum queries can filter
      cleanly by policy. Does NOT compute or assign a new discount_amount.
    """
    employee = doc.get("custom_closing_employee")
    branch = doc.get("branch")

    item_groups = set()
    for item in doc.get("items") or []:
        if item.item_code:
            item_group = frappe.get_cached_value("Item", item.item_code, "item_group")
            if item_group:
                item_groups.add(item_group)

    policy = None
    for item_group in item_groups or [None]:
        policy = get_applicable_policy(
            customer=doc.get("customer"),
            employee=employee,
            branch=branch,
            item_group=item_group,
        )
        if policy:
            break

    if not policy:
        return

    # Validate against whatever discount_amount is ALREADY on the doc
    # (set by the cashier/POS UI before totals were calculated) — never
    # compute or assign a new value here.
    discount_amount = flt(doc.get("discount_amount"))

    per_transaction_cap = flt(policy.get("per_transaction_cap"))
    if per_transaction_cap and discount_amount > per_transaction_cap:
        frappe.throw(
            (
                "Staff Discount Policy {0} caps the per-transaction discount at {1}. "
                "This invoice's discount ({2}) exceeds that limit."
            ).format(policy.get("policy_name") or policy.get("name"), per_transaction_cap, discount_amount)
        )

    period = policy.get("period")
    period_cap = flt(policy.get("period_cap"))
    if period and period != "None" and period_cap:
        customer_group = None
        matching_employee = None
        if policy.get("applies_to") == "Customer Group":
            customer_group = frappe.db.get_value("Customer", doc.get("customer"), "customer_group")
        else:
            matching_employee = employee

        period_to_date = _period_to_date_discount(
            policy.get("name"),
            doc.name,
            customer_group=customer_group,
            employee=matching_employee,
            period=period,
        )

        if period_to_date + discount_amount > period_cap:
            frappe.throw(
                (
                    "Staff Discount Policy {0} caps total {1} discounts at {2}. "
                    "{3} has already been applied this period; this invoice's discount "
                    "({4}) would exceed the cap."
                ).format(
                    policy.get("policy_name") or policy.get("name"),
                    period,
                    period_cap,
                    period_to_date,
                    discount_amount,
                )
            )

    doc.staff_discount_policy = policy.get("name")


def _line_identity(item):
    # The stable line key sync_order carries across saves, else the row itself; never the
    # item code, since two lines of one item are two lines.
    return item.get("reservation_line_key") or item.name


def validate_invoice(doc, method):
    if doc.waiter == None or doc.waiter == "":
        doc.waiter = doc.modified_by
    if getattr(frappe.flags, "ury_bill_split", False):
        return
    if getattr(frappe.flags, "ury_qty_reduction", False):
        return
    # A new document has no printed copy to compare against, and a return refunds a paid
    # check; neither is an edit of a printed bill.
    if doc.is_new() or doc.get("is_return"):
        return
    remove_items = frappe.db.get_value("POS Profile", doc.pos_profile, "remove_items")

    if doc.invoice_printed == 1 and remove_items == 0:
        # Get the original items from db
        original_doc = frappe.get_doc("POS Invoice", doc.name)

        # Create dictionaries to store both quantities and names, one entry per line
        original_items = {
            _line_identity(item): {"qty": item.qty, "name": item.item_name}
            for item in original_doc.items
        }
        current_items = {
            _line_identity(item): {"qty": item.qty, "name": item.item_name}
            for item in doc.items
        }

        # Check for removed items
        removed_items = set(original_items.keys()) - set(current_items.keys())

        # Check for quantity reductions
        reduced_qty_items = []
        for line, item_data in original_items.items():
            if (line in current_items and
                current_items[line]["qty"] < item_data["qty"]):
                reduced_qty_items.append(
                    f"{item_data['name']} (qty reduced from {item_data['qty']} "
                    f"to {current_items[line]['qty']})"
                )

        if removed_items or reduced_qty_items:
            error_msg = []
            if removed_items:
                removed_item_names = [
                    original_items[line]["name"]
                    for line in removed_items
                ]
                error_msg.append(f"Removed items: {', '.join(removed_item_names)}")
            if reduced_qty_items:
                error_msg.append(f"Modified quantities: {', '.join(reduced_qty_items)}")
                
            frappe.throw(
                ("Cannot modify items after invoice is printed.\n{0}")
                .format("\n".join(error_msg))
            )


def validate_customer(doc, method):
    if not doc.customer and not doc.customer_name:
        frappe.throw(_("Customer is required."))


LIVE_PHONE_PAID_STATUSES = ["Paid", "Partially Paid", "Partially Refunded"]


def _is_phone_payment_row(row):
    if row.get("type") == "Phone":
        return True
    mode = row.get("mode_of_payment")
    if not mode:
        return False
    return frappe.db.get_value("Mode of Payment", mode, "type") == "Phone"


def _active_received_db_rows(doc, existing_rows):
    """Filter DB custom_tender_received=1 rows to those that are still genuinely received.
    Phone rows whose Payment Request is no longer live Paid/Partially Paid/Partially Refunded
    with positive paid amount are excluded so cancelled/failed/expired/refunded Phone requests
    can be cleared or settled with Cash."""
    if not existing_rows:
        return []

    has_phone = any(_is_phone_payment_row(r) for r in existing_rows)
    live_pr_names = set()
    live_paid_by_mode = {}
    if has_phone:
        paid_requests = frappe.get_all(
            "Payment Request",
            filters={
                "reference_doctype": "POS Invoice",
                "reference_name": doc.name,
                "docstatus": 1,
                "status": ["in", LIVE_PHONE_PAID_STATUSES],
            },
            fields=["name", "mode_of_payment", "grand_total", "outstanding_amount"],
        )
        if not isinstance(paid_requests, (list, tuple)):
            paid_requests = []
        for pr in paid_requests:
            paid_amt = flt(pr.get("grand_total")) - flt(pr.get("outstanding_amount"))
            if paid_amt <= 0:
                continue
            if pr.get("name"):
                live_pr_names.add(pr.get("name"))
            pr_mode = pr.get("mode_of_payment")
            if pr_mode:
                live_paid_by_mode[pr_mode] = live_paid_by_mode.get(pr_mode, 0.0) + paid_amt

    active = []
    for stored in existing_rows:
        if _is_phone_payment_row(stored):
            ref = (stored.get("reference_no") or "").strip()
            mode = stored.get("mode_of_payment")
            if ref:
                if ref not in live_pr_names:
                    continue
            else:
                if not mode or live_paid_by_mode.get(mode, 0.0) <= 0:
                    continue
                live_paid_by_mode[mode] = max(0.0, live_paid_by_mode[mode] - flt(stored.get("amount")))
        active.append(stored)
    return active


def validate_server_only_tender_received(doc, method=None):
    """custom_tender_received on payment rows may only be set, modified, or cleared by
    confirmed server flows. Always compares against DB rows so a raw REST PUT, client
    save, or background Data Import (even as Administrator) cannot drop, unflag, reduce,
    re-mode, or replace an already-received tender."""
    if getattr(frappe.flags, "in_patch", False) or getattr(frappe.flags, "in_migrate", False):
        return
    if doc.get("is_return") or int(doc.get("docstatus") or 0) == 2:
        return
    if (
        getattr(frappe.flags, "ury_server_tender_received", False)
        and not getattr(frappe.flags, "in_import", False)
    ):
        return

    payments = doc.get("payments") or []
    flagged = [row for row in payments if int(row.get("custom_tender_received") or 0) == 1]

    if doc.is_new():
        if flagged:
            frappe.throw(
                frappe._("Only confirmed server payment flows can mark a tender as received."),
                frappe.ValidationError,
            )
        return

    existing_rows = frappe.get_all(
        "Sales Invoice Payment",
        filters={
            "parent": doc.name,
            "parenttype": "POS Invoice",
            "custom_tender_received": 1,
        },
        fields=["name", "mode_of_payment", "amount", "reference_no", "type"],
    )
    if not isinstance(existing_rows, (list, tuple)):
        existing_rows = []

    if not flagged and not existing_rows:
        return

    all_by_name = {r.get("name"): r for r in existing_rows if r.get("name")}
    active_existing = _active_received_db_rows(doc, existing_rows)
    active_by_name = {r.get("name"): r for r in active_existing if r.get("name")}

    precision = doc.precision("paid_amount") if callable(getattr(doc, "precision", None)) else 2
    if not isinstance(precision, int) or isinstance(precision, bool):
        precision = 2

    # 1. No client may forge or tamper with custom_tender_received=1 on any row.
    for row in flagged:
        stored = all_by_name.get(row.get("name"))
        if not stored:
            frappe.throw(
                frappe._("Only confirmed server payment flows can mark a tender as received."),
                frappe.ValidationError,
            )
        if (
            stored.get("mode_of_payment") != row.get("mode_of_payment")
            or round(flt(stored.get("amount")), precision) != round(flt(row.get("amount")), precision)
            or (stored.get("reference_no") or "").strip() != (row.get("reference_no") or "").strip()
        ):
            frappe.throw(
                frappe._("Only confirmed server payment flows can mark a tender as received."),
                frappe.ValidationError,
            )

    # 2. No client may drop, unflag, reduce, re-mode, or replace any active received row.
    current_by_name = {row.get("name"): row for row in payments if row.get("name")}
    for row_name, stored in active_by_name.items():
        current = current_by_name.get(row_name)
        if (
            not current
            or int(current.get("custom_tender_received") or 0) != 1
            or current.get("mode_of_payment") != stored.get("mode_of_payment")
            or round(flt(current.get("amount")), precision) != round(flt(stored.get("amount")), precision)
            or (current.get("reference_no") or "").strip() != (stored.get("reference_no") or "").strip()
        ):
            frappe.throw(
                frappe._("Cannot drop, reduce, or alter a tender already received on this check."),
                frappe.ValidationError,
            )


def validate_confirmed_phone_payments(doc, method=None):
    """Refuse submitting any Phone payment row without a confirmed Paid Payment Request.

    ERPNext POSInvoice.check_phone_payments() skips validation when no Paid Payment Request
    exists (paid_amt is None), which lets an unconfirmed mobile-money check close as paid.
    """
    if doc.get("is_return"):
        return

    phone_totals = {}
    for row in doc.get("payments") or []:
        amt = flt(row.get("amount"))
        if amt <= 0:
            continue
        if not _is_phone_payment_row(row):
            continue
        mode = row.get("mode_of_payment")
        phone_totals[mode] = phone_totals.get(mode, 0.0) + amt

    if not phone_totals:
        return

    precision = doc.precision("paid_amount") if callable(getattr(doc, "precision", None)) else 2
    if not isinstance(precision, int) or isinstance(precision, bool):
        precision = 2

    for mode, requested_total in phone_totals.items():
        paid_requests = frappe.get_all(
            "Payment Request",
            filters={
                "reference_doctype": "POS Invoice",
                "reference_name": doc.name,
                "mode_of_payment": mode,
                "docstatus": 1,
                "status": ["in", LIVE_PHONE_PAID_STATUSES],
            },
            fields=["name", "grand_total", "outstanding_amount"],
        )
        if not isinstance(paid_requests, (list, tuple)):
            paid_requests = []
        paid_total = sum(
            max(0.0, flt(pr.get("grand_total")) - flt(pr.get("outstanding_amount")))
            for pr in paid_requests
        )
        if round(flt(paid_total), precision) <= 0:
            frappe.throw(
                frappe._(
                    "Payment for {0} has not been confirmed yet. Confirm the payment or choose another payment method."
                ).format(mode),
                frappe.ValidationError,
            )
        if round(flt(requested_total), precision) != round(flt(paid_total), precision):
            frappe.throw(
                frappe._("Confirmed payment for {0} is {1}, not {2}.").format(
                    mode, round(flt(paid_total), precision), round(flt(requested_total), precision)
                ),
                frappe.ValidationError,
            )


def calculate_and_set_times(doc, method=None):
    if not getattr(doc, "creation", None):
        doc.creation = now_datetime()

    doc.arrived_time = doc.creation

    current_time = get_datetime(now()) or now_datetime()
    creation_time = get_datetime(doc.creation) or current_time

    time_difference = current_time - creation_time

    total_seconds = max(0, int(time_difference.total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    formatted_spend_time = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    doc.total_spend_time = formatted_spend_time


def validate_invoice_print(doc, method):
    if not doc.restaurant_table:
        return

    pos_profile = getattr(doc, "pos_profile", None)
    if pos_profile:
        enable_pwp = frappe.db.get_value(
            "POS Profile", pos_profile, "custom_enable_payment_without_printing"
        )
        if enable_pwp:
            return

    # Check if the invoice has been printed
    invoice_printed = frappe.db.get_value("POS Invoice", doc.name, "invoice_printed") or 0

    if invoice_printed == 0:
        frappe.throw(
            "Printing the invoice is mandatory before submitting. Please print the invoice."
        )


def table_status_delete(doc, method):
    if doc.restaurant_table:
        release_merge_cluster_tables(doc.restaurant_table, commit=False)


def pos_invoice_naming(doc, method):
    if not getattr(doc, "pos_profile", None):
        return
    if not frappe.db.exists("POS Profile", doc.pos_profile):
        return

    pos_profile = frappe.db.get_value("POS Profile", doc.pos_profile, ["restaurant"], as_dict=True)
    if not pos_profile or not pos_profile.get("restaurant"):
        return

    restaurant = pos_profile.get("restaurant")

    if not doc.restaurant_table:
        prefix = frappe.db.get_value(
            "URY Restaurant", restaurant, "invoice_series_prefix"
        )
        if prefix:
            doc.naming_series = prefix

        if doc.order_type == "Aggregators":
            agg_prefix = frappe.db.get_value(
                "URY Restaurant", restaurant, "aggregator_series_prefix"
            )
            if agg_prefix:
                doc.naming_series = agg_prefix
    


def order_type_update(doc, method):
    if doc.restaurant_table:
        if not doc.order_type:
            is_take_away = frappe.db.get_value(
                "URY Table", doc.restaurant_table, "is_take_away"
            )
            if is_take_away == 1:
                doc.order_type = "Take Away"
            else:
                doc.order_type = "Dine In"
    


# reload restaurant order page if submitted invoice is open there
def ro_reload_submit(doc, method):
    frappe.publish_realtime("reload_ro", {"name": doc.name})


def validate_price_list(doc, method):
    """Keep the price list of the menu this check was rung on.

    Resolution mirrors how sync_order prices a line, so validate never re-prices a check from
    another menu: an aggregator check uses its Aggregator Settings price list; a table check
    uses its room's menu when the restaurant prices by room; a counter check uses its order
    type's menu when the restaurant prices by order type; otherwise the active menu. The
    menu's enabled Price List is the check's price list.
    """
    if not doc.restaurant:
        return

    if doc.order_type == "Aggregators":
        price_list = frappe.db.get_value("Aggregator Settings",
            {"customer": doc.customer, "parent": doc.branch, "parenttype": "Branch"},
            "price_list",
            )

        if not price_list:
            frappe.throw(f"Price list for customer {doc.customer} in branch {doc.branch} not found in Aggregator Settings.")

        doc.selling_price_list = price_list
        return

    menu_name = None
    if doc.restaurant_table:
        if frappe.db.get_value("URY Restaurant", doc.restaurant, "room_wise_menu"):
            room = frappe.db.get_value("URY Table", doc.restaurant_table, "restaurant_room")
            menu_name = frappe.db.get_value(
                "Menu for Room", {"parent": doc.restaurant, "room": room}, "menu"
            )
    elif frappe.db.get_value("URY Restaurant", doc.restaurant, "order_type_wise_menu"):
        menu_name = frappe.db.get_value(
            "Order Type Menu", {"parent": doc.restaurant, "order_type": doc.order_type}, "menu"
        )

    menu_name = menu_name or frappe.db.get_value("URY Restaurant", doc.restaurant, "active_menu")

    doc.selling_price_list = frappe.db.get_value(
        "Price List", dict(restaurant_menu=menu_name, enabled=1)
    )


def restrict_existing_order(doc, event):
    if not doc.restaurant_table:
        return

    if getattr(frappe.flags, "ury_bill_split", False):
        return

    if doc.get("custom_split_from"):
        source = frappe.db.get_value(
            "POS Invoice",
            doc.custom_split_from,
            ["docstatus", "restaurant_table"],
            as_dict=True,
        )
        if (
            source
            and source.docstatus == 0
            and (source.restaurant_table or "") == (doc.restaurant_table or "")
        ):
            return

    # Enforce table reservation buffer lock check for Dine In orders
    if (doc.order_type or "Dine In") == "Dine In" and doc.restaurant_table:
        from ury.ury.api.table_reservation import check_table_reservation, parse_to_datetime
        active_res = check_table_reservation(doc.restaurant_table)
        if active_res and active_res.get("is_lock_window_active") and active_res.get("status") == "Confirmed":
            res_time_raw = active_res.get("reserved_at")
            formatted_res_time = ""
            if res_time_raw:
                try:
                    res_dt = parse_to_datetime(res_time_raw)
                    formatted_res_time = res_dt.strftime("%I:%M %p").lstrip("0")
                except Exception:
                    formatted_res_time = str(res_time_raw)
            frappe.throw(
                frappe._("Table {0} is reserved for {1}. Please choose another table.").format(
                    doc.restaurant_table, formatted_res_time
                )
            )

    invoice_exist = frappe.db.exists(
        "POS Invoice",
        {
            "restaurant_table": doc.restaurant_table,
            "docstatus": 0,
            "invoice_printed": 0,
        },
    )
    if invoice_exist:
        frappe.throw(
            ("Table {0} has an existing invoice").format(doc.restaurant_table)
        )

def validate_table_reservation_lock(doc, method):
    if not doc.restaurant_table or (doc.order_type or "Dine In") != "Dine In":
        return

    # Check on new document or if table was changed
    if doc.is_new() or doc.has_value_changed("restaurant_table"):
        from ury.ury.api.table_reservation import check_table_reservation, parse_to_datetime
        active_res = check_table_reservation(doc.restaurant_table)
        if active_res and active_res.get("is_lock_window_active") and active_res.get("status") == "Confirmed":
            res_time_raw = active_res.get("reserved_at")
            formatted_res_time = ""
            if res_time_raw:
                try:
                    res_dt = parse_to_datetime(res_time_raw)
                    formatted_res_time = res_dt.strftime("%I:%M %p").lstrip("0")
                except Exception:
                    formatted_res_time = str(res_time_raw)
            frappe.throw(
                frappe._("Table {0} is reserved for {1}. Please choose another table.").format(
                    doc.restaurant_table, formatted_res_time
                )
            )

def sync_merged_invoice(doc):
    if getattr(frappe.flags, "in_bill_merge_sync", False):
        return

    linked_invoice = doc.custom_merged_pos_invoice
    if not linked_invoice:
        return

    frappe.flags.in_bill_merge_sync = True

    try:
        if not frappe.db.exists("POS Invoice", linked_invoice):
            return

        target = frappe.get_doc("POS Invoice", linked_invoice)

        # A partner that already posted has nothing left to mirror.
        if target.docstatus != 0:
            return

        # sync invoice printed
        target.invoice_printed = doc.invoice_printed

        # The partner keeps the tenders make_invoice allocated and saved on it; the
        # primary's tender rows are the primary's, never copied onto the partner.

        # sync submit/payment status
        if doc.docstatus == 1:

            target.flags.ignore_validate_update_after_submit = True
            target.save(ignore_permissions=True)
            target.submit()

        else:
            target.save(ignore_permissions=True)

        frappe.publish_realtime(
            "pos_invoice_updated",
            {
                "name": target.name,
                "docstatus": target.docstatus,
                "paid_amount": target.paid_amount,
                "invoice_printed": target.invoice_printed
            }
        )

    except Exception:
        # One guest bill posts both halves or neither: a partner that cannot post stops
        # the primary posting.
        if doc.docstatus == 1:
            raise
        frappe.log_error(
            frappe.get_traceback(),
            "Merged Invoice Sync Failed"
        )
        # Don't re-raise a draft save: this is an on_update hook, and other
        # linked-record sync hooks in this module (e.g.
        # release_merged_tables) deliberately swallow failures so a
        # secondary sync problem can't block the primary POS Invoice's own
        # draft save. Still make sure operators are notified, since the
        # success publish_realtime above never fires on this path.
        #
        # linked_invoice is assigned before this try block starts, so it is
        # always bound here. The publish_realtime call is still wrapped in
        # its own try/except so a failure in this visibility notification
        # itself (e.g. the realtime transport being unavailable) can never
        # raise out of this except handler and mask the original error.
        try:
            frappe.publish_realtime(
                "pos_invoice_sync_failed",
                {
                    "name": doc.name,
                    "linked_invoice": linked_invoice,
                }
            )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                "Merged Invoice Sync Failure Notification Failed"
            )

    finally:
        frappe.flags.in_bill_merge_sync = False


def on_update(doc, method):
    sync_merged_invoice(doc)


def on_submit(doc, method):
    sync_merged_invoice(doc)
    release_merged_tables(doc)

def release_merged_tables(doc):

    invoices = [doc]

    if doc.custom_merged_pos_invoice:
        try:
            invoices.append(
                frappe.get_doc(
                    "POS Invoice",
                    doc.custom_merged_pos_invoice
                )
            )
        except Exception:
            pass

    for invoice in invoices:

        # only release dine in tables
        if invoice.order_type != "Dine In":
            continue

        if not invoice.restaurant_table:
            continue

        frappe.db.set_value(
            "URY Table",
            invoice.restaurant_table,
            {
                "occupied": 0,
                "latest_invoice_time": None,
            },
            update_modified=False,
        )