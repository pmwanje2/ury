"""Site-less tests for POS-PR-8: G3 Condiments/Modifiers & G4 Native Product Bundle Combos.

Covers:
- Config-first custom fields module (`ury.ury_pos.modifier_combo_fields`) & DocType JSON contracts.
- G3 Modifier Groups attached via Item and Item Group inheritance.
- Required / min_selections / max_selections enforcement (`[POS-MODIFIER-MIN-REQUIRED]`,
  `[POS-MODIFIER-MAX-EXCEEDED]`).
- Allowed prefixes (`No`, `Extra`, `Side`, `Light`, `On Side`) validation
  (`[POS-MODIFIER-PREFIX-INVALID]`).
- Disallowed modifier items (`[POS-MODIFIER-NOT-ALLOWED]`), orphan child lines
  (`[POS-MODIFIER-ORPHAN]`), and 86'd/disabled modifier options (`[POS-MODIFIER-UNAVAILABLE]`).
- Unpriced prep modifiers stored on parent (`custom_modifiers_json` + `comment`) with ZERO extra
  invoice lines (EFRIS-safe) and non-priced check suppression metadata (Simphony Opt 79).
- Authoritative server-side pricing for `Menu Item Price` and `Combo Upcharge` across 0/2/3dp
  currencies (`UGX`, `USD`, `KWD`), ignoring spoofed client rates.
- G4 Native ERPNext `Product Bundle` combo resolution, choice substitution (`Fries` -> `Onion Rings`),
  availability checks, and `packed_items` swap at Sales Invoice consolidation.
- Atomic parent-child `split_bill` (moving parent moves linked children; splitting child alone refused).
- 100% Maximus white-label: zero vendor terms in refusal messages or DocType labels/descriptions.
"""

import importlib
import importlib.util
import json
import re
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

APP_ROOT = Path(__file__).resolve().parents[1]  # ury/ury_pos -> ury/
REPO_ROOT = APP_ROOT.parent
TODAY = date(2026, 11, 24)
FORBIDDEN_VENDOR_RE = re.compile(
	r"\b(?:erpnext|frappe|ury|kamra|huf|heykoala|tridz|buildsuite)\b", re.IGNORECASE
)


class Doc(SimpleNamespace):
	"""Document double where `.items` is a list attribute rather than `dict.items`."""

	def get(self, key, default=None):
		return getattr(self, key, default)

	def __getitem__(self, key):
		return getattr(self, key)


class Row(dict):
	"""frappe._dict-compatible double."""

	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError:
			return None

	def __setattr__(self, key, value):
		self[key] = value


def _stub(name, **attrs):
	module = ModuleType(name)
	for key, value in attrs.items():
		setattr(module, key, value)
	return module


def _flt(value, precision=None):
	num = float(value or 0)
	if precision is not None:
		return round(num, int(precision))
	return num


def _cint(value):
	try:
		return int(float(value or 0))
	except (TypeError, ValueError):
		return 0


def _getdate(value=None):
	if value is None:
		return TODAY
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	return date.fromisoformat(str(value)[:10])


def _make_frappe_double():
	framework = MagicMock()
	framework.PermissionError = type("PermissionError", (Exception,), {})
	framework.ValidationError = type("ValidationError", (Exception,), {})
	framework.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
	framework._ = lambda text, *args, **kwargs: text
	framework.whitelist = lambda *args, **kwargs: (lambda fn: fn)
	framework._dict = Row
	framework.conf = {}
	framework.flags = SimpleNamespace()
	framework.local = SimpleNamespace(flags=SimpleNamespace())
	framework.session = Row(user="waiter@maximus.test", sid="sid-mod-test", data={})
	framework.has_permission.return_value = True
	framework.get_roles.return_value = ["URY Cashier", "URY Captain"]
	framework.generate_hash.side_effect = lambda length=10: "HASH123456"[:length]

	def throw(message, exc=None, **kwargs):
		raise (exc or framework.ValidationError)(message)

	framework.throw = throw
	utils = _stub(
		"frappe.utils",
		cint=_cint,
		flt=_flt,
		getdate=_getdate,
		get_datetime=lambda v: v,
		now_datetime=lambda: datetime(2026, 11, 24, 14, 0, 0),
		get_system_timezone=lambda: "Africa/Kampala",
	)
	custom_field_mod = _stub(
		"frappe.custom.doctype.custom_field.custom_field",
		create_custom_fields=MagicMock(),
	)
	return framework, {
		"frappe": framework,
		"frappe.utils": utils,
		"frappe.model.document": _stub("frappe.model.document", Document=object),
		"frappe.custom.doctype.custom_field.custom_field": custom_field_mod,
		"erpnext.controllers.queries": _stub("erpnext.controllers.queries", item_query=MagicMock()),
	}


def _load_module(dotted_name, rel_path):
	path = APP_ROOT / rel_path
	if not path.is_file():
		raise AssertionError(f"Expected module {dotted_name} at {path} does not exist yet")
	spec = importlib.util.spec_from_file_location(dotted_name, path)
	module = importlib.util.module_from_spec(spec)
	sys.modules[dotted_name] = module
	spec.loader.exec_module(module)
	return module


class TestModifierComboSchemaAndWhiteLabel(unittest.TestCase):
	def test_dedicated_custom_fields_module_defines_all_g3_g4_fields_idempotently(self):
		framework, modules = _make_frappe_double()
		framework.db.exists.return_value = True
		with patch.dict(sys.modules, modules):
			fields_mod = _load_module("ury.ury_pos.modifier_combo_fields", "ury_pos/modifier_combo_fields.py")
			fields = fields_mod.get_modifier_combo_custom_fields()

			self.assertIn("Item", fields)
			self.assertIn("Item Group", fields)
			self.assertIn("POS Invoice Item", fields)
			self.assertIn("Sales Invoice Item", fields)

			item_names = {f["fieldname"] for f in fields["Item"]}
			self.assertIn("custom_modifier_groups", item_names)

			ig_names = {f["fieldname"] for f in fields["Item Group"]}
			self.assertIn("custom_default_modifier_groups", ig_names)

			for dt in ("POS Invoice Item", "Sales Invoice Item"):
				names = {f["fieldname"] for f in fields[dt]}
				for expected in (
					"custom_parent_line_key",
					"custom_line_role",
					"custom_is_modifier",
					"custom_modifier_prefix",
					"custom_modifier_group",
					"custom_modifier_option",
					"custom_modifiers_json",
					"custom_bundle_overrides_json",
					"custom_inherit_parent_routing",
					"custom_hide_unpriced_on_receipt",
				):
					self.assertIn(expected, names, f"{dt} missing {expected}")

			# White-label check on all custom field labels and descriptions
			for dt, rows in fields.items():
				for row in rows:
					for key in ("label", "description"):
						val = row.get(key) or ""
						self.assertIsNone(
							FORBIDDEN_VENDOR_RE.search(val),
							f"Vendor leak in {dt}.{row['fieldname']}.{key}: {val!r}",
						)

			# Idempotent installation runs cleanly twice
			fields_mod.ensure_modifier_combo_custom_fields()
			fields_mod.ensure_modifier_combo_custom_fields()
			self.assertEqual(modules["frappe.custom.doctype.custom_field.custom_field"].create_custom_fields.call_count, 2)

	def test_new_doctypes_exist_and_have_clean_white_label_metadata(self):
		expected_doctypes = [
			"ury/doctype/ury_modifier_group/ury_modifier_group.json",
			"ury/doctype/ury_modifier_option/ury_modifier_option.json",
			"ury/doctype/ury_item_modifier_group/ury_item_modifier_group.json",
		]
		for rel in expected_doctypes:
			path = APP_ROOT / rel
			self.assertTrue(path.is_file(), f"Missing DocType schema: {path}")
			data = json.loads(path.read_text(encoding="utf-8"))
			for field in data.get("fields", []):
				for key in ("label", "description"):
					val = field.get(key) or ""
					self.assertIsNone(
						FORBIDDEN_VENDOR_RE.search(val),
						f"Vendor leak in {rel} field {field.get('fieldname')}.{key}: {val!r}",
					)

		# URY KOT Items carries parent_line_key, is_modifier, modifier_summary
		kot_items = json.loads(
			(APP_ROOT / "ury/doctype/ury_kot_items/ury_kot_items.json").read_text(encoding="utf-8")
		)
		kot_fieldnames = {f["fieldname"] for f in kot_items["fields"]}
		for expected in ("parent_line_key", "is_modifier", "modifier_summary"):
			self.assertIn(expected, kot_fieldnames)

	def test_patch_is_registered_in_patches_txt(self):
		patches = (APP_ROOT / "patches.txt").read_text(encoding="utf-8").splitlines()
		self.assertIn("ury.patches.v2_0.setup_pos_modifiers_and_combos", [p.strip() for p in patches])


