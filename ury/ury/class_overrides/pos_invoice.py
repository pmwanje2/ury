from html import escape

import frappe
from erpnext.accounts.doctype.pos_invoice.pos_invoice import (
	POSInvoice,
	get_stock_availability,
)
from frappe import _
from frappe.utils import cint, flt

try:
	from erpnext.accounts.doctype.pos_invoice.pos_invoice import ProductBundleStockValidationError
except ImportError:
	class ProductBundleStockValidationError(frappe.ValidationError):
		pass

# ERPNext v16+ refactored stock validation into batched queries
try:
	from erpnext.accounts.doctype.pos_invoice.pos_invoice import (
		get_active_bundle_items,
		get_available_stock_qty,
		get_required_stock_qty,
		get_stock_item_details,
		is_stock_check_needed,
	)
	HAS_BATCHED_STOCK_CHECK = True
except ImportError:
	HAS_BATCHED_STOCK_CHECK = False

try:
	from erpnext.accounts.doctype.pos_invoice.pos_invoice import (
		get_product_bundle_stock_availability,
	)
except ImportError:
	def get_product_bundle_stock_availability(item_code, warehouse, item_qty):
		try:
			from erpnext.accounts.doctype.pos_invoice.pos_invoice import (
				get_bin_qty,
				get_pos_reserved_qty,
			)
			from erpnext.stock.stock_ledger import is_negative_stock_allowed

			is_stock_item = True
			bundle = frappe.get_doc("Product Bundle", item_code)
			availabilities = []
			for bundle_item in bundle.items:
				if frappe.get_value("Item", bundle_item.item_code, "is_stock_item"):
					bin_qty = get_bin_qty(bundle_item.item_code, warehouse)
					reserved_qty = get_pos_reserved_qty(bundle_item.item_code, warehouse)
					available = bin_qty - reserved_qty
					availabilities.append(
						{
							"item_code": bundle_item.item_code,
							"required": bundle_item.qty * item_qty,
							"available": available,
						}
					)

			return availabilities, is_stock_item, is_negative_stock_allowed(item_code=item_code)
		except Exception:
			return get_stock_availability(item_code, warehouse)


# A serialized printview document cannot supply this process-local identity.
_COPY_RENDER_MARKER = object()


