"""Native ERPNext Product Bundle resolver for POS-PR-8 (G4 Combo Meals).

Resolves default `Product Bundle Item` components, applies customer combo substitutions
stored in `custom_bundle_overrides_json`, and swaps `packed_items` on `POS Invoice` /
consolidated `Sales Invoice` right after ERPNext's `make_packing_list()` so Stock Ledger
Entries and COGS GL entries reflect the actual substituted items.
"""

import json

import frappe
from frappe.utils import cint, flt


def parse_bundle_overrides(raw):
	"""Parse `custom_bundle_overrides_json` into a clean list of override dicts."""
	if not raw:
		return []
	if isinstance(raw, str):
		try:
			data = json.loads(raw)
		except (TypeError, ValueError):
			return []
	elif isinstance(raw, (list, tuple)):
		data = list(raw)
	else:
		return []

	overrides = []
	for entry in data:
		if not isinstance(entry, dict):
			continue
		chosen_item = (entry.get("chosen_item") or entry.get("item") or "").strip()
		slot_item = (entry.get("slot_item") or entry.get("replaces_bundle_item") or "").strip()
		if not chosen_item:
			continue
		overrides.append(
			{
				"group": (entry.get("group") or "").strip(),
				"slot_item": slot_item,
				"chosen_item": chosen_item,
				"option_code": (entry.get("option_code") or chosen_item).strip(),
				"label": (entry.get("label") or chosen_item).strip(),
				"qty": flt(entry.get("qty") or 1),
				"inherit_parent_routing": cint(entry.get("inherit_parent_routing", 1)),
				"hide_on_receipt": cint(entry.get("hide_on_receipt", 0)),
			}
		)
	return overrides


def get_product_bundle_name(item_code):
	"""Return native ERPNext Product Bundle name for `item_code`, or None."""
	if not item_code:
		return None
	bundle = frappe.db.get_value("Product Bundle", {"new_item_code": item_code}, "name")
	if not bundle and frappe.db.exists("Product Bundle", item_code):
		bundle = item_code
	return bundle


def get_product_bundle_defaults(item_code):
	"""Return default `Product Bundle Item` rows for `item_code`."""
	bundle_name = get_product_bundle_name(item_code)
	if not bundle_name:
		return None, []
	rows = frappe.get_all(
		"Product Bundle Item",
		filters={"parent": bundle_name},
		fields=["item_code", "qty", "uom", "description"],
		order_by="idx asc",
	)
	if not rows:
		return bundle_name, []

	codes = [r.get("item_code") for r in rows if r.get("item_code")]
	names = {}
	if codes:
		for item_row in frappe.get_all(
			"Item", filters={"name": ["in", list(dict.fromkeys(codes))]}, fields=["name", "item_name", "stock_uom"]
		):
			names[item_row.get("name")] = item_row

	defaults = []
	for row in rows:
		code = row.get("item_code")
		meta = names.get(code) or {}
		defaults.append(
			{
				"item_code": code,
				"item_name": meta.get("item_name") or row.get("description") or code,
				"qty": flt(row.get("qty") or 1),
				"uom": row.get("uom") or meta.get("stock_uom") or "Nos",
				"description": row.get("description") or meta.get("item_name") or code,
			}
		)
	return bundle_name, defaults