class _FakeModifierDB:
	"""In-memory DB double for testing modifier groups, item inheritance, prices, availability & bundles."""

	def __init__(self):
		self.items = {}
		self.item_modifier_groups = []  # rows of URY Item Modifier Group
		self.modifier_groups = {}  # name -> Row
		self.modifier_options = []  # rows of URY Modifier Option
		self.item_prices = {}  # (item_code, price_list) -> rate
		self.item_price_currencies = {}  # (item_code, price_list) -> currency
		self.price_list_currencies = {}  # price_list -> currency
		self.company_currency = "UGX"
		self.availability = {}  # (branch, item) -> Row(status, countdown_enabled, remaining_qty)
		self.bundles = {}  # new_item_code -> bundle_name
		self.bundle_items = {}  # bundle_name -> [Row(item_code, qty, uom, description)]
		self.ipc = {}  # (item, branch) -> Row
		self.currency_precision = {"UGX": 0, "USD": 2, "KWD": 3}

	def add_item(self, code, item_name=None, item_group="Mains", is_stock_item=1, disabled=0, stock_uom="Nos"):
		self.items[code] = Row(
			name=code,
			item_code=code,
			item_name=item_name or code,
			item_group=item_group,
			is_stock_item=is_stock_item,
			disabled=disabled,
			stock_uom=stock_uom,
			description=item_name or code,
		)

	def add_price(self, item_code, rate, price_list="Menu A", currency=None):
		self.item_prices[(item_code, price_list)] = rate
		if currency:
			self.item_price_currencies[(item_code, price_list)] = currency
			self.price_list_currencies[price_list] = currency

	def add_group(self, name, **kwargs):
		grp = Row(
			name=name,
			group_name=name,
			display_title=kwargs.get("display_title", name),
			group_type=kwargs.get("group_type", "Condiment"),
			required=kwargs.get("required", 0),
			prompt_on_add=kwargs.get("prompt_on_add", 0),
			min_selections=kwargs.get("min_selections", 0),
			max_selections=kwargs.get("max_selections", 0),
			allow_prefixes=kwargs.get("allow_prefixes", 1),
			allowed_prefixes=kwargs.get("allowed_prefixes", "No\nExtra\nSide\nLight\nOn Side"),
			inherit_parent_routing=kwargs.get("inherit_parent_routing", 1),
			hide_unpriced_on_receipt=kwargs.get("hide_unpriced_on_receipt", 1),
			sort_order=kwargs.get("sort_order", 0),
			disabled=kwargs.get("disabled", 0),
		)
		self.modifier_groups[name] = grp
		return grp

	def add_option(self, group_name, **kwargs):
		opt = Row(
			parent=group_name,
			parenttype="URY Modifier Group",
			item=kwargs.get("item"),
			option_code=kwargs.get("option_code") or kwargs.get("item") or kwargs.get("label"),
			label=kwargs.get("label") or kwargs.get("item"),
			price_mode=kwargs.get("price_mode", "Unpriced Prep"),
			upcharge_amount=kwargs.get("upcharge_amount", 0),
			upcharge_item=kwargs.get("upcharge_item"),
			replaces_bundle_item=kwargs.get("replaces_bundle_item"),
			qty=kwargs.get("qty", 1),
			is_default=kwargs.get("is_default", 0),
			sort_order=kwargs.get("sort_order", len(self.modifier_options) + 1),
			idx=len(self.modifier_options) + 1,
			disabled=kwargs.get("disabled", 0),
		)
		self.modifier_options.append(opt)
		return opt

	def attach_group(self, parenttype, parent, modifier_group, **kwargs):
		self.item_modifier_groups.append(
			Row(
				parenttype=parenttype,
				parent=parent,
				modifier_group=modifier_group,
				override_required=kwargs.get("override_required", 0),
				min_override=kwargs.get("min_override", 0),
				max_override=kwargs.get("max_override", 0),
				sort_order=kwargs.get("sort_order", 0),
				idx=len(self.item_modifier_groups) + 1,
			)
		)

	def add_bundle(self, combo_item, components):
		self.bundles[combo_item] = combo_item
		self.bundle_items[combo_item] = [
			Row(item_code=c[0], qty=c[1], uom="Nos", description=c[0])
			for c in components
		]

	def wire(self, framework):
		def exists(dt, filters=None):
			if dt == "DocType":
				return True
			if dt == "Product Bundle":
				if isinstance(filters, dict):
					return filters.get("new_item_code") in self.bundles
				return filters in self.bundles
			if dt == "Item":
				return filters in self.items
			return True

		def table_exists(dt):
			return True

		def get_all(dt, filters=None, fields=None, order_by=None, pluck=None, limit=None):
			filters = filters or {}

			def _match(row):
				for k, v in filters.items():
					val = row.get(k)
					if isinstance(v, (list, tuple)) and len(v) == 2 and v[0] == "in":
						if val not in v[1]:
							return False
					elif val != v:
						return False
				return True

			if dt == "Item":
				rows = [r for r in self.items.values() if _match(r)]
			elif dt == "URY Item Modifier Group":
				rows = [r for r in self.item_modifier_groups if _match(r)]
			elif dt == "URY Modifier Group":
				rows = [r for r in self.modifier_groups.values() if _match(r)]
			elif dt == "URY Modifier Option":
				rows = [r for r in self.modifier_options if _match(r)]
			elif dt == "Product Bundle":
				rows = [Row(name=b, new_item_code=ic) for ic, b in self.bundles.items() if _match(Row(name=b, new_item_code=ic))]
			elif dt == "Product Bundle Item":
				parent = filters.get("parent")
				if isinstance(parent, (list, tuple)) and len(parent) == 2 and parent[0] == "in":
					rows = []
					for p in parent[1]:
						for r in self.bundle_items.get(p, []):
							rows.append(Row(**r, parent=p))
				else:
					rows = [Row(**r, parent=parent) for r in self.bundle_items.get(parent, [])]
			elif dt == "URY Item Availability":
				rows = [r for r in self.availability.values() if _match(r)]
			elif dt == "URY Item Production Configuration":
				rows = [r for r in self.ipc.values() if _match(r)]
			else:
				rows = []

			if pluck:
				return [r.get(pluck) for r in rows]
			return rows

		def get_value(dt, filters, fieldname=None, as_dict=False, **kwargs):
			if dt == "Item":
				row = self.items.get(filters) if isinstance(filters, str) else None
				if not row:
					return None
				if as_dict or isinstance(fieldname, (list, tuple)):
					cols = fieldname if isinstance(fieldname, (list, tuple)) else list(row.keys())
					return Row({c: row.get(c) for c in cols})
				return row.get(fieldname)
			if dt == "Product Bundle":
				if isinstance(filters, dict):
					return self.bundles.get(filters.get("new_item_code"))
				return self.bundles.get(filters)
			if dt == "Workflow":
				return None
			if dt == "Branch" and fieldname == "company":
				return "Maximus Hospitality"
			if dt == "Company" and fieldname == "default_currency":
				return self.company_currency
			if dt == "Price List" and fieldname == "currency":
				return self.price_list_currencies.get(filters)
			if dt == "Currency" and fieldname == "fraction_units":
				prec = self.currency_precision.get(filters)
				return 10 ** prec if prec is not None else None
			if dt == "POS Profile" and fieldname == "cost_center":
				return "Main - MH"
			return None

		def get_list(dt, filters=None, fields=None, **kwargs):
			if dt == "Item Price":
				item_code = (filters or {}).get("item_code")
				price_list = (filters or {}).get("price_list")
				rate = self.item_prices.get((item_code, price_list))
				if rate is None:
					return []
				return [
					Row(
						name=f"IP-{item_code}",
						item_code=item_code,
						price_list=price_list,
						price_list_rate=rate,
						currency=self.item_price_currencies.get((item_code, price_list)),
						uom="Nos",
					)
				]
			return []

		framework.db.exists.side_effect = exists
		framework.db.table_exists.side_effect = table_exists
		framework.get_all.side_effect = get_all
		framework.db.get_all.side_effect = get_all
		framework.db.get_value.side_effect = get_value
		framework.db.get_list.side_effect = get_list
		framework.get_cached_value.side_effect = get_value


