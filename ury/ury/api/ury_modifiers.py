"""Server-authoritative engine for POS-PR-8: G3 Condiments/Modifiers & G4 Combo Meals.

Responsibilities:
- Batched menu metadata lookup (`get_modifier_groups_for_items`) with Item Group inheritance,
  native `Product Bundle` defaults, authoritative `Item Price` lookup, and 86/disabled checks.
- Order validation & normalization (`validate_and_normalize_order_items`): enforces required
  groups, `min_selections`, `max_selections`, configured prefixes, allowed items, parent-child
  integrity (no orphans), availability/86 checks, and authoritative 0/2/3dp server pricing
  (never trusts client rates).
- EFRIS-safe storage: unpriced prep condiments stay on the parent row (`custom_modifiers_json`
  + formatted `comment`) with zero extra invoice rows; priced add-ons and positive combo
  upcharges use linked child rows (`custom_parent_line_key`, `custom_is_modifier=1`).
- Defense-in-depth `before_submit` validation (`validate_invoice_modifiers_before_submit`).
"""

import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt

from ury.brand import coded_message
from ury.ury.api.item_price import item_price_release_states, price_valid_on
from ury.ury.api.ury_bundle_resolver import parse_bundle_overrides

DEFAULT_ALLOWED_PREFIXES = ("No", "Extra", "Side", "Light", "On Side")
MODIFIER_GROUP_DOCTYPE = "URY Modifier Group"
MODIFIER_OPTION_DOCTYPE = "URY Modifier Option"
ITEM_MODIFIER_LINK_DOCTYPE = "URY Item Modifier Group"


def parse_allowed_prefixes(raw):
	"""Parse newline- or comma-separated prefix configuration into a clean list."""
	if not raw:
		return []
	if isinstance(raw, (list, tuple)):
		tokens = [str(p).strip() for p in raw]
	else:
		tokens = []
		for line in str(raw).splitlines():
			for part in line.split(","):
				tokens.append(part.strip())
	return list(dict.fromkeys(tok for tok in tokens if tok))


def _parse_json_list(raw):
	if not raw:
		return []
	if isinstance(raw, str):
		try:
			parsed = json.loads(raw)
		except (TypeError, ValueError):
			return []
	elif isinstance(raw, (list, tuple)):
		parsed = list(raw)
	else:
		return []
	return [row for row in parsed if isinstance(row, dict)]


def _schema_ready():
	try:
		return bool(
			frappe.db.table_exists(MODIFIER_GROUP_DOCTYPE)
			and frappe.db.table_exists(MODIFIER_OPTION_DOCTYPE)
			and frappe.db.table_exists(ITEM_MODIFIER_LINK_DOCTYPE)
		)
	except Exception:
		return False


def _empty_item_meta():
	return {
		"is_combo_meal": False,
		"product_bundle": None,
		"bundle_defaults": [],
		"has_modifiers": False,
		"has_required_modifiers": False,
		"prompt_on_add": False,
		"modifier_groups": [],
	}


def _availability_map(item_codes, branch):
	"""Return {item_code: bool(available)} checking Item.disabled and URY Item Availability."""
	codes = list(dict.fromkeys(c for c in item_codes if c))
	if not codes:
		return {}

	item_rows = frappe.get_all(
		"Item",
		filters={"name": ["in", codes]},
		fields=["name", "disabled"],
	)
	disabled_by_code = {r.get("name"): cint(r.get("disabled")) for r in item_rows}

	overrides_by_code = {}
	if branch:
		try:
			if frappe.db.table_exists("URY Item Availability"):
				for ov in frappe.get_all(
					"URY Item Availability",
					filters={"branch": branch, "item": ["in", codes]},
					fields=["item", "status", "countdown_enabled", "remaining_qty"],
				):
					overrides_by_code[ov.get("item")] = ov
		except Exception:
			pass

	result = {}
	for code in codes:
		if code in disabled_by_code and disabled_by_code[code] == 1:
			result[code] = False
			continue
		ov = overrides_by_code.get(code)
		if ov:
			if ov.get("status") == "Sold Out":
				result[code] = False
				continue
			if cint(ov.get("countdown_enabled")) == 1 and flt(ov.get("remaining_qty")) <= 0:
				result[code] = False
				continue
		result[code] = True
	return result