def resolve_bundle_components(item_code, branch=None, company=None, overrides=None, qty=1):
	"""Resolve effective physical bundle components after applying choice overrides."""
	_bundle_name, defaults = get_product_bundle_defaults(item_code)
	if not defaults:
		return []

	parsed_overrides = parse_bundle_overrides(overrides)
	override_queue = {}
	extra_overrides = []
	for ov in parsed_overrides:
		if ov["slot_item"]:
			override_queue.setdefault(ov["slot_item"], []).append(ov)
		else:
			extra_overrides.append(ov)

	parent_qty = 1.0 if qty is None else flt(qty)
	resolved = []
	for default_row in defaults:
		slot_code = default_row["item_code"]
		slot_overrides = override_queue.get(slot_code)
		if slot_overrides:
			ov = slot_overrides.pop(0)
			chosen_code = ov["chosen_item"]
			comp_qty = flt(ov.get("qty") or default_row["qty"] or 1) * parent_qty
			chosen_meta = frappe.db.get_value(
				"Item", chosen_code, ["item_name", "stock_uom", "description"], as_dict=True
			) or {}
			resolved.append(
				{
					"item_code": chosen_code,
					"item_name": chosen_meta.get("item_name") or ov.get("label") or chosen_code,
					"qty": comp_qty,
					"uom": chosen_meta.get("stock_uom") or default_row["uom"] or "Nos",
					"description": chosen_meta.get("description") or chosen_meta.get("item_name") or chosen_code,
					"slot_item": slot_code,
					"was_substituted": chosen_code != slot_code,
					"group": ov.get("group") or "",
					"inherit_parent_routing": cint(ov.get("inherit_parent_routing", 1)),
				}
			)
		else:
			resolved.append(
				{
					"item_code": slot_code,
					"item_name": default_row["item_name"],
					"qty": flt(default_row["qty"] or 1) * parent_qty,
					"uom": default_row["uom"] or "Nos",
					"description": default_row["description"] or default_row["item_name"],
					"slot_item": slot_code,
					"was_substituted": False,
					"group": "",
					"inherit_parent_routing": 1,
				}
			)

	for ov in extra_overrides:
		chosen_code = ov["chosen_item"]
		chosen_meta = frappe.db.get_value(
			"Item", chosen_code, ["item_name", "stock_uom", "description"], as_dict=True
		) or {}
		resolved.append(
			{
				"item_code": chosen_code,
				"item_name": chosen_meta.get("item_name") or ov.get("label") or chosen_code,
				"qty": flt(ov.get("qty") or 1) * parent_qty,
				"uom": chosen_meta.get("stock_uom") or "Nos",
				"description": chosen_meta.get("description") or chosen_meta.get("item_name") or chosen_code,
				"slot_item": "",
				"was_substituted": True,
				"group": ov.get("group") or "",
				"inherit_parent_routing": cint(ov.get("inherit_parent_routing", 1)),
			}
		)

	return resolved


def _resolve_component_warehouse(item_code, branch, company, fallback_warehouse):
	if branch and company:
		try:
			from ury.ury.api.ury_order_reservation_service import (
				_warehouse_for_context,
				resolve_production_context,
			)

			ctx = resolve_production_context(item_code, branch, company)
			if ctx:
				wh = _warehouse_for_context(ctx)
				if wh:
					return wh
		except Exception:
			pass
	return fallback_warehouse


def _update_packed_row_from_component(packed, comp, parent_row, branch, company):
	chosen_code = comp["item_code"]
	target_qty = flt(comp.get("qty"))
	code_changed = chosen_code != packed.get("item_code")

	if code_changed:
		meta = frappe.db.get_value(
			"Item",
			chosen_code,
			["item_name", "description", "stock_uom"],
			as_dict=True,
		) or {}
		packed.item_code = chosen_code
		packed.item_name = meta.get("item_name") or comp.get("item_name") or chosen_code
		packed.description = meta.get("description") or comp.get("description") or packed.item_name
		packed.uom = meta.get("stock_uom") or comp.get("uom") or packed.get("uom") or "Nos"
		if hasattr(packed, "conversion_factor") or (isinstance(packed, dict) and "conversion_factor" in packed):
			packed.conversion_factor = 1.0

		new_wh = _resolve_component_warehouse(
			chosen_code, branch, company, packed.get("warehouse") or parent_row.get("warehouse")
		)
		if new_wh:
			packed.warehouse = new_wh

		# Refresh Bin stock & valuation rate so SLE/COGS GL uses the chosen item's real cost.
		if new_wh and frappe.db.exists("DocType", "Bin"):
			bin_info = frappe.db.get_value(
				"Bin",
				{"item_code": chosen_code, "warehouse": new_wh},
				["actual_qty", "projected_qty", "valuation_rate"],
				as_dict=True,
			)
			if isinstance(bin_info, dict):
				if bin_info.get("actual_qty") is not None:
					packed.actual_qty = flt(bin_info.get("actual_qty"))
				if bin_info.get("projected_qty") is not None:
					packed.projected_qty = flt(bin_info.get("projected_qty"))
				if bin_info.get("valuation_rate") is not None:
					packed.incoming_rate = flt(bin_info.get("valuation_rate"))

	packed.qty = target_qty


