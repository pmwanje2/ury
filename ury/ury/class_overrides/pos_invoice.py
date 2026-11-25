from contextlib import nullcontext
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
	def validate(self):
		# G41 coexistence with sales-time-pricing@1:
		# `Sales Time Pricing - POS Invoice` (Before Validate Server Script) runs AFTER
		# `ury_pos_invoice.before_validate` and overwrites `custom_transaction_local_time` /
		# `custom_transaction_weekday` with site `now_datetime()`. Re-apply the server-only
		# outlet-timezone stamp captured on `self.flags` right BEFORE `super().validate()`
		# evaluates ERPNext's `apply_pricing_rule()` so Pricing Rules ALWAYS evaluate against
		# the outlet's true local time (or verified offline capture window).
		flags = getattr(self, "flags", None)
		if flags is not None and not cint(self.get("is_return")):
			auth_time = getattr(flags, "_ury_auth_local_time", None)
			auth_wd = getattr(flags, "_ury_auth_weekday", None)
			if isinstance(auth_time, str) and auth_time.strip():
				self.custom_transaction_local_time = auth_time.strip()
			if isinstance(auth_wd, str) and auth_wd.strip():
				self.custom_transaction_weekday = auth_wd.strip()
			if hasattr(flags, "_ury_auth_serving_period"):
				auth_period = getattr(flags, "_ury_auth_serving_period", None)
				self.custom_serving_period = auth_period or None

		orig_spt = cint(self.get("set_posting_time"))
		temp_offline_spt = False
		if (
			flags is not None
			and getattr(flags, "ury_trusted_offline_capture", False)
			and not cint(self.get("is_return"))
		):
			offline_dt = getattr(flags, "_ury_offline_posting_dt", None)
			if offline_dt is not None:
				if not hasattr(offline_dt, "date"):
					try:
						offline_dt = frappe.utils.get_datetime(offline_dt)
					except Exception:
						offline_dt = None
				if offline_dt is not None and hasattr(offline_dt, "date"):
					self.posting_date = offline_dt.date()
					self.posting_time = offline_dt.time()
					if not orig_spt:
						self.set_posting_time = 1
						temp_offline_spt = True

		# Coexistence with sales-time-pricing@1 on same-second returns:
		# `Sales Time Pricing - POS Invoice` (Before Validate) already enforced SALES-TIME-005
		# and stamped whole-second `HH:MM:SS` (`.000000` microseconds) when `set_posting_time == 0`.
		# Meanwhile the original submitted POS Invoice stores microsecond precision from ERPNext's
		# `validate_posting_time()`, which runs AFTER `super(SalesInvoice, self).validate()` ->
		# `validate_return_against()`. Restore current server `now_datetime()` microsecond precision
		# right before `super().validate()` ONLY when `set_posting_time == 0` (never weakening
		# SALES-TIME-005 or altering explicit privileged posting times).
		if (
			cint(self.get("is_return")) == 1
			and not cint(self.get("set_posting_time"))
			and not getattr(frappe.flags, "in_import", False)
			and not getattr(flags, "from_restore", False)
		):
			now_dt = frappe.utils.now_datetime()
			if hasattr(now_dt, "strftime"):
				self.posting_date = now_dt.strftime("%Y-%m-%d")
				self.posting_time = now_dt.strftime("%H:%M:%S.%f")

		# Shield server-verified or persisted #152 overridden/open/comp rows from
		# ERPNext's apply_pricing_rule() re-applying sales-time-pricing@1 discounts.
		# Never shield unverified client-forged rows.
		protected_rows = self._snapshot_authorized_pricing_rows()
		try:
			res = super().validate()
			if protected_rows:
				self._restore_authorized_pricing_rows(protected_rows)
			return res
		finally:
			if temp_offline_spt:
				self.set_posting_time = 0

	def _snapshot_authorized_pricing_rows(self):
		if cint(self.get("is_return")):
			return {}
		flags = getattr(self, "flags", None)
		privileged_verified = bool(getattr(flags, "ury_privileged_action_verified", False))
		persisted_auth_names = set()
		if not privileged_verified:
			is_new_fn = getattr(self, "is_new", None)
			is_new = bool(is_new_fn()) if callable(is_new_fn) else not bool(self.get("name"))
			if not is_new:
				prev = None
				get_before = getattr(self, "get_doc_before_save", None)
				if callable(get_before):
					try:
						prev = get_before()
					except Exception:
						prev = None
				prev_items = (
					(prev.get("items") if hasattr(prev, "get") else getattr(prev, "items", None))
					if prev is not None
					else None
				)
				if prev_items is None and self.get("name"):
					try:
						prev_items = frappe.get_all(
							"POS Invoice Item",
							filters={"parent": self.get("name")},
							fields=[
								"name",
								"custom_price_overridden",
								"custom_is_open_item",
								"custom_is_complimentary",
							],
						)
					except Exception:
						prev_items = None
				for prow in prev_items or []:
					pname = prow.get("name") if hasattr(prow, "get") else getattr(prow, "name", None)
					if not pname:
						continue
					if (
						cint(prow.get("custom_price_overridden") if hasattr(prow, "get") else getattr(prow, "custom_price_overridden", 0)) == 1
						or cint(prow.get("custom_is_open_item") if hasattr(prow, "get") else getattr(prow, "custom_is_open_item", 0)) == 1
						or cint(prow.get("custom_is_complimentary") if hasattr(prow, "get") else getattr(prow, "custom_is_complimentary", 0)) == 1
					):
						persisted_auth_names.add(pname)

		snaps = {}
		for row in self.get("items") or []:
			if cint(row.get("is_disposable") if hasattr(row, "get") else getattr(row, "is_disposable", 0)) == 1:
				continue
			has_flag = (
				cint(row.get("custom_price_overridden") if hasattr(row, "get") else getattr(row, "custom_price_overridden", 0)) == 1
				or cint(row.get("custom_is_open_item") if hasattr(row, "get") else getattr(row, "custom_is_open_item", 0)) == 1
				or cint(row.get("custom_is_complimentary") if hasattr(row, "get") else getattr(row, "custom_is_complimentary", 0)) == 1
			)
			if not has_flag:
				continue
			rname = row.get("name") if hasattr(row, "get") else getattr(row, "name", None)
			if not privileged_verified and (not rname or rname not in persisted_auth_names):
				continue
			snaps[id(row)] = {
				"name": rname,
				"price_list_rate": flt(row.get("price_list_rate") if hasattr(row, "get") else getattr(row, "price_list_rate", 0)),
				"rate": flt(row.get("rate") if hasattr(row, "get") else getattr(row, "rate", 0)),
				"base_price_list_rate": flt(row.get("base_price_list_rate") if hasattr(row, "get") else getattr(row, "base_price_list_rate", 0)),
				"base_rate": flt(row.get("base_rate") if hasattr(row, "get") else getattr(row, "base_rate", 0)),
				"discount_percentage": flt(row.get("discount_percentage") if hasattr(row, "get") else getattr(row, "discount_percentage", 0)),
				"discount_amount": flt(row.get("discount_amount") if hasattr(row, "get") else getattr(row, "discount_amount", 0)),
				"pricing_rules": row.get("pricing_rules") if hasattr(row, "get") else getattr(row, "pricing_rules", None),
			}
		return snaps

	def _restore_authorized_pricing_rows(self, snaps):
		restored_names = set()
		changed = False
		for row in self.get("items") or []:
			snap = snaps.get(id(row))
			if snap is None:
				continue
			curr_rate = flt(row.get("rate") if hasattr(row, "get") else getattr(row, "rate", 0))
			curr_plr = flt(row.get("price_list_rate") if hasattr(row, "get") else getattr(row, "price_list_rate", 0))
			curr_disc_pct = flt(row.get("discount_percentage") if hasattr(row, "get") else getattr(row, "discount_percentage", 0))
			curr_disc_amt = flt(row.get("discount_amount") if hasattr(row, "get") else getattr(row, "discount_amount", 0))
			curr_pr = (row.get("pricing_rules") if hasattr(row, "get") else getattr(row, "pricing_rules", None)) or ""
			snap_pr = snap["pricing_rules"] or ""
			if (
				abs(curr_rate - snap["rate"]) > 1e-6
				or abs(curr_plr - snap["price_list_rate"]) > 1e-6
				or abs(curr_disc_pct - snap["discount_percentage"]) > 1e-6
				or abs(curr_disc_amt - snap["discount_amount"]) > 1e-6
				or curr_pr != snap_pr
			):
				for field in (
					"price_list_rate",
					"rate",
					"base_price_list_rate",
					"base_rate",
					"discount_percentage",
					"discount_amount",
					"pricing_rules",
				):
					setattr(row, field, snap[field])
				rname = (row.get("name") if hasattr(row, "get") else getattr(row, "name", None)) or snap["name"]
				if rname:
					restored_names.add(rname)
				changed = True
		if changed:
			pr_rows = self.get("pricing_rules")
			if isinstance(pr_rows, list) and restored_names:
				kept = [
					pr
					for pr in pr_rows
					if (pr.get("child_docname") if hasattr(pr, "get") else getattr(pr, "child_docname", None))
					not in restored_names
				]
				if callable(getattr(self, "set", None)):
					self.set("pricing_rules", kept)
				else:
					self.pricing_rules = kept
			if callable(getattr(self, "calculate_taxes_and_totals", None)):
				self.calculate_taxes_and_totals()

	def set_taxes(self):
		if getattr(self.flags, "ury_explicit_no_taxes", False):
			return
		return super().set_taxes()

	def set_missing_values(self, for_validate=False):
		protected_rows = self._snapshot_authorized_pricing_rows()
		res = super().set_missing_values(for_validate=for_validate)
		if protected_rows:
			self._restore_authorized_pricing_rows(protected_rows)
		return res

	def set_pos_fields(self, for_validate=False):
		# Open-check rate stability (G40): ERPNext's set_pos_fields() unconditionally resets
		# selling_price_list to POS Profile.selling_price_list on every validate(). Preserve ONLY
		# a server-authoritative price list (set by before_validate/sync_order/change_order_type)
		# or a persisted open check's stamped Serving Period price list — never an unvalidated
		# client payload on a new REST POST/PUT.
		preserved_pl = None
		if (self.get("order_type") or "") != "Aggregators":
			flags = getattr(self, "flags", None)
			auth_pl = (getattr(flags, "ury_authoritative_price_list", None) or "").strip() if flags else ""
			if auth_pl:
				preserved_pl = auth_pl
			elif not (callable(getattr(self, "is_new", None)) and self.is_new()):
				get_before = getattr(self, "get_doc_before_save", None)
				prev = get_before() if callable(get_before) else None
				prev_period = (
					(prev.get("custom_serving_period") if hasattr(prev, "get") else getattr(prev, "custom_serving_period", None))
					or ""
				).strip() if prev is not None else ""
				prev_pl = (
					(prev.get("selling_price_list") if hasattr(prev, "get") else getattr(prev, "selling_price_list", None))
					or ""
				).strip() if prev is not None else ""
				if prev_period and prev_pl:
					preserved_pl = prev_pl
		res = super().set_pos_fields(for_validate=for_validate)
		if preserved_pl:
			self.selling_price_list = preserved_pl
		return res

	def set_qty_as_per_stock_uom(self):
		from ury.ury_pos.split_balances import get_saved_split_balance, restore_split_stock

		super().set_qty_as_per_stock_uom()
		restore_split_stock(self, get_saved_split_balance(self))

	def calculate_taxes_and_totals(self):
		from ury.ury.hooks.ury_pos_tender import calculate_taxes_and_totals_excluding_tip
		from ury.ury_pos.pricing_actions import calculate_taxes_and_totals_for_service_charge

		from ury.ury_pos.discount import with_currency_precision
		from ury.ury_pos.split_balances import _repricing_split_discount, get_saved_split_balance, restore_split_balance

		base_calculate = super().calculate_taxes_and_totals
		snapshot = get_saved_split_balance(self)
		if _repricing_split_discount(self) and self.flags.ury_split_tax_scopes:
			from ury.ury_pos.scoped_split_taxes import calculate_scoped_split_taxes

			base_calculate = lambda: calculate_scoped_split_taxes(self)
		# Repricing and later saves must use the same minor unit as the split.
		with with_currency_precision(self) if snapshot else nullcontext():
			result = calculate_taxes_and_totals_for_service_charge(
				self,
				lambda: calculate_taxes_and_totals_excluding_tip(self, base_calculate),
			)
			restore_split_balance(self, snapshot)
		return result

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
			from ury.ury.api.ury_bundle_resolver import parse_bundle_overrides

			effective_bundles_by_row = {}
			has_any_bundle_overrides = False
			for d in rows:
				if d.item_code not in bundle_items:
					continue
				raw_ov = d.get("custom_bundle_overrides_json")
				if not raw_ov:
					effective_bundles_by_row[id(d)] = bundle_items[d.item_code]
					continue
				parsed = parse_bundle_overrides(raw_ov)
				if not parsed:
					effective_bundles_by_row[id(d)] = bundle_items[d.item_code]
					continue
				has_any_bundle_overrides = True
				slot_queue = {}
				for ov in parsed:
					if ov["slot_item"]:
						slot_queue.setdefault(ov["slot_item"], []).append(ov)
				swapped = []
				for bi in bundle_items[d.item_code]:
					bi_code = bi.get("item_code") if isinstance(bi, dict) else getattr(bi, "item_code", None)
					bi_qty = bi.get("qty") if isinstance(bi, dict) else getattr(bi, "qty", 1)
					slot_list = slot_queue.get(bi_code)
					if slot_list:
						ov = slot_list.pop(0)
						swapped.append(frappe._dict(item_code=ov["chosen_item"], qty=flt(ov.get("qty") or bi_qty or 1)))
					else:
						swapped.append(bi)
				effective_bundles_by_row[id(d)] = swapped

			if has_any_bundle_overrides:
				required_qty = {}
				for d in rows:
					if d.item_code in bundle_items:
						for bi in effective_bundles_by_row.get(id(d), bundle_items[d.item_code]):
							bi_code = bi.get("item_code") if isinstance(bi, dict) else getattr(bi, "item_code", None)
							bi_qty = flt(bi.get("qty") if isinstance(bi, dict) else getattr(bi, "qty", 1))
							key = (bi_code, d.warehouse)
							required_qty[key] = required_qty.get(key, 0.0) + flt(d.stock_qty) * bi_qty
					else:
						key = (d.item_code, d.warehouse)
						required_qty[key] = required_qty.get(key, 0.0) + flt(d.stock_qty)
			else:
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
						d, effective_bundles_by_row.get(id(d), bundle_items[d.item_code]), required_qty, available_qty, keys_to_check
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