def get_modifier_groups_for_items(
	item_codes,
	price_list=None,
	branch=None,
	company=None,
	posting_date=None,
	customer=None,
):
	"""Resolve modifier groups (with Item Group inheritance) & native Product Bundle defaults in batches."""
	codes = list(dict.fromkeys(c for c in (item_codes or []) if c))
	result = {code: _empty_item_meta() for code in codes}
	if not codes or not _schema_ready():
		return result

	posting_date = frappe.utils.getdate(posting_date) if posting_date else frappe.utils.getdate()

	# 1. Batch-read Item metadata (for item_group inheritance)
	item_rows = frappe.get_all(
		"Item",
		filters={"name": ["in", codes]},
		fields=["name", "item_name", "item_group", "disabled"],
	)
	item_group_by_code = {r.get("name"): r.get("item_group") for r in item_rows}
	item_groups = list(dict.fromkeys(ig for ig in item_group_by_code.values() if ig))

	# 2. Batch-read Item and Item Group modifier group links
	item_links = frappe.get_all(
		ITEM_MODIFIER_LINK_DOCTYPE,
		filters={"parenttype": "Item", "parent": ["in", codes]},
		fields=["parent", "modifier_group", "override_required", "min_override", "max_override", "sort_order", "idx"],
		order_by="sort_order asc, idx asc",
	)
	group_links = (
		frappe.get_all(
			ITEM_MODIFIER_LINK_DOCTYPE,
			filters={"parenttype": "Item Group", "parent": ["in", item_groups]},
			fields=["parent", "modifier_group", "override_required", "min_override", "max_override", "sort_order", "idx"],
			order_by="sort_order asc, idx asc",
		)
		if item_groups
		else []
	)

	item_links_by_parent = defaultdict(list)
	for row in item_links:
		item_links_by_parent[row.get("parent")].append(row)

	ig_links_by_parent = defaultdict(list)
	for row in group_links:
		ig_links_by_parent[row.get("parent")].append(row)

	# 3. Batch-read native Product Bundles & default items
	bundle_by_item = {}
	bundle_defaults_by_bundle = defaultdict(list)
	try:
		bundles = frappe.get_all(
			"Product Bundle",
			filters={"new_item_code": ["in", codes]},
			fields=["name", "new_item_code"],
		)
		for b in bundles:
			if b.get("new_item_code"):
				bundle_by_item[b["new_item_code"]] = b["name"]
		if bundle_by_item:
			pb_items = frappe.get_all(
				"Product Bundle Item",
				filters={"parent": ["in", list(dict.fromkeys(bundle_by_item.values()))]},
				fields=["parent", "item_code", "qty", "uom", "description"],
				order_by="idx asc",
			)
			for pbi in pb_items:
				bundle_defaults_by_bundle[pbi.get("parent")].append(pbi)
	except Exception:
		pass

	# 4. Batch-read referenced URY Modifier Groups and their options
	all_group_names = list(
		dict.fromkeys(
			row.get("modifier_group")
			for row in (list(item_links) + list(group_links))
			if row.get("modifier_group")
		)
	)
	groups_by_name = {}
	options_by_group = defaultdict(list)
	option_item_codes = set()
	for pb_rows in bundle_defaults_by_bundle.values():
		for pbi in pb_rows:
			if pbi.get("item_code"):
				option_item_codes.add(pbi["item_code"])

	if all_group_names:
		for grp in frappe.get_all(
			MODIFIER_GROUP_DOCTYPE,
			filters={"name": ["in", all_group_names], "disabled": 0},
			fields=[
				"name",
				"group_name",
				"display_title",
				"group_type",
				"required",
				"prompt_on_add",
				"min_selections",
				"max_selections",
				"allow_prefixes",
				"allowed_prefixes",
				"inherit_parent_routing",
				"hide_unpriced_on_receipt",
				"sort_order",
			],
		):
			groups_by_name[grp.get("name")] = grp

		if groups_by_name:
			opt_rows = frappe.get_all(
				MODIFIER_OPTION_DOCTYPE,
				filters={"parent": ["in", list(groups_by_name.keys())], "disabled": 0},
				fields=[
					"parent",
					"item",
					"option_code",
					"label",
					"price_mode",
					"upcharge_amount",
					"upcharge_item",
					"replaces_bundle_item",
					"qty",
					"is_default",
					"sort_order",
					"idx",
				],
				order_by="sort_order asc, idx asc",
			)
			for opt in opt_rows:
				options_by_group[opt.get("parent")].append(opt)
				if opt.get("item"):
					option_item_codes.add(opt["item"])
				if opt.get("upcharge_item"):
					option_item_codes.add(opt["upcharge_item"])

	# 5. Batch-read availability & item names for all option / bundle component items
	avail_by_code = _availability_map(option_item_codes, branch)
	comp_item_names = {}
	if option_item_codes:
		for row in frappe.get_all(
			"Item",
			filters={"name": ["in", list(option_item_codes)]},
			fields=["name", "item_name"],
		):
			comp_item_names[row.get("name")] = row.get("item_name")

	# 6. Cache Menu Item Price lookups for priced options
	release_field, released = (None, None)
	price_cache = {}

	def _resolve_option_price(opt):
		mode = opt.get("price_mode") or "Unpriced Prep"
		if mode == "Unpriced Prep":
			return 0.0
		if mode == "Combo Upcharge":
			return flt(opt.get("upcharge_amount") or 0)
		if mode == "Menu Item Price":
			opt_item = opt.get("item")
			if not opt_item or not price_list:
				return 0.0
			cache_key = (opt_item, price_list)
			if cache_key not in price_cache:
				nonlocal release_field, released
				if release_field is None and released is None:
					release_field, released = item_price_release_states()
				ip = price_valid_on(
					opt_item, price_list, posting_date, release_field, released, customer=customer
				)
				price_cache[cache_key] = flt(ip.price_list_rate) if ip else None
			val = price_cache[cache_key]
			return flt(val) if val is not None else 0.0
		return 0.0

	# 7. Assemble per-item payload
	for code in codes:
		merged_links = {}
		ig = item_group_by_code.get(code)
		for link in ig_links_by_parent.get(ig, []):
			gname = link.get("modifier_group")
			if gname in groups_by_name:
				merged_links[gname] = (link, 0)
		for link in item_links_by_parent.get(code, []):
			gname = link.get("modifier_group")
			if gname in groups_by_name:
				merged_links[gname] = (link, 1)

		ordered_links = sorted(
			merged_links.values(),
			key=lambda pair: (
				cint(pair[0].get("sort_order") or groups_by_name[pair[0].get("modifier_group")].get("sort_order")),
				pair[1],
				cint(pair[0].get("idx")),
			),
		)

		serialized_groups = []
		for link, _origin in ordered_links:
			grp = groups_by_name[link.get("modifier_group")]
			required = bool(cint(link.get("override_required")) or cint(grp.get("required")))
			min_sel = cint(link.get("min_override")) if cint(link.get("min_override")) > 0 else cint(grp.get("min_selections"))
			if required and min_sel < 1:
				min_sel = 1
			max_sel = cint(link.get("max_override")) if cint(link.get("max_override")) > 0 else cint(grp.get("max_selections"))
			allow_prefixes = bool(cint(grp.get("allow_prefixes")))
			prefixes = parse_allowed_prefixes(grp.get("allowed_prefixes")) if allow_prefixes else []

			serialized_options = []
			for opt in options_by_group.get(grp.get("name"), []):
				opt_item = opt.get("item")
				upcharge_item = opt.get("upcharge_item")
				is_avail = True
				if opt_item:
					is_avail = bool(avail_by_code.get(opt_item, True))
				if is_avail and upcharge_item:
					is_avail = bool(avail_by_code.get(upcharge_item, True))
				serialized_options.append(
					{
						"option_code": opt.get("option_code") or opt_item or opt.get("label"),
						"item": opt_item or None,
						"label": opt.get("label") or comp_item_names.get(opt_item) or opt_item or opt.get("option_code"),
						"price_mode": opt.get("price_mode") or "Unpriced Prep",
						"upcharge_amount": flt(opt.get("upcharge_amount") or 0),
						"upcharge_item": upcharge_item or None,
						"replaces_bundle_item": opt.get("replaces_bundle_item") or None,
						"qty": flt(opt.get("qty") or 1),
						"is_default": bool(cint(opt.get("is_default"))),
						"unit_price": _resolve_option_price(opt),
						"available": is_avail,
					}
				)

			serialized_groups.append(
				{
					"name": grp.get("name"),
					"display_title": grp.get("display_title") or grp.get("group_name") or grp.get("name"),
					"group_type": grp.get("group_type") or "Condiment",
					"required": required,
					"prompt_on_add": bool(cint(grp.get("prompt_on_add")) or required or min_sel > 0),
					"min_selections": min_sel,
					"max_selections": max_sel,
					"allow_prefixes": allow_prefixes,
					"allowed_prefixes": prefixes,
					"inherit_parent_routing": bool(cint(grp.get("inherit_parent_routing", 1))),
					"hide_unpriced_on_receipt": bool(cint(grp.get("hide_unpriced_on_receipt", 1))),
					"options": serialized_options,
				}
			)

		bundle_name = bundle_by_item.get(code)
		bundle_defaults = [
			{
				"item_code": pbi.get("item_code"),
				"item_name": comp_item_names.get(pbi.get("item_code")) or pbi.get("description") or pbi.get("item_code"),
				"qty": flt(pbi.get("qty") or 1),
			}
			for pbi in bundle_defaults_by_bundle.get(bundle_name, [])
		]

		has_required = any(g["required"] or g["min_selections"] > 0 for g in serialized_groups)
		prompt_on_add = any(g["prompt_on_add"] for g in serialized_groups) or (bool(bundle_name) and bool(serialized_groups))
		result[code] = {
			"is_combo_meal": bool(bundle_name),
			"product_bundle": bundle_name,
			"bundle_defaults": bundle_defaults,
			"has_modifiers": bool(serialized_groups),
			"has_required_modifiers": has_required,
			"prompt_on_add": prompt_on_add,
			"modifier_groups": serialized_groups,
		}

	return result