def _reconcile_packed_rows_for_parent(doc, parent_row, parsed_overrides, rows_for_parent, branch, company, rows_to_remove):
	"""Idempotently align `rows_for_parent` 1:1 with the resolved bundle components.

	ERPNext's `make_packing_list()` keeps previously swapped rows on draft re-save / submit
	while appending freshly regenerated default bundle rows. Reconciling 1:1 against
	`resolve_bundle_components()` reuses existing rows (preferring exact `item_code` match,
	then slot match) and drops every regenerated default or stale duplicate row so repeated
	saves and `submit()` always preserve exact 1:1 `packed_items`.
	"""
	parent_qty = flt(parent_row.get("qty") or 0)
	resolved = resolve_bundle_components(
		parent_row.get("item_code"),
		branch=branch,
		company=company,
		overrides=parsed_overrides,
		qty=parent_qty,
	)

	if resolved:
		pool = list(rows_for_parent)
		for comp in resolved:
			chosen_code = comp["item_code"]
			slot_code = comp.get("slot_item") or ""
			matched = None

			# 1. Reuse already-swapped / matching item_code row first (idempotent across re-saves!)
			for idx, cand in enumerate(pool):
				if cand.get("item_code") == chosen_code:
					matched = pool.pop(idx)
					break

			# 2. Else reuse the default slot row that this override replaces
			if matched is None and slot_code:
				for idx, cand in enumerate(pool):
					if cand.get("item_code") == slot_code:
						matched = pool.pop(idx)
						break

			# 3. Else reuse any leftover row from this parent
			if matched is None and pool:
				matched = pool.pop(0)

			if matched is not None:
				_update_packed_row_from_component(matched, comp, parent_row, branch, company)
			else:
				new_wh = _resolve_component_warehouse(
					chosen_code, branch, company, parent_row.get("warehouse")
				)
				row_data = {
					"parent_item": parent_row.get("item_code"),
					"parent_detail_docname": parent_row.get("name"),
					"item_code": chosen_code,
					"item_name": comp.get("item_name") or chosen_code,
					"description": comp.get("description") or comp.get("item_name") or chosen_code,
					"uom": comp.get("uom") or "Nos",
					"qty": flt(comp.get("qty")),
					"conversion_factor": 1.0,
					"warehouse": new_wh,
				}
				if new_wh and frappe.db.exists("DocType", "Bin"):
					bin_info = frappe.db.get_value(
						"Bin",
						{"item_code": chosen_code, "warehouse": new_wh},
						["actual_qty", "projected_qty", "valuation_rate"],
						as_dict=True,
					)
					if isinstance(bin_info, dict):
						if bin_info.get("actual_qty") is not None:
							row_data["actual_qty"] = flt(bin_info.get("actual_qty"))
						if bin_info.get("projected_qty") is not None:
							row_data["projected_qty"] = flt(bin_info.get("projected_qty"))
						if bin_info.get("valuation_rate") is not None:
							row_data["incoming_rate"] = flt(bin_info.get("valuation_rate"))
				if callable(getattr(doc, "append", None)):
					doc.append("packed_items", row_data)
				else:
					doc.packed_items.append(frappe._dict(row_data))

		# Drop any leftover regenerated default or duplicate rows for this parent
		rows_to_remove.extend(pool)
		return

	# Fallback when Product Bundle Item rows are absent in minimal synthetic mocks:
	# apply slot overrides once and drop duplicate rows for already-substituted slots.
	slot_queue = {}
	for ov in parsed_overrides:
		if ov["slot_item"]:
			slot_queue.setdefault(ov["slot_item"], []).append(ov)

	already_present = {r.get("item_code") for r in rows_for_parent}
	for packed in rows_for_parent:
		code = packed.get("item_code")
		slot_list = slot_queue.get(code)
		if not slot_list:
			continue
		ov = slot_list.pop(0)
		chosen_code = ov["chosen_item"]
		override_qty = flt(ov.get("qty") or 1) * parent_qty
		if chosen_code != code and chosen_code in already_present:
			rows_to_remove.append(packed)
			continue
		_update_packed_row_from_component(
			packed,
			{"item_code": chosen_code, "item_name": ov.get("label") or chosen_code, "qty": override_qty},
			parent_row,
			branch,
			company,
		)
		already_present.add(chosen_code)


