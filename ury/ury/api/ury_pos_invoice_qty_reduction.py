"""Cashier/captain-facing quantity-reduction API for a printed POS Invoice.

`ury.ury.hooks.ury_pos_invoice.validate_invoice` blocks any qty reduction or
item removal on a POS Invoice once `invoice_printed == 1`, unless the POS
Profile's `remove_items` flag is set -- see that function for the existing
blanket-block behaviour and its own generic "Cannot modify items after
invoice is printed" error. This module adds a SEPARATE, deliberate, item-
level qty-reduction path that:

  1. Is gated by Order Type, not by `remove_items`. Reduction is only
     permitted when the invoice's `order_type` (an existing Select field on
     POS Invoice; see fixtures/custom_field.json "POS Invoice-order_type")
     appears in the POS Profile's new
     `custom_qty_reduction_allowed_order_types` field (a comma-separated
     list of the same Select's options -- see
     fixtures/custom_field.json "POS Profile-custom_qty_reduction_allowed_
     order_types"). An order type NOT in that list is rejected with a
     specific `ORDER_TYPE_NOT_ALLOWED` error naming the order type --
     deliberately NOT the generic "Cannot modify items after invoice is
     printed" message `validate_invoice` raises for everything else. An
     unconfigured (empty) allow-list means NO order type is permitted --
     fail closed, matching this app's cancellation-service style (see
     ury_kot_cancellation_service.py's ITEM_LEVEL_STATE_REQUIRED case).

  2. Bypasses `validate_invoice`'s guard for exactly this one legitimate
     write, via a `frappe.flags.ury_qty_reduction` flag -- the identical
     pattern `ury_bill_split` already uses in the same function. This module
     never weakens or removes that guard for any other caller; see
     `ury_pos_invoice.validate_invoice`'s own diff.

  3. Reuses the existing cancel-KOT creation path
     (`ury_kot_generate.process_items_for_cancel_kot` ->
     `create_cancel_kot_doc`) to generate a partial ("Partially cancelled")
     cancel-KOT for exactly the reduced delta -- it does not reimplement
     cancel-KOT creation, and it does not touch `URY KOT Execution` /
     `cancel_partial` in ury_kot_cancellation_service.py at all; that
     KOT-execution-level state machine is deliberately untouched by this
     item-level qty-reduction path.

Generic by design: this is a single whitelisted API used by BOTH cashier
(Register) and captain (order UI) frontends -- there is no cashier-specific
or captain-specific backend branch here. Frontend wiring is out of scope for
this module.
"""

import inspect
import frappe
from frappe import _
from frappe.utils import cint, flt

from ury.ury.api.ury_kot_generate import (
	_line_keyed_items,
	process_items_for_cancel_kot,
)
from ury.ury_pos.approval_currency import company_approval_amount
from ury.ury_pos.void_return_reopen import (
	CODE_OUTLET_MISMATCH,
	CODE_VOID_CLOSED_USE_RETURN,
	CODE_VOID_PARTIAL_PAYMENT,
	post_waste_stock_entry,
	received_tender_total,
	require_pos_override,
	resolve_pos_reason,
)


# Reason codes, following ury_kot_cancellation_service.py's stable-reason-code style.
INVOICE_NOT_FOUND = "INVOICE_NOT_FOUND"
ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
INVALID_QTY = "INVALID_QTY"
ORDER_TYPE_NOT_ALLOWED = "ORDER_TYPE_NOT_ALLOWED"
KOT_NAMING_SERIES_MISSING = "KOT_NAMING_SERIES_MISSING"
NOT_PERMITTED = "NOT_PERMITTED"
LAST_ITEM_CANNOT_BE_REMOVED = "LAST_ITEM_CANNOT_BE_REMOVED"


class QtyReductionError(frappe.ValidationError):
	"""Raised for fail-closed qty-reduction errors; carries a stable reason_code."""

	def __init__(self, reason_code, message=None):
		self.reason_code = reason_code
		super().__init__(message or reason_code)


def _allowed_order_types(pos_profile_doc):
	raw = (pos_profile_doc.get("custom_qty_reduction_allowed_order_types") or "").strip()
	if not raw:
		return set()
	return {part.strip() for part in raw.split(",") if part.strip()}