def format_modifiers_comment(unpriced_modifiers, bundle_overrides=None, manual_comment=""):
	"""Build clean KDS/waiter summary string from structured modifiers + optional manual note."""
	parts = []
	for mod in unpriced_modifiers or []:
		label = (mod.get("label") or mod.get("option_code") or "").strip()
		prefix = (mod.get("prefix") or "").strip()
		text = f"{prefix} {label}".strip() if prefix else label
		if text and text not in parts:
			parts.append(text)

	for ov in bundle_overrides or []:
		label = (ov.get("label") or ov.get("chosen_item") or "").strip()
		if label and label not in parts:
			parts.append(label)

	# Preserve any free-text manual segments not already generated from modifiers/combos
	manual_segments = []
	for seg in str(manual_comment or "").split("·"):
		clean = seg.strip()
		if clean and clean not in parts and clean not in manual_segments:
			manual_segments.append(clean)

	return " · ".join(parts + manual_segments)


def _match_prefix(prefix, group):
	clean = (prefix or "").strip()
	if not clean:
		return ""
	if not group.get("allow_prefixes"):
		frappe.throw(
			coded_message(
				"MODIFIER-PREFIX-INVALID",
				_("Prefix '{0}' is not allowed for {1}.").format(clean, group["display_title"]),
			),
			frappe.ValidationError,
		)
	allowed = group.get("allowed_prefixes") or []
	by_lower = {p.lower(): p for p in allowed}
	canonical = by_lower.get(clean.lower())
	if not canonical:
		frappe.throw(
			coded_message(
				"MODIFIER-PREFIX-INVALID",
				_("Prefix '{0}' is not allowed for {1}. Allowed: {2}.").format(
					clean, group["display_title"], ", ".join(allowed) or _("none")
				),
			),
			frappe.ValidationError,
		)
	return canonical