class CustomPOSInvoice(POSInvoice):
	def calculate_taxes_and_totals(self):
		from ury.ury.hooks.ury_pos_tender import calculate_taxes_and_totals_excluding_tip
		from ury.ury_pos.pricing_actions import calculate_taxes_and_totals_for_service_charge

		base_calculate = super().calculate_taxes_and_totals
		return calculate_taxes_and_totals_for_service_charge(
			self,
			lambda: calculate_taxes_and_totals_excluding_tip(self, base_calculate),
		)

	def before_print(self, settings=None):
		from ury.ury.doctype.ury_order.ury_order import check_split_target_access
		from ury.ury.print_format.defaults import validate_pos_invoice_print_format

		# Enforce Print Format.disabled (closing Frappe v16 beta bypass) and block
		# XSS-vulnerable standard formats on hospitality POS Invoices without frame walking.
		form_dict = getattr(frappe, "form_dict", None)
		requested_fmt = None
		if isinstance(form_dict, dict):
			requested_fmt = form_dict.get("format") or form_dict.get("print_format")
		validate_pos_invoice_print_format(self, requested_fmt)

		is_internal_copy = getattr(self, "_ury_copy_render", None) is _COPY_RENDER_MARKER
		if is_internal_copy:
			self.copy_number = cint(getattr(self, "copy_number", None)) or 1
		else:
			# Never trust client-supplied copy_timestamp or non-integer copy_number from serialized doc payloads.
			self.copy_timestamp = None

		# Frappe printview also accepts a serialized document. Authorize the saved check.
		if self.name and frappe.db.exists("POS Invoice", self.name):
			permission = "read" if is_internal_copy else "write"
			saved = frappe.get_doc("POS Invoice", self.name)
			check_split_target_access(saved, permission)
			if saved.get("custom_merged_pos_invoice") and saved.get("custom_merged_pos_invoice") == self.get("custom_merged_pos_invoice"):
				partner = frappe.get_doc("POS Invoice", saved.custom_merged_pos_invoice, check_permission="read")
				check_split_target_access(partner, "read")
				self.ury_print_partner = partner
			if not is_internal_copy and saved.docstatus == 1:
				logged = cint(
					frappe.db.get_value(
						"URY Bill Reprint Log",
						{"invoice": self.name},
						"copy_number",
						order_by="copy_number desc",
					)
				)
				requested = cint((form_dict if isinstance(form_dict, dict) else {}).get("copy_number"))
				if logged > 0 and 1 <= requested <= logged:
					self.copy_number = requested
				else:
					self.copy_number = logged or 1
		elif not is_internal_copy:
			self.copy_number = cint(getattr(self, "copy_number", None)) or 1
		super().before_print(settings)
		if flt(getattr(self, "outstanding_amount", 0)) > 0:
			# The native POS receipt renders terms. Add the balance to this print-only copy.
			line = _("Charged to account {0}: {1} {2}").format(self.customer, self.outstanding_amount, self.currency)
			self.terms = (self.terms or "") + "<p>" + escape(line) + "</p>"

	def check_phone_payments(self):
		"""Validate Phone payment rows against the sum of live confirmed Payment Requests.

		ERPNext's stock check_phone_payments() uses get_value(..., status='Paid'), which
		both skips validation when no Paid PR exists and breaks when a grown check has
		two Paid Payment Requests or a partially refunded Payment Request.
		"""
		from ury.ury.hooks.ury_pos_invoice import validate_confirmed_phone_payments

		validate_confirmed_phone_payments(self, "on_submit")

	def clear_unallocated_mode_of_payments(self):
		"""Preserve the 0-amount local change-account payment row when change_amount > 0.

		Stock POSInvoice.clear_unallocated_mode_of_payments() deletes every amount=0 row
		on submit. When an invoice is paid 100% in a foreign currency tender (whose
		default_account is FX Till Clearing, not account_for_change_amount), deleting the
		0-amount local Cash row causes POSClosingEntry.get_change_payment_row() to fall
		back to the first Cash-type row (the foreign Cash mode!) and wrongly deduct local
		change from the foreign currency tender.
		"""
		change_account = self.get("account_for_change_amount")
		if not self.get("is_return") and flt(self.get("change_amount")) > 0 and change_account:
			kept = [
				row
				for row in (self.get("payments") or [])
				if row.get("amount") not in (0, None, "") or row.get("account") == change_account
			]
			self.set("payments", kept)
			sip = frappe.qb.DocType("Sales Invoice Payment")
			(
				frappe.qb.from_(sip)
				.delete()
				.where(sip.parent == self.name)
				.where(sip.amount == 0)
				.where((sip.account.isnull()) | (sip.account != change_account))
				.run()
			)
			return
		super().clear_unallocated_mode_of_payments()

	def validate_stock_availablility(self):
		if self.is_return or frappe.get_cached_doc("Stock Settings").allow_negative_stock:
			return

		if self.docstatus.is_draft() and not frappe.db.get_value(
			"POS Profile", self.pos_profile, "validate_stock_on_save"
		):
			return

		branch = self.branch or frappe.db.get_value("POS Profile", self.pos_profile, "branch")

		if HAS_BATCHED_STOCK_CHECK:
			rows = [
				d
				for d in self.get("items")
				if d.item_code
				and not d.serial_and_batch_bundle
				and not (branch and self._is_always_available(d.item_code, branch))
			]
			if not rows:
				return

			bundle_items = get_active_bundle_items({d.item_code for d in rows})
			required_qty = get_required_stock_qty(rows, bundle_items)
			available_qty, reserved_stock_keys = get_available_stock_qty(required_qty)
			items = get_stock_item_details({item_code for item_code, warehouse in required_qty})
			keys_to_check = {
				key
				for key in required_qty
				if is_stock_check_needed(items.get(key[0]), key in reserved_stock_keys)
			}

			for d in rows:
				if d.item_code in bundle_items:
					self.validate_bundle_stock(
						d, bundle_items[d.item_code], required_qty, available_qty, keys_to_check
					)
				elif (d.item_code, d.warehouse) in keys_to_check:
					self.validate_item_stock(d, required_qty, available_qty)
			return

		# Fallback stock validation for older ERPNext versions
		for d in self.get("items"):
			if d.serial_and_batch_bundle:
				continue

			if branch and self._is_always_available(d.item_code, branch):
				continue

			if frappe.db.exists("Product Bundle", d.item_code):
				(
					availability,
					is_stock_item,
					is_negative_stock_allowed,
				) = get_product_bundle_stock_availability(d.item_code, d.warehouse, d.stock_qty)

			else:
				availability, is_stock_item, is_negative_stock_allowed = get_stock_availability(
					d.item_code, d.warehouse
				)

			if is_negative_stock_allowed:
				continue

			if isinstance(availability, list):
				error_msgs = []
				for item in availability:
					if flt(item["available"]) < flt(item["required"]):
						error_msgs.append(
							_("<li>Packed Item {0}: Required {1}, Available {2}</li>").format(
								frappe.bold(item["item_code"]),
								frappe.bold(flt(item["required"], 2)),
								frappe.bold(flt(item["available"], 2)),
							)
						)

				if error_msgs:
					frappe.throw(
						_(
							"<b>Row #{0}:</b> Bundle {1} in warehouse {2} has insufficient packed items:<br><div style='margin-top: 15px;'><ul style='line-height: 0.8;'>{3}</ul></div>"
						).format(
							d.idx,
							frappe.bold(d.item_code),
							frappe.bold(d.warehouse),
							"<br>".join(error_msgs),
						),
						title=_("Insufficient Stock for Product Bundle Items"),
						exc=ProductBundleStockValidationError,
					)

			else:
				item_code, warehouse = frappe.bold(d.item_code), frappe.bold(d.warehouse)
				if is_stock_item and flt(availability) <= 0:
					frappe.throw(
						_("Row #{0}: Item {1} has no stock in warehouse {2}.").format(
							d.idx, item_code, warehouse
						),
						title=_("Item Out of Stock"),
					)
				elif flt(availability) < flt(d.stock_qty):
					frappe.throw(
						_(
							"Row #{0}: Item {1} in warehouse {2}: Available {3}, Needed {4}."
						).format(
							d.idx,
							item_code,
							warehouse,
							frappe.bold(flt(availability)),
							frappe.bold(d.stock_qty),
						),
						title=_("Insufficient Stock"),
					)

	@staticmethod
	def _is_always_available(item_code, branch):
		return frappe.db.exists(
			"URY Item Production Configuration",
			{
				"item": item_code,
				"branch": branch,
				"availability_mode": "Always Available",
				"active": 1,
			},
		)