class TestModifiersAndCombosEngine(unittest.TestCase):
	def setUp(self):
		self.framework, self.modules = _make_frappe_double()
		self.db = _FakeModifierDB()
		self.db.wire(self.framework)

		# Items
		self.db.add_item("RIBEYE", "Ribeye Steak", item_group="Steaks")
		self.db.add_item("EXTRA-CHEESE", "Aged Cheddar", item_group="Dairy")
		self.db.add_item("GARLIC-BUTTER", "Garlic Butter", item_group="Sauces")
		self.db.add_item("RANDOM-WINE", "House Wine", item_group="Beverages")

		# Combo items
		self.db.add_item("BURGER-COMBO", "Burger Combo", item_group="Combos", is_stock_item=0)
		self.db.add_item("BURGER", "Classic Burger", item_group="Mains", is_stock_item=1)
		self.db.add_item("FRIES", "French Fries", item_group="Sides", is_stock_item=1)
		self.db.add_item("ONION-RINGS", "Onion Rings", item_group="Sides", is_stock_item=1)
		self.db.add_item("COLA", "Cola", item_group="Beverages", is_stock_item=1)
		self.db.add_item("MANGO-JUICE", "Fresh Mango Juice", item_group="Beverages", is_stock_item=1)
		self.db.add_item("COMBO-UPGRADE-FEE", "Combo Upgrade", item_group="Services", is_stock_item=0)

		# Prices (UGX 0dp default)
		self.db.add_price("RIBEYE", 65000, "Menu A")
		self.db.add_price("EXTRA-CHEESE", 3000, "Menu A")
		self.db.add_price("GARLIC-BUTTER", 2500, "Menu A")
		self.db.add_price("BURGER-COMBO", 25000, "Menu A")
		self.db.add_price("COMBO-UPGRADE-FEE", 1, "Menu A")

		# G3: Required Meat Temperature attached at Item Group = "Steaks" (inheritance!)
		self.db.add_group(
			"Meat Temperature",
			display_title="Meat Temperature",
			group_type="Condiment",
			required=1,
			prompt_on_add=1,
			min_selections=1,
			max_selections=1,
			allow_prefixes=0,
			allowed_prefixes="",
			inherit_parent_routing=1,
			hide_unpriced_on_receipt=1,
		)
		self.db.add_option("Meat Temperature", option_code="MED-RARE", label="Medium Rare", price_mode="Unpriced Prep")
		self.db.add_option("Meat Temperature", option_code="WELL-DONE", label="Well Done", price_mode="Unpriced Prep")
		self.db.attach_group("Item Group", "Steaks", "Meat Temperature", sort_order=1)

		# G3: Optional Condiments with prefixes & priced add-on attached on RIBEYE
		self.db.add_group(
			"Steak Extras",
			display_title="Steak Extras",
			group_type="Condiment",
			required=0,
			min_selections=0,
			max_selections=2,
			allow_prefixes=1,
			allowed_prefixes="No\nExtra\nSide\nLight\nOn Side",
			inherit_parent_routing=1,
			hide_unpriced_on_receipt=1,
		)
		self.db.add_option("Steak Extras", option_code="ONION", label="Onion", price_mode="Unpriced Prep")
		self.db.add_option(
			"Steak Extras",
			item="EXTRA-CHEESE",
			option_code="EXTRA-CHEESE",
			label="Cheese",
			price_mode="Menu Item Price",
		)
		self.db.add_option(
			"Steak Extras",
			item="GARLIC-BUTTER",
			option_code="GARLIC-BUTTER",
			label="Garlic Butter",
			price_mode="Menu Item Price",
		)
		self.db.attach_group("Item", "RIBEYE", "Steak Extras", sort_order=2)

		# G4: Native Product Bundle for BURGER-COMBO (defaults: BURGER, FRIES, COLA)
		self.db.add_bundle("BURGER-COMBO", [("BURGER", 1), ("FRIES", 1), ("COLA", 1)])
		self.db.add_group(
			"Choose Side",
			display_title="Choose Side",
			group_type="Combo Choice",
			required=1,
			prompt_on_add=1,
			min_selections=1,
			max_selections=1,
			allow_prefixes=0,
			inherit_parent_routing=1,
			hide_unpriced_on_receipt=0,
		)
		self.db.add_option(
			"Choose Side",
			item="FRIES",
			option_code="FRIES",
			label="French Fries",
			price_mode="Combo Upcharge",
			upcharge_amount=0,
			replaces_bundle_item="FRIES",
			is_default=1,
		)
		self.db.add_option(
			"Choose Side",
			item="ONION-RINGS",
			option_code="ONION-RINGS",
			label="Onion Rings",
			price_mode="Combo Upcharge",
			upcharge_amount=3000,
			upcharge_item="COMBO-UPGRADE-FEE",
			replaces_bundle_item="FRIES",
		)
		self.db.attach_group("Item", "BURGER-COMBO", "Choose Side", sort_order=1)

		self.db.add_group(
			"Choose Drink",
			display_title="Choose Drink",
			group_type="Combo Choice",
			required=1,
			prompt_on_add=1,
			min_selections=1,
			max_selections=1,
			allow_prefixes=0,
			inherit_parent_routing=0,
			hide_unpriced_on_receipt=0,
		)
		self.db.add_option(
			"Choose Drink",
			item="COLA",
			option_code="COLA",
			label="Cola",
			price_mode="Combo Upcharge",
			upcharge_amount=0,
			replaces_bundle_item="COLA",
			is_default=1,
		)
		self.db.add_option(
			"Choose Drink",
			item="MANGO-JUICE",
			option_code="MANGO-JUICE",
			label="Fresh Mango Juice",
			price_mode="Combo Upcharge",
			upcharge_amount=4000,
			upcharge_item="COMBO-UPGRADE-FEE",
			replaces_bundle_item="COLA",
		)
		self.db.attach_group("Item", "BURGER-COMBO", "Choose Drink", sort_order=2)

	def _load_engines(self):
		item_price = _load_module("ury.ury.api.item_price", "ury/api/item_price.py")
		brand = _load_module("ury.brand", "brand.py")
		bundle = _load_module("ury.ury.api.ury_bundle_resolver", "ury/api/ury_bundle_resolver.py")
		mods = _load_module("ury.ury.api.ury_modifiers", "ury/api/ury_modifiers.py")
		return item_price, brand, bundle, mods

	def test_item_group_inheritance_and_batched_metadata(self):
		with patch.dict(sys.modules, self.modules):
			_, _, _, mods = self._load_engines()
			meta = mods.get_modifier_groups_for_items(
				["RIBEYE", "BURGER-COMBO"], price_list="Menu A", branch="Main", company="Maximus Hospitality"
			)

			ribeye = meta["RIBEYE"]
			self.assertTrue(ribeye["has_required_modifiers"])
			self.assertTrue(ribeye["prompt_on_add"])
			group_names = [g["name"] for g in ribeye["modifier_groups"]]
			self.assertEqual(group_names, ["Meat Temperature", "Steak Extras"])
			extras = ribeye["modifier_groups"][1]
			cheese = next(o for o in extras["options"] if o["option_code"] == "EXTRA-CHEESE")
			self.assertEqual(cheese["unit_price"], 3000)

			combo = meta["BURGER-COMBO"]
			self.assertTrue(combo["is_combo_meal"])
			self.assertTrue(combo["has_required_modifiers"])
			self.assertEqual([d["item_code"] for d in combo["bundle_defaults"]], ["BURGER", "FRIES", "COLA"])

	def test_missing_required_modifier_is_refused_with_neutral_pos_code(self):
		with patch.dict(sys.modules, self.modules):
			_, _, _, mods = self._load_engines()
			items = [{"item": "RIBEYE", "item_name": "Ribeye Steak", "qty": 1, "reservation_line_key": "line-1"}]
			with self.assertRaises(self.framework.ValidationError) as ctx:
				mods.validate_and_normalize_order_items(
					items, [], "Menu A", "Menu A", "Main", "Maximus Hospitality"
				)
			msg = str(ctx.exception)
			self.assertIn("[POS-MODIFIER-MIN-REQUIRED]", msg)
			self.assertIsNone(FORBIDDEN_VENDOR_RE.search(msg), f"Vendor leak in refusal: {msg}")

	def test_max_selections_exceeded_is_refused(self):
		with patch.dict(sys.modules, self.modules):
			_, _, _, mods = self._load_engines()
			items = [
				{
					"item": "RIBEYE",
					"item_name": "Ribeye Steak",
					"qty": 1,
					"reservation_line_key": "line-1",
					"custom_modifiers_json": json.dumps([
						{"group": "Meat Temperature", "option_code": "MED-RARE", "label": "Medium Rare"},
						{"group": "Steak Extras", "option_code": "ONION", "label": "Onion", "prefix": "No"},
					]),
				},
				{
					"item": "EXTRA-CHEESE",
					"item_name": "Cheese",
					"qty": 1,
					"reservation_line_key": "line-1-m1",
					"custom_parent_line_key": "line-1",
					"custom_is_modifier": 1,
					"custom_line_role": "Modifier",
					"custom_modifier_group": "Steak Extras",
					"custom_modifier_option": "EXTRA-CHEESE",
				},
				{
					"item": "GARLIC-BUTTER",
					"item_name": "Garlic Butter",
					"qty": 1,
					"reservation_line_key": "line-1-m2",
					"custom_parent_line_key": "line-1",
					"custom_is_modifier": 1,
					"custom_line_role": "Modifier",
					"custom_modifier_group": "Steak Extras",
					"custom_modifier_option": "GARLIC-BUTTER",
				},
			]
			with self.assertRaises(self.framework.ValidationError) as ctx:
				mods.validate_and_normalize_order_items(
					items, [], "Menu A", "Menu A", "Main", "Maximus Hospitality"
				)
			self.assertIn("[POS-MODIFIER-MAX-EXCEEDED]", str(ctx.exception))

	def test_invalid_prefix_and_disallowed_modifier_and_orphan_child_are_refused(self):
		with patch.dict(sys.modules, self.modules):
			_, _, _, mods = self._load_engines()

			# Invalid prefix
			bad_prefix = [
				{
					"item": "RIBEYE",
					"qty": 1,
					"reservation_line_key": "line-1",
					"custom_modifiers_json": json.dumps([
						{"group": "Meat Temperature", "option_code": "MED-RARE", "label": "Medium Rare"},
						{"group": "Steak Extras", "option_code": "ONION", "label": "Onion", "prefix": "Triple"},
					]),
				}
			]
			with self.assertRaises(self.framework.ValidationError) as ctx:
				mods.validate_and_normalize_order_items(bad_prefix, [], "Menu A", "Menu A", "Main", "Maximus Hospitality")
			self.assertIn("[POS-MODIFIER-PREFIX-INVALID]", str(ctx.exception))

			# Disallowed item posed as modifier
			disallowed = [
				{
					"item": "RIBEYE",
					"qty": 1,
					"reservation_line_key": "line-1",
					"custom_modifiers_json": json.dumps([
						{"group": "Meat Temperature", "option_code": "MED-RARE", "label": "Medium Rare"},
					]),
				},
				{
					"item": "RANDOM-WINE",
					"qty": 1,
					"reservation_line_key": "line-1-bad",
					"custom_parent_line_key": "line-1",
					"custom_is_modifier": 1,
					"custom_line_role": "Modifier",
					"custom_modifier_group": "Steak Extras",
					"custom_modifier_option": "RANDOM-WINE",
				},
			]
			with self.assertRaises(self.framework.ValidationError) as ctx:
				mods.validate_and_normalize_order_items(disallowed, [], "Menu A", "Menu A", "Main", "Maximus Hospitality")
			self.assertIn("[POS-MODIFIER-NOT-ALLOWED]", str(ctx.exception))

			# Orphan modifier without parent in order
			orphan = [
				{
					"item": "EXTRA-CHEESE",
					"qty": 1,
					"reservation_line_key": "line-orphan",
					"custom_parent_line_key": "missing-parent",
					"custom_is_modifier": 1,
					"custom_line_role": "Modifier",
					"custom_modifier_group": "Steak Extras",
					"custom_modifier_option": "EXTRA-CHEESE",
				}
			]
			with self.assertRaises(self.framework.ValidationError) as ctx:
				mods.validate_and_normalize_order_items(orphan, [], "Menu A", "Menu A", "Main", "Maximus Hospitality")
			self.assertIn("[POS-MODIFIER-ORPHAN]", str(ctx.exception))

	def test_unpriced_prep_creates_zero_extra_rows_and_forged_rates_are_overridden_at_0_2_3dp(self):
		with patch.dict(sys.modules, self.modules):
			_, _, _, mods = self._load_engines()

			# 1. Unpriced prep only -> 1 row total, zero extra rows (EFRIS safe!), comment populated
			prep_only = [
				{
					"item": "RIBEYE",
					"item_name": "Ribeye Steak",
					"qty": 1,
					"reservation_line_key": "line-prep",
					"custom_modifiers_json": json.dumps([
						{"group": "Meat Temperature", "option_code": "MED-RARE", "label": "Medium Rare"},
						{"group": "Steak Extras", "option_code": "ONION", "label": "Onion", "prefix": "No"},
					]),
				}
			]
			normalized, overrides = mods.validate_and_normalize_order_items(
				prep_only, [], "Menu A", "Menu A", "Main", "Maximus Hospitality", currency_precision=0
			)
			self.assertEqual(len(normalized), 1)
			self.assertIn("Medium Rare", normalized[0]["comment"])
			self.assertIn("No Onion", normalized[0]["comment"])
			self.assertEqual(normalized[0]["custom_hide_unpriced_on_receipt"], 1)

			# 2. Priced modifier with forged rate=0.01 -> server enforces authoritative price across 0/2/3dp
			for currency, server_rate, precision in (("UGX", 3000.0, 0), ("USD", 1.5, 2), ("KWD", 0.375, 3)):
				with self.subTest(currency=currency):
					self.db.add_price("EXTRA-CHEESE", server_rate, f"PL-{currency}")
					payload = [
						{
							"item": "RIBEYE",
							"qty": 1,
							"reservation_line_key": "line-r",
							"custom_modifiers_json": json.dumps([
								{"group": "Meat Temperature", "option_code": "MED-RARE", "label": "Medium Rare"},
							]),
						},
						{
							"item": "EXTRA-CHEESE",
							"qty": 1,
							"rate": 0.001,  # forged by client!
							"reservation_line_key": "line-r-cheese",
							"custom_parent_line_key": "line-r",
							"custom_is_modifier": 1,
							"custom_line_role": "Modifier",
							"custom_modifier_group": "Steak Extras",
							"custom_modifier_option": "EXTRA-CHEESE",
							"custom_modifier_prefix": "Extra",
						},
					]
					norm, price_map = mods.validate_and_normalize_order_items(
						payload, [], "Menu A", f"PL-{currency}", "Main", "Maximus Hospitality", currency_precision=precision
					)
					self.assertEqual(price_map["line-r-cheese"]["rate"], round(server_rate, precision))
					self.assertEqual(norm[1]["custom_modifier_prefix"], "Extra")
					self.assertEqual(norm[1]["custom_inherit_parent_routing"], 1)

	def test_combo_meal_choices_upcharge_pricing_and_86_refusal(self):
		with patch.dict(sys.modules, self.modules):
			_, _, _, mods = self._load_engines()

			# Valid combo with Onion Rings (+3000) and Mango Juice (+4000), client tries rate=0 forgery
			combo_payload = [
				{
					"item": "BURGER-COMBO",
					"item_name": "Burger Combo",
					"qty": 1,
					"reservation_line_key": "combo-1",
					"custom_line_role": "Combo Header",
					"custom_bundle_overrides_json": json.dumps([
						{"group": "Choose Side", "slot_item": "FRIES", "chosen_item": "ONION-RINGS", "label": "Onion Rings", "qty": 1},
						{"group": "Choose Drink", "slot_item": "COLA", "chosen_item": "MANGO-JUICE", "label": "Fresh Mango Juice", "qty": 1},
					]),
				},
				{
					"item": "COMBO-UPGRADE-FEE",
					"item_name": "Upgrade: Onion Rings",
					"qty": 1,
					"rate": 0,  # forged!
					"reservation_line_key": "combo-1-side",
					"custom_parent_line_key": "combo-1",
					"custom_is_modifier": 1,
					"custom_line_role": "Combo Upcharge",
					"custom_modifier_group": "Choose Side",
					"custom_modifier_option": "ONION-RINGS",
				},
				{
					"item": "COMBO-UPGRADE-FEE",
					"item_name": "Upgrade: Fresh Mango Juice",
					"qty": 1,
					"rate": 100,  # forged!
					"reservation_line_key": "combo-1-drink",
					"custom_parent_line_key": "combo-1",
					"custom_is_modifier": 1,
					"custom_line_role": "Combo Upcharge",
					"custom_modifier_group": "Choose Drink",
					"custom_modifier_option": "MANGO-JUICE",
				},
			]
			norm, price_map = mods.validate_and_normalize_order_items(
				combo_payload, [], "Menu A", "Menu A", "Main", "Maximus Hospitality", currency_precision=0
			)
			self.assertEqual(price_map["combo-1-side"]["rate"], 3000)
			self.assertEqual(price_map["combo-1-drink"]["rate"], 4000)
			# Group-level routing preserved: Side inherits parent (1), Drink routes to Bar (0)
			self.assertEqual(norm[1]["custom_inherit_parent_routing"], 1)
			self.assertEqual(norm[2]["custom_inherit_parent_routing"], 0)
			parsed_overrides = json.loads(norm[0]["custom_bundle_overrides_json"])
			self.assertEqual(len(parsed_overrides), 2)
			self.assertEqual(parsed_overrides[1]["inherit_parent_routing"], 0)

			# Mark ONION-RINGS Sold Out (86'd) -> server refuses combo selection!
			self.db.availability[("Main", "ONION-RINGS")] = Row(
				branch="Main", item="ONION-RINGS", status="Sold Out", countdown_enabled=0, remaining_qty=0
			)
			with self.assertRaises(self.framework.ValidationError) as ctx:
				mods.validate_and_normalize_order_items(
					combo_payload, [], "Menu A", "Menu A", "Main", "Maximus Hospitality", currency_precision=0
				)
			self.assertIn("[POS-MODIFIER-UNAVAILABLE]", str(ctx.exception))

	def test_bundle_resolver_swaps_packed_items_on_sales_invoice_consolidation(self):
		with patch.dict(sys.modules, self.modules):
			_, _, bundle, _ = self._load_engines()

			overrides = [
				{"group": "Choose Side", "slot_item": "FRIES", "chosen_item": "ONION-RINGS", "qty": 1, "inherit_parent_routing": 1},
				{"group": "Choose Drink", "slot_item": "COLA", "chosen_item": "MANGO-JUICE", "qty": 1, "inherit_parent_routing": 0},
			]
			resolved = bundle.resolve_bundle_components("BURGER-COMBO", branch="Main", company="Maximus Hospitality", overrides=overrides, qty=2)
			self.assertEqual(
				[(r["item_code"], r["qty"]) for r in resolved],
				[("BURGER", 2.0), ("ONION-RINGS", 2.0), ("MANGO-JUICE", 2.0)],
			)

			# Simulate Sales Invoice after ERPNext make_packing_list() populated defaults (BURGER, FRIES, COLA)
			si_item = Row(
				name="SII-1",
				item_code="BURGER-COMBO",
				qty=2,
				warehouse="Kitchen Store - MH",
				custom_bundle_overrides_json=json.dumps(overrides),
			)
			packed_rows = [
				Row(parent_item="BURGER-COMBO", parent_detail_docname="SII-1", item_code="BURGER", item_name="Classic Burger", qty=2, warehouse="Kitchen Store - MH", uom="Nos"),
				Row(parent_item="BURGER-COMBO", parent_detail_docname="SII-1", item_code="FRIES", item_name="French Fries", qty=2, warehouse="Kitchen Store - MH", uom="Nos"),
				Row(parent_item="BURGER-COMBO", parent_detail_docname="SII-1", item_code="COLA", item_name="Cola", qty=2, warehouse="Kitchen Store - MH", uom="Nos"),
			]
			si_doc = Doc(
				doctype="Sales Invoice",
				company="Maximus Hospitality",
				branch="Main",
				update_stock=1,
				items=[si_item],
				packed_items=packed_rows,
			)
			bundle.apply_bundle_overrides_to_packed_items(si_doc)

			packed_codes = [r.item_code for r in si_doc.packed_items]
			self.assertEqual(packed_codes, ["BURGER", "ONION-RINGS", "MANGO-JUICE"])
			self.assertNotIn("FRIES", packed_codes)
			self.assertNotIn("COLA", packed_codes)
			self.assertEqual([r.qty for r in si_doc.packed_items], [2.0, 2.0, 2.0])

	def test_apply_bundle_overrides_is_idempotent_across_repeated_saves_and_submit(self):
		"""R1 H1 RED: ERPNext make_packing_list() keeps swapped rows and re-appends default bundle
		slots on every draft save / submit; apply_bundle_overrides_to_packed_items must stay 1:1."""
		with patch.dict(sys.modules, self.modules):
			_, _, bundle, _ = self._load_engines()

			overrides = [
				{"group": "Choose Side", "slot_item": "FRIES", "chosen_item": "ONION-RINGS", "qty": 1, "inherit_parent_routing": 1},
				{"group": "Choose Drink", "slot_item": "COLA", "chosen_item": "MANGO-JUICE", "qty": 1, "inherit_parent_routing": 0},
			]
			inv_item = Row(
				name="PII-COMBO-1",
				item_code="BURGER-COMBO",
				qty=1,
				warehouse="Kitchen Store - MH",
				custom_bundle_overrides_json=json.dumps(overrides),
			)
			doc = Doc(
				doctype="POS Invoice",
				company="Maximus Hospitality",
				branch="Main",
				update_stock=1,
				items=[inv_item],
				packed_items=[
					Row(parent_item="BURGER-COMBO", parent_detail_docname="PII-COMBO-1", item_code="BURGER", item_name="Classic Burger", qty=1, warehouse="Kitchen Store - MH", uom="Nos"),
					Row(parent_item="BURGER-COMBO", parent_detail_docname="PII-COMBO-1", item_code="FRIES", item_name="French Fries", qty=1, warehouse="Kitchen Store - MH", uom="Nos"),
					Row(parent_item="BURGER-COMBO", parent_detail_docname="PII-COMBO-1", item_code="COLA", item_name="Cola", qty=1, warehouse="Kitchen Store - MH", uom="Nos"),
				],
			)

			# Save 1
			bundle.apply_bundle_overrides_to_packed_items(doc)
			self.assertEqual(
				[(r.item_code, r.qty) for r in doc.packed_items],
				[("BURGER", 1.0), ("ONION-RINGS", 1.0), ("MANGO-JUICE", 1.0)],
			)

			# Save 2: simulate ERPNext make_packing_list() keeping swapped rows + re-adding default FRIES & COLA
			doc.packed_items.extend([
				Row(parent_item="BURGER-COMBO", parent_detail_docname="PII-COMBO-1", item_code="FRIES", item_name="French Fries", qty=1, warehouse="Kitchen Store - MH", uom="Nos"),
				Row(parent_item="BURGER-COMBO", parent_detail_docname="PII-COMBO-1", item_code="COLA", item_name="Cola", qty=1, warehouse="Kitchen Store - MH", uom="Nos"),
			])
			bundle.apply_bundle_overrides_to_packed_items(doc)
			self.assertEqual(
				[(r.item_code, r.qty) for r in doc.packed_items],
				[("BURGER", 1.0), ("ONION-RINGS", 1.0), ("MANGO-JUICE", 1.0)],
			)

			# Save 3 / submit with qty bumped 1 -> 2 and ERPNext regenerating default slots at qty=2
			inv_item.qty = 2
			doc.packed_items[0].qty = 2
			doc.packed_items.extend([
				Row(parent_item="BURGER-COMBO", parent_detail_docname="PII-COMBO-1", item_code="FRIES", item_name="French Fries", qty=2, warehouse="Kitchen Store - MH", uom="Nos"),
				Row(parent_item="BURGER-COMBO", parent_detail_docname="PII-COMBO-1", item_code="COLA", item_name="Cola", qty=2, warehouse="Kitchen Store - MH", uom="Nos"),
			])
			bundle.apply_bundle_overrides_to_packed_items(doc)
			self.assertEqual(
				[(r.item_code, r.qty) for r in doc.packed_items],
				[("BURGER", 2.0), ("ONION-RINGS", 2.0), ("MANGO-JUICE", 2.0)],
			)

			# Negative-qty return keeps exact -1.0 swapped components
			inv_item.qty = -1
			doc.packed_items = [
				Row(parent_item="BURGER-COMBO", parent_detail_docname="PII-COMBO-1", item_code="BURGER", item_name="Classic Burger", qty=-1, warehouse="Kitchen Store - MH", uom="Nos"),
				Row(parent_item="BURGER-COMBO", parent_detail_docname="PII-COMBO-1", item_code="FRIES", item_name="French Fries", qty=-1, warehouse="Kitchen Store - MH", uom="Nos"),
				Row(parent_item="BURGER-COMBO", parent_detail_docname="PII-COMBO-1", item_code="COLA", item_name="Cola", qty=-1, warehouse="Kitchen Store - MH", uom="Nos"),
			]
			bundle.apply_bundle_overrides_to_packed_items(doc)
			self.assertEqual(
				[(r.item_code, r.qty) for r in doc.packed_items],
				[("BURGER", -1.0), ("ONION-RINGS", -1.0), ("MANGO-JUICE", -1.0)],
			)

	def test_pos_invoice_validate_and_before_submit_block_rest_modifier_and_bundle_bypasses(self):
		"""R1 H2 RED: Direct REST POST/PUT and frappe.client.submit must enforce all modifier & bundle rules."""
		self.db.add_item("AVOCADO", "Smoked Avocado", item_group="Sides")
		self.db.add_price("AVOCADO", 5000, "Menu A")
		self.db.add_option(
			"Steak Extras",
			item="AVOCADO",
			option_code="AVOCADO",
			label="Smoked Avocado",
			price_mode="Menu Item Price",
		)
		self.db.availability[("Main", "AVOCADO")] = Row(
			branch="Main", item="AVOCADO", status="Sold Out", countdown_enabled=0, remaining_qty=0
		)

		extra_modules = {
			**self.modules,
			"ury.ury.doctype.ury_order.ury_order": _stub(
				"ury.ury.doctype.ury_order.ury_order", release_merge_cluster_tables=MagicMock()
			),
			"ury.ury.doctype.staff_discount_policy.staff_discount_policy": _stub(
				"ury.ury.doctype.staff_discount_policy.staff_discount_policy",
				get_applicable_policy=MagicMock(return_value=None),
			),
			"ury.ury_pos.discount": _stub(
				"ury.ury_pos.discount",
				prorate_return_discount=MagicMock(),
				validate_pos_discount=MagicMock(),
			),
		}
		extra_modules["frappe.utils"].now = lambda: "2026-11-24 14:00:00"
		extra_modules["frappe.utils"].get_time = lambda v=None: v
		extra_modules["frappe.utils"].get_first_day = lambda v=None: v
		extra_modules["frappe.utils"].get_last_day = lambda v=None: v

		with patch.dict(sys.modules, extra_modules):
			self._load_engines()
			hooks = _load_module("ury.ury.hooks.ury_pos_invoice", "ury/hooks/ury_pos_invoice.py")
			self.framework.db.sql.return_value = []

			def make_doc(items, packed_items=None):
				doc = Doc(
					name="REST-BYPASS-1",
					doctype="POS Invoice",
					company="Maximus Hospitality",
					branch="Main",
					restaurant=None,
					pos_profile="Main POS",
					customer="Walk-in",
					customer_name="Walk-in",
					waiter="cashier@maximus.test",
					cashier="cashier@maximus.test",
					posting_date="2026-11-24",
					selling_price_list="Menu A",
					currency="UGX",
					is_return=0,
					docstatus=0,
					invoice_printed=1,
					restaurant_table=None,
					items=list(items),
					packed_items=list(packed_items or []),
					payments=[],
					flags=SimpleNamespace(),
				)
				doc.is_new = lambda: True
				doc.precision = lambda field=None: 0
				return doc

			def assert_blocked_both(doc, expected_code):
				with self.assertRaises(self.framework.ValidationError) as ctx_v:
					hooks.validate(doc, "validate")
				self.assertIn(expected_code, str(ctx_v.exception), f"validate missed {expected_code}")
				self.assertIsNone(FORBIDDEN_VENDOR_RE.search(str(ctx_v.exception)))

				with self.assertRaises(self.framework.ValidationError) as ctx_s:
					hooks.before_submit(doc, "before_submit")
				self.assertIn(expected_code, str(ctx_s.exception), f"before_submit missed {expected_code}")
				self.assertIsNone(FORBIDDEN_VENDOR_RE.search(str(ctx_s.exception)))

			# 1. Missing required Meat Temperature on RIBEYE
			assert_blocked_both(
				make_doc([Row(name="r1", item_code="RIBEYE", item_name="Ribeye Steak", qty=1, rate=65000, reservation_line_key="k1")]),
				"[POS-MODIFIER-MIN-REQUIRED]",
			)

			# 2a. max_selections exceeded (3 separate EXTRA-CHEESE rows > max 2)
			ribeye_ok = Row(
				name="r1",
				item_code="RIBEYE",
				item_name="Ribeye Steak",
				qty=1,
				rate=65000,
				reservation_line_key="k1",
				custom_modifiers_json=json.dumps([{"group": "Meat Temperature", "option_code": "MED-RARE", "label": "Medium Rare"}]),
			)
			three_cheese_rows = [
				Row(
					name=f"c{i}",
					item_code="EXTRA-CHEESE",
					item_name="Aged Cheddar",
					qty=1,
					rate=3000,
					reservation_line_key=f"kc{i}",
					custom_parent_line_key="k1",
					custom_is_modifier=1,
					custom_line_role="Modifier",
					custom_modifier_group="Steak Extras",
					custom_modifier_option="EXTRA-CHEESE",
				)
				for i in range(3)
			]
			assert_blocked_both(make_doc([ribeye_ok, *three_cheese_rows]), "[POS-MODIFIER-MAX-EXCEEDED]")

			# 2b. max_selections exceeded via single row qty=3 on qty=1 parent
			assert_blocked_both(
				make_doc([
					ribeye_ok,
					Row(
						name="c-big",
						item_code="EXTRA-CHEESE",
						item_name="Aged Cheddar",
						qty=3,
						rate=3000,
						reservation_line_key="kc-big",
						custom_parent_line_key="k1",
						custom_is_modifier=1,
						custom_line_role="Modifier",
						custom_modifier_group="Steak Extras",
						custom_modifier_option="EXTRA-CHEESE",
					),
				]),
				"[POS-MODIFIER-MAX-EXCEEDED]",
			)

			# 3. 86'd (Sold Out) AVOCADO modifier
			assert_blocked_both(
				make_doc([
					ribeye_ok,
					Row(
						name="c-avo",
						item_code="AVOCADO",
						item_name="Smoked Avocado",
						qty=1,
						rate=5000,
						reservation_line_key="kc-avo",
						custom_parent_line_key="k1",
						custom_is_modifier=1,
						custom_line_role="Modifier",
						custom_modifier_group="Steak Extras",
						custom_modifier_option="AVOCADO",
					),
				]),
				"[POS-MODIFIER-UNAVAILABLE]",
			)

			# 4. Forged modifier rate=0.01 on available EXTRA-CHEESE (server price 3000)
			assert_blocked_both(
				make_doc([
					ribeye_ok,
					Row(
						name="c-forge",
						item_code="EXTRA-CHEESE",
						item_name="Aged Cheddar",
						qty=1,
						rate=0.01,
						price_list_rate=3000,
						reservation_line_key="kc-forge",
						custom_parent_line_key="k1",
						custom_is_modifier=1,
						custom_line_role="Modifier",
						custom_modifier_group="Steak Extras",
						custom_modifier_option="EXTRA-CHEESE",
					),
				]),
				"[POS-MODIFIER-RATE-MISMATCH]",
			)

			# 5. Forged Product Bundle swap: RIBEYE swapped into FRIES slot on BURGER-COMBO
			forged_combo_doc = make_doc(
				[
					Row(
						name="r-combo",
						item_code="BURGER-COMBO",
						item_name="Burger Combo",
						qty=1,
						rate=25000,
						reservation_line_key="k-combo",
						custom_line_role="Combo Header",
						custom_bundle_overrides_json=json.dumps([
							{"group": "Choose Side", "slot_item": "FRIES", "chosen_item": "RIBEYE", "qty": 1},
							{"group": "Choose Drink", "slot_item": "COLA", "chosen_item": "COLA", "qty": 1},
						]),
					)
				],
				packed_items=[
					Row(parent_item="BURGER-COMBO", parent_detail_docname="r-combo", item_code="BURGER", qty=1),
					Row(parent_item="BURGER-COMBO", parent_detail_docname="r-combo", item_code="FRIES", qty=1),
					Row(parent_item="BURGER-COMBO", parent_detail_docname="r-combo", item_code="COLA", qty=1),
				],
			)
			assert_blocked_both(forged_combo_doc, "[POS-MODIFIER-NOT-ALLOWED]")
			self.assertNotIn("RIBEYE", [p.item_code for p in forged_combo_doc.packed_items])

			# 6. Valid invoice passes both validate and before_submit cleanly
			valid_doc = make_doc([
				ribeye_ok,
				Row(
					name="c-ok",
					item_code="EXTRA-CHEESE",
					item_name="Aged Cheddar",
					qty=1,
					rate=3000,
					price_list_rate=3000,
					reservation_line_key="kc-ok",
					custom_parent_line_key="k1",
					custom_is_modifier=1,
					custom_line_role="Modifier",
					custom_modifier_group="Steak Extras",
					custom_modifier_option="EXTRA-CHEESE",
					custom_modifier_prefix="Extra",
				),
				Row(
					name="r-combo-ok",
					item_code="BURGER-COMBO",
					item_name="Burger Combo",
					qty=1,
					rate=25000,
					reservation_line_key="k-combo-ok",
					custom_line_role="Combo Header",
					custom_bundle_overrides_json=json.dumps([
						{"group": "Choose Side", "slot_item": "FRIES", "chosen_item": "ONION-RINGS", "option_code": "ONION-RINGS", "label": "Onion Rings", "qty": 1},
						{"group": "Choose Drink", "slot_item": "COLA", "chosen_item": "COLA", "option_code": "COLA", "label": "Cola", "qty": 1},
					]),
				),
				Row(
					name="c-fee-ok",
					item_code="COMBO-UPGRADE-FEE",
					item_name="Onion Rings",
					qty=1,
					rate=3000,
					price_list_rate=3000,
					reservation_line_key="k-fee-ok",
					custom_parent_line_key="k-combo-ok",
					custom_is_modifier=1,
					custom_line_role="Combo Upcharge",
					custom_modifier_group="Choose Side",
					custom_modifier_option="ONION-RINGS",
				),
			])
			hooks.validate(valid_doc, "validate")
			hooks.before_submit(valid_doc, "before_submit")

	def test_combo_upcharge_resolves_multi_currency_item_price_at_0_2_3dp(self):
		"""R1 M1 RED: Combo Upcharge resolves currency-aware Item Price for UGX (0dp), USD (2dp), KWD (3dp)
		with safe fallback to upcharge_amount when no Item Price exists."""
		with patch.dict(sys.modules, self.modules):
			_, _, _, mods = self._load_engines()

			self.db.add_price("COMBO-UPGRADE-FEE", 3000, "PL-UGX", currency="UGX")
			self.db.add_price("COMBO-UPGRADE-FEE", 1.50, "PL-USD", currency="USD")
			self.db.add_price("COMBO-UPGRADE-FEE", 0.375, "PL-KWD", currency="KWD")
			self.db.price_list_currencies["PL-UGX-NO-IP"] = "UGX"

			cases = (
				("PL-UGX", "UGX", 0, 3000.0),
				("PL-USD", "USD", 2, 1.5),
				("PL-KWD", "KWD", 3, 0.375),
				("PL-UGX-NO-IP", "UGX", 0, 3000.0),
			)
			for price_list, currency, precision, expected_fee in cases:
				with self.subTest(price_list=price_list, currency=currency):
					meta = mods.get_modifier_groups_for_items(
						["BURGER-COMBO"], price_list=price_list, branch="Main", company="Maximus Hospitality"
					)
					side_grp = next(g for g in meta["BURGER-COMBO"]["modifier_groups"] if g["name"] == "Choose Side")
					onion_opt = next(o for o in side_grp["options"] if o["option_code"] == "ONION-RINGS")
					fries_opt = next(o for o in side_grp["options"] if o["option_code"] == "FRIES")
					self.assertEqual(onion_opt["unit_price"], expected_fee)
					self.assertEqual(fries_opt["unit_price"], 0.0)

					payload = [
						{
							"item": "BURGER-COMBO",
							"item_name": "Burger Combo",
							"qty": 1,
							"reservation_line_key": f"c-{currency}",
							"custom_line_role": "Combo Header",
							"custom_bundle_overrides_json": json.dumps([
								{"group": "Choose Side", "slot_item": "FRIES", "chosen_item": "ONION-RINGS", "label": "Onion Rings", "qty": 1},
								{"group": "Choose Drink", "slot_item": "COLA", "chosen_item": "COLA", "label": "Cola", "qty": 1},
							]),
						},
						{
							"item": "COMBO-UPGRADE-FEE",
							"item_name": "Onion Rings",
							"qty": 1,
							"rate": 9999,
							"reservation_line_key": f"c-{currency}-fee",
							"custom_parent_line_key": f"c-{currency}",
							"custom_is_modifier": 1,
							"custom_line_role": "Combo Upcharge",
							"custom_modifier_group": "Choose Side",
							"custom_modifier_option": "ONION-RINGS",
						},
					]
					norm, price_map = mods.validate_and_normalize_order_items(
						payload, [], "Menu A", price_list, "Main", "Maximus Hospitality", currency_precision=precision
					)
					self.assertEqual(price_map[f"c-{currency}-fee"]["rate"], expected_fee)
					self.assertEqual(norm[1]["rate"], expected_fee)