def _find_option(group, *, option_code=None, item_code=None, label=None):
	options = group.get("options") or []
	if option_code:
		for opt in options:
			if opt["option_code"] == option_code:
				return opt
	if item_code:
		for opt in options:
			if opt.get("item") == item_code or opt.get("upcharge_item") == item_code:
				return opt
	if label:
		for opt in options:
			if (opt.get("label") or "").strip().lower() == str(label).strip().lower():
				return opt
	return None


def validate_and_normalize_order_items(
	items,
	past_item,
	menu,
	price_list,
	branch,
	company,
	*,
	posting_date=None,
	customer=None,
	currency_precision=2,
):
	"""Validate all G3 modifier & G4 combo selections and compute authoritative server rates.

	Returns `(normalized_items, price_overrides)` where `price_overrides` maps each child row's
	`reservation_line_key` (or index fallback) to `{rate, price_list_rate, item_code, is_validated_modifier}`.
	"""
	if not items or not _schema_ready():
		return list(items or []), {}

	posting_date = frappe.utils.getdate(posting_date) if posting_date else frappe.utils.getdate()
	precision = cint(currency_precision) if currency_precision is not None else 2
	normalized = [dict(row) for row in items]

	# Identify parents vs linked child modifier / combo upcharge rows
	parent_by_key = {}
	parent_internal_keys = []
	children_by_parent_key = defaultdict(list)
	child_rows = []
	parent_rows = []

	for idx, row in enumerate(normalized):
		item_code = row.get("item") or row.get("item_code")
		if item_code and not row.get("item"):
			row["item"] = item_code

		is_mod = cint(row.get("custom_is_modifier")) == 1 or bool(row.get("custom_parent_line_key"))
		if is_mod:
			if not row.get("reservation_line_key"):
				row["reservation_line_key"] = f"mod-line-{idx + 1}"
			child_rows.append(row)
		else:
			parent_key = row.get("reservation_line_key") or f"__line-{idx + 1}"
			parent_rows.append((parent_key, row))
			parent_by_key[parent_key] = row
			parent_internal_keys.append(parent_key)

	# Also allow past_item parent keys when appending rounds
	historic_parent_keys = {
		p.get("reservation_line_key")
		for p in (past_item or [])
		if p.get("reservation_line_key") and not cint(p.get("custom_is_modifier"))
	}

	# 1. Orphan check
	for child in child_rows:
		parent_key = (child.get("custom_parent_line_key") or "").strip()
		if not parent_key or (parent_key not in parent_by_key and parent_key not in historic_parent_keys):
			frappe.throw(
				coded_message(
					"MODIFIER-ORPHAN",
					_("Modifier item '{0}' requires an active parent item on this order.").format(
						child.get("item_name") or child.get("item")
					),
				),
				frappe.ValidationError,
			)
		children_by_parent_key[parent_key].append(child)

	# 2. Fetch batched modifier/combo metadata for all parent items
	parent_codes = list(dict.fromkeys(r.get("item") for _k, r in parent_rows if r.get("item")))
	meta_by_item = get_modifier_groups_for_items(
		parent_codes,
		price_list=price_list,
		branch=branch,
		company=company,
		posting_date=posting_date,
		customer=customer,
	)

	historic_by_key = {
		p.get("reservation_line_key"): p
		for p in (past_item or [])
		if p.get("reservation_line_key")
	}

	release_field, released = (None, None)
	price_overrides = {}

	# 3. Validate each parent + its unpriced prep, combo overrides, and linked priced children
	for parent_key, parent in parent_rows:
		parent_code = parent.get("item")
		parent_label = parent.get("item_name") or parent_code
		meta = meta_by_item.get(parent_code) or _empty_item_meta()
		groups = meta["modifier_groups"]
		groups_by_name = {g["name"]: g for g in groups}

		raw_unpriced = _parse_json_list(parent.get("custom_modifiers_json"))
		raw_overrides = parse_bundle_overrides(parent.get("custom_bundle_overrides_json"))
		linked_children = children_by_parent_key.get(parent_key, [])

		# Untouched legacy row from before modifiers were configured on this item
		is_untouched_historic = (
			parent_key in historic_by_key
			and not raw_unpriced
			and not raw_overrides
			and not linked_children
			and not groups
		)
		if is_untouched_historic:
			continue

		selections_by_group = defaultdict(int)
		combo_counted_keys = set()
		norm_unpriced = []
		norm_overrides = []

		# 3a. Validate unpriced prep modifiers stored on parent
		for entry in raw_unpriced:
			gname = (entry.get("group") or "").strip()
			group = groups_by_name.get(gname)
			if not group:
				frappe.throw(
					coded_message(
						"MODIFIER-NOT-ALLOWED",
						_("Modifier group '{0}' is not allowed on {1}.").format(gname or _("unknown"), parent_label),
					),
					frappe.ValidationError,
				)
			opt = _find_option(
				group,
				option_code=entry.get("option_code"),
				item_code=entry.get("item"),
				label=entry.get("label"),
			)
			if not opt:
				frappe.throw(
					coded_message(
						"MODIFIER-NOT-ALLOWED",
						_("Modifier '{0}' is not allowed in {1}.").format(
							entry.get("label") or entry.get("option_code") or _("unknown"),
							group["display_title"],
						),
					),
					frappe.ValidationError,
				)
			if not opt.get("available", True):
				frappe.throw(
					coded_message(
						"MODIFIER-UNAVAILABLE",
						_("Option '{0}' is currently unavailable.").format(opt["label"]),
					),
					frappe.ValidationError,
				)
			canonical_prefix = _match_prefix(entry.get("prefix"), group)
			selections_by_group[gname] += 1
			norm_unpriced.append(
				{
					"group": gname,
					"option_code": opt["option_code"],
					"label": opt["label"],
					"prefix": canonical_prefix,
					"price_mode": opt["price_mode"],
					"hide_on_receipt": 1 if group["hide_unpriced_on_receipt"] else 0,
				}
			)

		# 3b. Validate linked child rows (priced modifiers & positive combo upcharges)
		child_combo_keys = set()
		for child in linked_children:
			child_code = child.get("item")
			gname = (child.get("custom_modifier_group") or "").strip()
			group = groups_by_name.get(gname)
			if not group:
				frappe.throw(
					coded_message(
						"MODIFIER-NOT-ALLOWED",
						_("Modifier '{0}' is not allowed on {1}.").format(
							child.get("item_name") or child_code, parent_label
						),
					),
					frappe.ValidationError,
				)
			opt = _find_option(
				group,
				option_code=child.get("custom_modifier_option"),
				item_code=child_code,
			)
			if not opt:
				frappe.throw(
					coded_message(
						"MODIFIER-NOT-ALLOWED",
						_("Item '{0}' is not an allowed option in {1}.").format(
							child.get("item_name") or child_code, group["display_title"]
						),
					),
					frappe.ValidationError,
				)

			# Verify the child row item matches the configured item or upcharge fee item
			expected_items = {x for x in (opt.get("item"), opt.get("upcharge_item")) if x}
			if expected_items and child_code not in expected_items:
				frappe.throw(
					coded_message(
						"MODIFIER-NOT-ALLOWED",
						_("Item '{0}' does not match configured option '{1}'.").format(child_code, opt["label"]),
					),
					frappe.ValidationError,
				)

			if not opt.get("available", True):
				frappe.throw(
					coded_message(
						"MODIFIER-UNAVAILABLE",
						_("Option '{0}' is currently unavailable.").format(opt["label"]),
					),
					frappe.ValidationError,
				)

			canonical_prefix = _match_prefix(child.get("custom_modifier_prefix"), group)

			# Authoritative server-side pricing (never trust client rate!)
			mode = opt["price_mode"]
			if mode == "Menu Item Price":
				if release_field is None and released is None:
					release_field, released = item_price_release_states()
				ip = price_valid_on(
					opt["item"], price_list, posting_date, release_field, released, customer=customer
				)
				if not ip:
					frappe.throw(
						coded_message(
							"PRICE-NOT-VALID",
							_("No released price found for modifier '{0}' in price list '{1}'.").format(
								opt["label"], price_list
							),
						),
						frappe.ValidationError,
					)
				server_rate = flt(ip.price_list_rate, precision)
				line_role = "Modifier"
			elif mode == "Combo Upcharge":
				server_rate = flt(opt.get("upcharge_amount") or 0, precision)
				if server_rate <= 0:
					frappe.throw(
						coded_message(
							"MODIFIER-NOT-ALLOWED",
							_("Zero-priced combo option '{0}' must not be billed as a separate line.").format(opt["label"]),
						),
						frappe.ValidationError,
					)
				line_role = "Combo Upcharge"
				child_combo_keys.add((gname, opt["option_code"]))
			else:
				frappe.throw(
					coded_message(
						"MODIFIER-NOT-ALLOWED",
						_("Unpriced modifier '{0}' cannot be billed as a separate priced line.").format(opt["label"]),
					),
					frappe.ValidationError,
				)

			child["custom_is_modifier"] = 1
			child["custom_parent_line_key"] = parent_key
			child["custom_line_role"] = line_role
			child["custom_modifier_group"] = gname
			child["custom_modifier_option"] = opt["option_code"]
			child["custom_modifier_prefix"] = canonical_prefix
			child["custom_inherit_parent_routing"] = 1 if group["inherit_parent_routing"] else 0
			child["custom_hide_unpriced_on_receipt"] = 0
			child["rate"] = server_rate

			price_overrides[child["reservation_line_key"]] = {
				"rate": server_rate,
				"price_list_rate": server_rate,
				"item_code": child_code,
				"is_validated_modifier": True,
			}

			if line_role == "Combo Upcharge":
				combo_counted_keys.add((gname, opt["option_code"]))
			selections_by_group[gname] += 1

		# 3c. Validate G4 combo bundle overrides stored on parent
		for ov in raw_overrides:
			gname = ov["group"]
			group = groups_by_name.get(gname)
			if not group:
				# Try locating the combo group by chosen_item when group omitted
				for candidate in groups:
					if candidate["group_type"] == "Combo Choice" and _find_option(
						candidate, option_code=ov.get("option_code"), item_code=ov["chosen_item"]
					):
						group = candidate
						gname = candidate["name"]
						break
			if not group:
				frappe.throw(
					coded_message(
						"MODIFIER-NOT-ALLOWED",
						_("Combo choice '{0}' is not allowed on {1}.").format(ov["chosen_item"], parent_label),
					),
					frappe.ValidationError,
				)
			opt = _find_option(
				group,
				option_code=ov.get("option_code"),
				item_code=ov["chosen_item"],
				label=ov.get("label"),
			)
			if not opt or (opt.get("item") and opt["item"] != ov["chosen_item"]):
				frappe.throw(
					coded_message(
						"MODIFIER-NOT-ALLOWED",
						_("Combo option '{0}' is not allowed in {1}.").format(
							ov.get("label") or ov["chosen_item"], group["display_title"]
						),
					),
					frappe.ValidationError,
				)
			if not opt.get("available", True):
				frappe.throw(
					coded_message(
						"MODIFIER-UNAVAILABLE",
						_("Combo option '{0}' is currently unavailable.").format(opt["label"]),
					),
					frappe.ValidationError,
				)

			# Prevent client from substituting a paid upgrade without its upcharge child line
			expected_upcharge = flt(opt.get("upcharge_amount") or 0, precision)
			if expected_upcharge > 0 and (gname, opt["option_code"]) not in child_combo_keys:
				frappe.throw(
					coded_message(
						"COMBO-UPCHARGE-REQUIRED",
						_("Selection '{0}' requires an upcharge line of {1}.").format(opt["label"], expected_upcharge),
					),
					frappe.ValidationError,
				)

			if (gname, opt["option_code"]) not in combo_counted_keys:
				selections_by_group[gname] += 1
				combo_counted_keys.add((gname, opt["option_code"]))

			norm_overrides.append(
				{
					"group": gname,
					"slot_item": ov["slot_item"] or opt.get("replaces_bundle_item") or "",
					"chosen_item": opt.get("item") or ov["chosen_item"],
					"option_code": opt["option_code"],
					"label": opt["label"],
					"qty": flt(ov.get("qty") or opt.get("qty") or 1),
					"inherit_parent_routing": 1 if group["inherit_parent_routing"] else 0,
					"hide_on_receipt": 1 if group["hide_unpriced_on_receipt"] else 0,
				}
			)

		# 3d. Enforce min/max across all groups attached to this item
		for group in groups:
			gname = group["name"]
			count = selections_by_group.get(gname, 0)
			min_req = max(cint(group["min_selections"]), 1 if group["required"] else 0)
			max_allowed = cint(group["max_selections"])
			if count < min_req:
				frappe.throw(
					coded_message(
						"MODIFIER-MIN-REQUIRED",
						_("Select at least {0} option(s) for {1} on {2}.").format(
							min_req, group["display_title"], parent_label
						),
					),
					frappe.ValidationError,
				)
			if max_allowed > 0 and count > max_allowed:
				frappe.throw(
					coded_message(
						"MODIFIER-MAX-EXCEEDED",
						_("Select at most {0} option(s) for {1} on {2}.").format(
							max_allowed, group["display_title"], parent_label
						),
					),
					frappe.ValidationError,
				)

		# 3e. Normalize parent metadata & formatted KDS/waiter comment
		parent["custom_is_modifier"] = 0
		parent["custom_parent_line_key"] = ""
		parent["custom_line_role"] = "Combo Header" if meta["is_combo_meal"] else (parent.get("custom_line_role") or "Standard")
		parent["custom_modifiers_json"] = json.dumps(norm_unpriced, ensure_ascii=False) if norm_unpriced else ""
		parent["custom_bundle_overrides_json"] = json.dumps(norm_overrides, ensure_ascii=False) if norm_overrides else ""
		parent["custom_hide_unpriced_on_receipt"] = 1 if any(m.get("hide_on_receipt") for m in norm_unpriced) else 0
		if norm_unpriced or norm_overrides:
			parent["comment"] = format_modifiers_comment(
				norm_unpriced, norm_overrides, manual_comment=parent.get("comment") or ""
			)

	return normalized, price_overrides