def apply_bundle_overrides_to_packed_items(doc):
	"""Swap default `packed_items` rows to chosen combo items before submit writes SLE/GL.

	Called in `Sales Invoice.validate` (right after ERPNext's `make_packing_list()`) and
	`POS Invoice.validate`. Idempotent across repeated draft saves and submit. Safe no-op
	when no row carries `custom_bundle_overrides_json`.
	"""
	items = doc.get("items") or []
	packed_items = doc.get("packed_items") or []
	if not items or not packed_items:
		return

	branch = doc.get("branch")
	company = doc.get("company")

	overrides_by_detail = {}
	overrides_by_parent_item = {}
	plain_bundles_by_detail = {}
	for row in items:
		raw_overrides = row.get("custom_bundle_overrides_json")
		row_name = row.get("name")
		if not raw_overrides:
			if row_name and row.get("item_code"):
				plain_bundles_by_detail[row_name] = row
			continue
		parsed = parse_bundle_overrides(raw_overrides)
		if not parsed:
			if row_name and row.get("item_code"):
				plain_bundles_by_detail[row_name] = row
			continue
		entry = (row, parsed)
		if row_name:
			overrides_by_detail[row_name] = entry
		else:
			overrides_by_parent_item.setdefault(row.get("item_code"), []).append(entry)

	# Group packed rows by parent_detail_docname
	packed_by_detail = {}
	for packed in packed_items:
		detail_name = packed.get("parent_detail_docname")
		if detail_name:
			packed_by_detail.setdefault(detail_name, []).append(packed)

	rows_to_remove = []

	for detail_name, (parent_row, parsed) in overrides_by_detail.items():
		rows_for_parent = packed_by_detail.get(detail_name, [])
		if rows_for_parent:
			_reconcile_packed_rows_for_parent(
				doc, parent_row, parsed, rows_for_parent, branch, company, rows_to_remove
			)

	# If a previously swapped bundle row had its overrides cleared back to default,
	# drop any stale extra rows so packed_items matches bundle defaults 1:1.
	for detail_name, parent_row in plain_bundles_by_detail.items():
		rows_for_parent = packed_by_detail.get(detail_name, [])
		if not rows_for_parent:
			continue
		_bname, defaults = get_product_bundle_defaults(parent_row.get("item_code"))
		if defaults and len(rows_for_parent) > len(defaults):
			_reconcile_packed_rows_for_parent(
				doc, parent_row, [], rows_for_parent, branch, company, rows_to_remove
			)

	for parent_code, entries in overrides_by_parent_item.items():
		matching_packed = [
			p for p in packed_items if not p.get("parent_detail_docname") and p.get("parent_item") == parent_code
		]
		for parent_row, parsed in entries:
			if matching_packed:
				_reconcile_packed_rows_for_parent(
					doc, parent_row, parsed, matching_packed, branch, company, rows_to_remove
				)

	if rows_to_remove:
		remove_ids = {id(r) for r in rows_to_remove}
		if callable(getattr(doc, "remove", None)) and all(getattr(r, "parentfield", None) for r in rows_to_remove):
			for r in rows_to_remove:
				doc.remove(r)
		else:
			doc.packed_items[:] = [p for p in doc.packed_items if id(p) not in remove_ids]
		for idx, p in enumerate(doc.packed_items, start=1):
			if hasattr(p, "idx"):
				p.idx = idx
			elif isinstance(p, dict):
				p["idx"] = idx