@frappe.whitelist()
def reduce_order_item_qty(
	invoice_id,
	item_row_name,
	new_qty,
	item_code=None,
	reason=None,
	note=None,
	is_waste=0,
	approval_token=None,
):
	"""Reduce (or fully remove, `new_qty=0`) one item's qty on a printed POS
	Invoice, for an Order Type the POS Profile has explicitly allowed, and
	generate the matching partial cancel-KOT for the delta.

	Sent items require an active reason and either `may_perform` or a manager
	PIN `approval_token` (`void_sent_item` for removal to 0, `reduce_sent_qty`
	for partial reduction). Unsent (`ury_held == 1`) items stay free (Simphony
	error correct). When the reason is waste, posts a native ERPNext Stock Entry
	(Material Issue / Wastage) and links it to the cancel KOT.
	"""
	actor = frappe.session.user

	if not invoice_id or not frappe.db.exists("POS Invoice", invoice_id):
		raise QtyReductionError(INVOICE_NOT_FOUND, _("POS Invoice {0} not found").format(invoice_id))

	pos_invoice = frappe.get_doc("POS Invoice", invoice_id, for_update=True)

	if cint(pos_invoice.get("docstatus")) == 1:
		raise QtyReductionError(
			CODE_VOID_CLOSED_USE_RETURN,
			_("Closed checks cannot be modified here. Process a return instead. [{0}]").format(
				CODE_VOID_CLOSED_USE_RETURN
			),
		)

	if not frappe.has_permission("POS Invoice", "write", doc=pos_invoice, user=actor):
		raise QtyReductionError(
			NOT_PERMITTED, _("Not permitted to modify invoice {0}").format(invoice_id)
		)

	# Outlet/branch scoping: never modify a check belonging to another outlet.
	inv_branch = pos_invoice.get("branch")
	if actor != "Administrator" and isinstance(inv_branch, str) and inv_branch:
		from ury.ury_pos.api import getBranch

		user_branch = getBranch()
		if isinstance(user_branch, str) and user_branch and user_branch != inv_branch:
			frappe.throw(
				_("Not permitted to modify a check from another outlet. [{0}]").format(CODE_OUTLET_MISMATCH),
				frappe.PermissionError,
			)

	order_type = pos_invoice.get("order_type")
	pos_profile = frappe.get_doc("POS Profile", pos_invoice.pos_profile)

	raw_clock_in_req = pos_profile.get("custom_require_clock_in_to_order")
	if isinstance(raw_clock_in_req, (int, float, str)) and cint(raw_clock_in_req) == 1:
		from ury.ury_pos import timeclock

		timeclock.enforce_clock_in_for_profile(pos_profile, user=actor)

	allowed_order_types = _allowed_order_types(pos_profile)

	if order_type not in allowed_order_types:
		raise QtyReductionError(
			ORDER_TYPE_NOT_ALLOWED,
			_(
				"Quantity reduction after printing is not permitted for order "
				"type {0} on POS Profile {1}. Allowed order types: {2}."
			).format(
				order_type or _("(not set)"),
				pos_profile.name,
				", ".join(sorted(allowed_order_types)) or _("(none configured)"),
			),
		)

	item_row = None
	for row in pos_invoice.items:
		if row.name == item_row_name:
			item_row = row
			break
	if item_row is None:
		raise QtyReductionError(
			ITEM_NOT_FOUND,
			_("Item row {0} not found on invoice {1}").format(item_row_name, invoice_id),
		)

	if item_code and item_row.item_code != item_code:
		raise QtyReductionError(
			ITEM_NOT_FOUND,
			_(
				"Item row {0} on invoice {1} is item {2}, not the expected {3}"
			).format(item_row_name, invoice_id, item_row.item_code, item_code),
		)

	item_code = item_row.item_code
	current_qty = item_row.qty

	try:
		new_qty = float(new_qty)
	except (TypeError, ValueError):
		raise QtyReductionError(INVALID_QTY, _("new_qty must be numeric, got {0!r}").format(new_qty))

	if not (0 <= new_qty < current_qty):
		raise QtyReductionError(
			INVALID_QTY,
			_(
				"new_qty ({0}) must be a real reduction: 0 <= new_qty < current qty ({1})"
			).format(new_qty, current_qty),
		)

	delta = new_qty - current_qty  # negative

	if not float(new_qty).is_integer() or not float(delta).is_integer():
		raise QtyReductionError(
			INVALID_QTY,
			_(
				"new_qty ({0}) must be a whole number: fractional quantity reductions are not supported"
			).format(new_qty),
		)

	# Locate any linked child modifier / combo upcharge rows belonging to this parent line
	parent_line_key = item_row.get("reservation_line_key")
	linked_children = [
		row
		for row in pos_invoice.items
		if row.name != item_row_name
		and parent_line_key
		and cint(row.get("custom_is_modifier")) == 1
		and row.get("custom_parent_line_key") == parent_line_key
		and flt(row.qty) > 0
	]
	linked_child_names = {c.name for c in linked_children}

	other_rows_with_qty = sum(
		1
		for row in pos_invoice.items
		if row.name != item_row_name and row.name not in linked_child_names and flt(row.qty) > 0
	)
	if new_qty == 0 and other_rows_with_qty == 0:
		raise QtyReductionError(
			LAST_ITEM_CANNOT_BE_REMOVED,
			_(
				"Item {0} is the only item on invoice {1}. Cancel the whole invoice instead of "
				"removing its last item."
			).format(item_code, invoice_id),
		)

	# Compute new quantities for linked child modifiers proportionally
	child_updates = []
	for child in linked_children:
		if new_qty == 0:
			child_new_qty = 0.0
		elif flt(current_qty) > 0:
			ratio = flt(new_qty) / flt(current_qty)
			scaled = flt(child.qty) * ratio
			child_new_qty = float(round(scaled)) if abs(scaled - round(scaled)) < 1e-6 else flt(child.qty)
		else:
			child_new_qty = flt(child.qty)
		if child_new_qty < flt(child.qty):
			child_updates.append((child, flt(child.qty), child_new_qty))

	# Guard against reducing a partially paid open check below already-received tender.
	received_paid = received_tender_total(pos_invoice)
	if received_paid > 0:
		rate = flt(item_row.get("rate"))
		current_total = flt(pos_invoice.get("rounded_total") or pos_invoice.get("grand_total"))
		if current_total <= 0:
			current_total = sum(flt(r.get("qty")) * flt(r.get("rate")) for r in pos_invoice.items)
		child_reduction_amount = sum(
			flt(child.get("rate")) * (child_prev_qty - child_new_qty)
			for child, child_prev_qty, child_new_qty in child_updates
		)
		projected_total = current_total + (delta * rate) - child_reduction_amount
		if projected_total < received_paid - 1e-6:
			raise QtyReductionError(
				CODE_VOID_PARTIAL_PAYMENT,
				_(
					"Cannot reduce items below payments already received on this check. [{0}]"
				).format(CODE_VOID_PARTIAL_PAYMENT),
			)

	pos_profile_id = pos_invoice.pos_profile
	kot_naming_series = pos_profile.custom_kot_naming_series
	held = cint(item_row.get("ury_held")) == 1
	if not held and not kot_naming_series:
		raise QtyReductionError(
			KOT_NAMING_SERIES_MISSING,
			_(
				"KOT Naming Series is mandatory for cancel-KOT creation. "
				"Ensure it is configured in POS Profile: {0}"
			).format(pos_profile.name),
		)
	cancel_kot_naming_series = "CNCL-" + (kot_naming_series or "")

	# Sent items require reason + privilege/manager PIN (Simphony Opt 135 keeps unsent/held free).
	approver = None
	reason_info = None
	if not held:
		privilege = "void_sent_item" if new_qty == 0 else "reduce_sent_qty"
		fallback_privs = ("void_sent_item",) if privilege == "reduce_sent_qty" else ()
		voided_qty = flt(current_qty) - flt(new_qty)
		voided_amount = flt(item_row.get("rate") or 0) * voided_qty
		for child, child_prev_qty, child_new_qty in child_updates:
			voided_amount += flt(child.get("rate") or 0) * (child_prev_qty - child_new_qty)
		voided_amount = round(voided_amount, 4)
		reason_info = resolve_pos_reason(
			privilege,
			pos_profile.name,
			reason,
			note=note,
			is_waste=is_waste,
			fallback_privileges=fallback_privs,
		)
		claim = {
			"invoice": invoice_id,
			"item_row_name": item_row_name,
			"item_code": item_code,
			"previous_qty": float(current_qty),
			"new_qty": float(new_qty),
			"delta": float(delta),
			"reason": reason_info["code"],
			"note": reason_info["note"],
			"is_waste": 1 if reason_info["is_waste"] else 0,
		}
		b2a_compat_claim = {
			"action": "reduce_order_item_qty",
			"item_row_name": item_row_name,
			"item_code": item_code,
			"from_qty": flt(current_qty),
			"to_qty": flt(new_qty),
			"amount": voided_amount,
			"reason": (reason or "").strip(),
		}
		approver = require_pos_override(
			privilege,
			pos_profile.name,
			"POS Invoice",
			invoice_id,
			claim,
			approval_token=approval_token,
			amount=company_approval_amount(pos_invoice, voided_amount, frappe),
			doc=pos_invoice,
			fallback_privileges=fallback_privs,
			alt_claim=b2a_compat_claim,
		)

	from ury.ury.api.ury_availability import apply_count_delta

	prev_count_rows = [{"item": item_code, "qty": current_qty}] + [
		{"item": c.item_code, "qty": prev_q} for c, prev_q, _ in child_updates
	]
	next_count_rows = [{"item": item_code, "qty": new_qty}] + [
		{"item": c.item_code, "qty": next_q} for c, _, next_q in child_updates
	]
	apply_count_delta(pos_invoice.branch, prev_count_rows, next_count_rows, invoice=pos_invoice)

	reconcile_held = False
	if held:
		from ury.ury.api.ury_stock_policy import get_branch_stock_policy
		from ury.ury.api.ury_order_reservation_service import reconcile_order_reservations
		from ury.ury.doctype.ury_order.ury_order import _previous_line_snapshot

		reconcile_held = get_branch_stock_policy(pos_invoice.branch, pos_invoice.company).reservation_control_enabled
		if reconcile_held:
			previous = _previous_line_snapshot(pos_invoice.items)
	else:
		from ury.ury.doctype.ury_order.ury_order import _kitchen_snapshot

		# Use the same canonical dish/note groups and persisted keys as the original KOT.
		previous_items_snapshot = list(_line_keyed_items(_kitchen_snapshot(pos_invoice.items)).values())
		cancel_line, = _line_keyed_items(_kitchen_snapshot([item_row])).values()
		cancel_payload = [{**cancel_line, "qty": delta}]
		for child, child_prev_qty, child_new_qty in child_updates:
			if cint(child.get("ury_held")) == 1:
				continue
			child_lines = list(_line_keyed_items(_kitchen_snapshot([child])).values())
			if child_lines:
				cancel_payload.append({**child_lines[0], "qty": child_new_qty - child_prev_qty})

	# Apply the reduction (or full removal) to the invoice, bypassing
	# validate_invoice's post-print guard for exactly this legitimate write.
	frappe.flags.ury_qty_reduction = True
	try:
		item_row.qty = new_qty
		if held and new_qty == 0:
			item_row.ury_held = 0
		for child, _child_prev_qty, child_new_qty in child_updates:
			child.qty = child_new_qty
			if held and child_new_qty == 0:
				child.ury_held = 0
		if reconcile_held:
			reconcile_order_reservations(order_ref=pos_invoice.name, previous_items=previous,
				accepted_items=_previous_line_snapshot(pos_invoice.items), branch=pos_invoice.branch,
				company=pos_invoice.company, actor=actor)
		if new_qty == 0 and pos_invoice.docstatus == 0:
			# ERPNext v16 validate_qty_is_not_zero() rejects qty==0 rows on draft POS Invoices.
			for child, _child_prev_qty, child_new_qty in child_updates:
				if child_new_qty == 0:
					pos_invoice.remove(child)
			pos_invoice.remove(item_row)
		pos_invoice.save(ignore_permissions=False)
	finally:
		frappe.flags.ury_qty_reduction = False

	# Reuse the existing cancel-KOT creation path for exactly the delta, carrying void/waste reason.
	created_kot_names = []
	waste_stock_entry = None
	if not held:
		cancel_comment = reason_info["display"] if reason_info else reason
		cancel_kwargs = {}
		if reason_info and "void_reason_code" in inspect.signature(process_items_for_cancel_kot).parameters:
			cancel_kwargs["void_reason_code"] = reason_info["code"]
		created_kot_names = process_items_for_cancel_kot(
			invoice_id, pos_invoice.customer, pos_invoice.get("restaurant_table"),
			cancel_payload, cancel_comment, pos_profile_id, cancel_kot_naming_series,
			"Partially cancelled", previous_items_snapshot, **cancel_kwargs,
		)
		if reason_info and reason_info["is_waste"]:
			waste_rows = [
				{
					"item_code": item_code,
					"qty": abs(delta),
					"warehouse": item_row.get("warehouse"),
					"uom": item_row.get("uom"),
					"custom_line_role": item_row.get("custom_line_role"),
					"custom_bundle_overrides_json": item_row.get("custom_bundle_overrides_json"),
				}
			]
			for child, child_prev_qty, child_new_qty in child_updates:
				if cint(child.get("ury_held")) == 1:
					continue
				if (child.get("custom_line_role") or "") == "Combo Upcharge":
					continue
				child_delta = flt(child_prev_qty) - flt(child_new_qty)
				if child_delta > 0:
					waste_rows.append(
						{
							"item_code": child.get("item_code"),
							"qty": child_delta,
							"warehouse": child.get("warehouse"),
							"uom": child.get("uom"),
							"custom_line_role": child.get("custom_line_role"),
							"custom_bundle_overrides_json": child.get("custom_bundle_overrides_json"),
						}
					)
			waste_stock_entry = post_waste_stock_entry(
				pos_invoice,
				pos_profile,
				waste_rows,
				reason_info,
				cancel_kot_names=created_kot_names,
				approver=approver,
			)

	return {
		"invoice": invoice_id,
		"item_row_name": item_row_name,
		"item_code": item_code,
		"previous_qty": current_qty,
		"new_qty": new_qty,
		"delta": delta,
		"order_type": order_type,
		"cancel_kot_names": created_kot_names,
		"actor": actor,
		"approver": approver,
		"approved_by": approver,
		"reason": reason_info["code"] if reason_info else reason,
		"is_waste": bool(reason_info["is_waste"]) if reason_info else False,
		"waste_stock_entry": waste_stock_entry,
	}