def validate_invoice_modifiers_before_submit(doc, method=None):
	"""Defense-in-depth check on POS Invoice.before_submit (blocks direct REST/Desk bypass)."""
	if getattr(frappe.flags, "in_patch", False) or getattr(frappe.flags, "in_migrate", False):
		return
	if cint(doc.get("is_return")) == 1:
		return
	if not _schema_ready():
		return

	items = doc.get("items") or []
	if not items:
		return

	parent_keys = set()
	for row in items:
		if cint(row.get("is_disposable")) == 1 or flt(row.get("qty")) <= 0:
			continue
		if not cint(row.get("custom_is_modifier")):
			key = row.get("reservation_line_key") or row.get("name")
			if key:
				parent_keys.add(key)

	for row in items:
		if cint(row.get("is_disposable")) == 1 or flt(row.get("qty")) <= 0:
			continue
		if cint(row.get("custom_is_modifier")) == 1:
			pkey = (row.get("custom_parent_line_key") or "").strip()
			if not pkey or pkey not in parent_keys:
				frappe.throw(
					coded_message(
						"MODIFIER-ORPHAN",
						_("Modifier item '{0}' cannot be submitted without its parent item.").format(
							row.get("item_name") or row.get("item_code")
						),
					),
					frappe.ValidationError,
				)