class TestSplitBillAtomicParentChildModifiers(unittest.TestCase):
	def test_split_bill_moves_child_modifiers_with_parent_and_blocks_orphan_child_split(self):
		framework, modules = _make_frappe_double()
		# Stub dependencies needed by ury_order
		modules.update({
			"ury.ury_pos.api": _stub(
				"ury.ury_pos.api",
				getBranch=MagicMock(return_value="Main"),
				getBranchRoom=MagicMock(),
				getRoom=MagicMock(return_value=[Row(name="Dining")]),
				posOpening=MagicMock(),
			),
			"ury.ury_pos.default_customer": _stub(
				"ury.ury_pos.default_customer",
				get_default_customer_config=MagicMock(),
				resolve_default_customer=MagicMock(),
			),
			"ury.ury.api.ury_kot_generate": _stub(
				"ury.ury.api.ury_kot_generate",
				_execute_kot=MagicMock(),
				process_items_for_cancel_kot=MagicMock(),
			),
			"ury.ury.api.ury_order_reservation_service": _stub(
				"ury.ury.api.ury_order_reservation_service",
				_line_key=lambda item_code, row, occ: f"ref:{item_code}:{row.get('reservation_line_key') or occ}",
				_line_ref=lambda row: row.get("reservation_line_key"),
				reconcile_order_reservations=MagicMock(),
				release_order_reservations=MagicMock(),
				resolve_production_context=MagicMock(return_value=None),
				_warehouse_for_context=MagicMock(),
			),
			"ury.ury.api.ury_stock_policy": _stub(
				"ury.ury.api.ury_stock_policy",
				get_branch_stock_policy=MagicMock(return_value=Row(reservation_control_enabled=False)),
			),
			"ury.ury.doctype.alert_settings.alert_settings": _stub(
				"ury.ury.doctype.alert_settings.alert_settings", get_alert_rule=MagicMock()
			),
			"ury.ury.api.ury_kot_notification": _stub(
				"ury.ury.api.ury_kot_notification",
				create_system_notification=MagicMock(),
				get_users_with_role=MagicMock(),
			),
			"ury.ury.hooks.ury_pos_tender": _stub("ury.ury.hooks.ury_pos_tender", sync_charged_tip=MagicMock()),
		})

		with patch.dict(sys.modules, modules):
			_load_module("ury.brand", "brand.py")
			_load_module("ury.ury.api.item_price", "ury/api/item_price.py")
			ury_order = _load_module("ury.ury.doctype.ury_order.ury_order", "ury/doctype/ury_order/ury_order.py")

			def make_source():
				parent1 = Row(
					name="ROW-P1",
					item_code="RIBEYE",
					item_name="Ribeye Steak",
					qty=1,
					rate=65000,
					price_list_rate=65000,
					base_price_list_rate=65000,
					cost_center="CC",
					uom="Nos",
					conversion_factor=1,
					warehouse="WH",
					reservation_line_key="key-p1",
					custom_parent_line_key=None,
					custom_is_modifier=0,
					custom_line_role="Standard",
					custom_modifiers_json='[{"group":"Meat Temperature","option_code":"MED-RARE","label":"Medium Rare"}]',
				)
				child1 = Row(
					name="ROW-C1",
					item_code="EXTRA-CHEESE",
					item_name="Cheese",
					qty=1,
					rate=3000,
					price_list_rate=3000,
					base_price_list_rate=3000,
					cost_center="CC",
					uom="Nos",
					conversion_factor=1,
					warehouse="WH",
					reservation_line_key="key-c1",
					custom_parent_line_key="key-p1",
					custom_is_modifier=1,
					custom_line_role="Modifier",
					custom_modifier_prefix="Extra",
					custom_inherit_parent_routing=1,
				)
				parent2 = Row(
					name="ROW-P2",
					item_code="COLA",
					item_name="Cola",
					qty=1,
					rate=5000,
					price_list_rate=5000,
					base_price_list_rate=5000,
					cost_center="CC",
					uom="Nos",
					conversion_factor=1,
					warehouse="WH",
					reservation_line_key="key-p2",
					custom_parent_line_key=None,
					custom_is_modifier=0,
					custom_line_role="Standard",
				)
				src = Doc(
					name="POS-001",
					docstatus=0,
					branch="Main",
					company="Maximus Hospitality",
					pos_profile="Main POS",
					waiter="waiter@maximus.test",
					invoice_printed=0,
					items=[parent1, child1, parent2],
					payments=[Row(mode_of_payment="Cash", amount=0, custom_tender_received=0)],
				)
				src.remove = lambda row: src.items.remove(row)
				src.set_missing_values = MagicMock()
				src.run_method = MagicMock()
				src.calculate_taxes_and_totals = MagicMock()
				src.save = MagicMock()
				return src

			profile = Doc(
				name="Main POS",
				transfer_role_permissions=[Row(role="URY Cashier")],
				role_allowed_for_billing=[Row(role="URY Cashier")],
			)
			created = []

			def new_doc(dt):
				doc = Doc(doctype=dt, name="POS-002", items=[], payments=[], invoice_created=0)
				doc.set = lambda k, v: setattr(doc, k, v)
				doc.append = lambda k, v: getattr(doc, k).append(Row(**v))
				doc.set_missing_values = MagicMock()
				doc.run_method = MagicMock()
				doc.calculate_taxes_and_totals = MagicMock()
				doc.insert = MagicMock()
				doc.reload = MagicMock()
				created.append(doc)
				return doc

			src = make_source()
			framework.get_doc.side_effect = lambda dt, name=None: src if dt == "POS Invoice" else profile
			framework.new_doc.side_effect = new_doc
			framework.get_all.return_value = []
			framework.db.get_value.return_value = "Maximus Hospitality"

			# 1. Trying to split child modifier row alone without its parent is refused!
			with self.assertRaises(framework.ValidationError) as ctx:
				ury_order.split_bill("POS-001", [{"name": "ROW-C1", "qty": 1}])
			self.assertIn("[POS-MODIFIER-SPLIT-WITH-PARENT]", str(ctx.exception))

			# 2. Moving parent ROW-P1 automatically moves linked child ROW-C1 together!
			src = make_source()
			created.clear()
			res = ury_order.split_bill("POS-001", [{"name": "ROW-P1", "qty": 1}])
			self.assertEqual(res["new_invoice"], "POS-002")
			new_inv = created[0]
			self.assertEqual([r.item_code for r in new_inv.items], ["RIBEYE", "EXTRA-CHEESE"])
			self.assertEqual(new_inv.items[1].custom_parent_line_key, new_inv.items[0].reservation_line_key)
			self.assertEqual(new_inv.items[1].custom_is_modifier, 1)
			self.assertEqual(new_inv.items[1].custom_modifier_prefix, "Extra")
			# Remaining source has only COLA
			self.assertEqual([r.item_code for r in src.items], ["COLA"])


if __name__ == "__main__":
	unittest.main()
