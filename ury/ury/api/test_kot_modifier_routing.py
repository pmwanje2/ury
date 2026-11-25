"""Site-less tests for G3/G4 KOT parent routing inheritance (Simphony Opt 22),
combo component routing split, and #146 print format indentation & unpriced suppression (Opt 79).
"""

import importlib.util
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

APP_ROOT = Path(__file__).resolve().parents[2]  # ury/ury/api -> ury/
PRINT_DIR = APP_ROOT / "ury" / "print_format"


class Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)

	def __getitem__(self, key):
		return getattr(self, key)


class Row(dict):
	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError:
			return None

	def __setattr__(self, key, value):
		self[key] = value


def _stub(name, **attrs):
	mod = ModuleType(name)
	for k, v in attrs.items():
		setattr(mod, k, v)
	return mod


def _load(dotted, rel_path):
	path = APP_ROOT / rel_path
	spec = importlib.util.spec_from_file_location(dotted, path)
	mod = importlib.util.module_from_spec(spec)
	sys.modules[dotted] = mod
	spec.loader.exec_module(mod)
	return mod


class TestKOTModifierParentRoutingAndComboSplit(unittest.TestCase):
	def setUp(self):
		self.framework = MagicMock()
		self.framework._ = lambda s: s
		self.framework.ValidationError = ValueError
		self.framework.PermissionError = PermissionError
		self.framework.whitelist = lambda *a, **kw: (lambda fn: fn)
		self.framework.has_permission.return_value = True
		self.framework.get_precision.return_value = 3
		self.framework.throw.side_effect = lambda msg, exc=ValueError: (_ for _ in ()).throw(exc(msg))

		utils = _stub(
			"frappe.utils",
			cint=lambda v: int(v or 0),
			flt=lambda v, p=None: round(float(v or 0), p or 9),
			get_datetime=lambda v: v,
		)
		self.tickets = []
		self.routing_calls = []

		# Production routing: RIBEYE -> Hot Kitchen, BURGER-COMBO -> Hot Kitchen,
		# ONION-RINGS -> Hot Kitchen, MANGO-JUICE -> Bar, EXTRA-CHEESE -> Dairy Prep (DIFFERENT!)
		self.station_map = {
			"RIBEYE": ["Hot Kitchen"],
			"BURGER-COMBO": ["Hot Kitchen"],
			"BURGER": ["Hot Kitchen"],
			"ONION-RINGS": ["Hot Kitchen"],
			"FRIES": ["Hot Kitchen"],
			"MANGO-JUICE": ["Bar"],
			"COLA": ["Bar"],
			"EXTRA-CHEESE": ["Cold Larder"],  # Distinct station — must be overridden by Opt 22!
		}

		def resolve_units(item_code, company, branch, **kwargs):
			self.routing_calls.append(item_code)
			return list(self.station_map.get(item_code, []))

		self.routing_mod = _stub("ury.ury.api.ury_kot_routing", resolve_production_units=resolve_units)
		self.prod_ctx_mod = _stub("ury.ury.api.ury_production_context", resolve_production_context=MagicMock(return_value=None))
		self.waiter_print_mod = _stub("ury.ury.api.ury_waiter_print", print_combined_waiter_order_slip=MagicMock())
		self.mto_mod = _stub("ury.ury.api.ury_mto_work_order_service", create_work_orders_for_kot=MagicMock())

		self.pos_profile = Row(name="Main POS", branch="Main", custom_kot_naming_series="KOT-.#####")
		self.pos_invoice = Doc(
			name="POS-009",
			company="Maximus Hospitality",
			branch="Main",
			pos_profile="Main POS",
			order_type="Dine In",
			custom_ury_order_number="109",
			custom_merged_tables="",
			custom_guest_label="Table 4",
			custom_aggregator_id=None,
			items=[],
		)

		def get_doc(dt, name=None):
			if isinstance(dt, dict):
				ticket = Row(**dt, name=f"KOT-{len(self.tickets) + 1}", kot_items=[])
				ticket.append = lambda field, row: ticket.kot_items.append(Row(**row))
				ticket.insert = MagicMock()
				ticket.submit = MagicMock()
				self.tickets.append(ticket)
				return ticket
			if dt == "POS Profile":
				return self.pos_profile
			if dt == "POS Invoice":
				return self.pos_invoice
			if dt == "URY KOT":
				for t in self.tickets:
					if t.name == name:
						return t
			return Row(name=name)

		def get_value(dt, filters, field=None, **kwargs):
			if dt == "Item":
				item_meta = {
					"RIBEYE": {"item_name": "Ribeye Steak", "item_group": "Steaks", "is_stock_item": 1},
					"EXTRA-CHEESE": {"item_name": "Cheese", "item_group": "Dairy", "is_stock_item": 1},
					"BURGER-COMBO": {"item_name": "Burger Combo", "item_group": "Combos", "is_stock_item": 0},
					"BURGER": {"item_name": "Classic Burger", "item_group": "Mains", "is_stock_item": 1},
					"FRIES": {"item_name": "French Fries", "item_group": "Sides", "is_stock_item": 1},
					"ONION-RINGS": {"item_name": "Onion Rings", "item_group": "Sides", "is_stock_item": 1},
					"COLA": {"item_name": "Cola", "item_group": "Beverages", "is_stock_item": 1},
					"MANGO-JUICE": {"item_name": "Fresh Mango Juice", "item_group": "Beverages", "is_stock_item": 1},
					"COMBO-UPGRADE-FEE": {"item_name": "Combo Upgrade", "item_group": "Services", "is_stock_item": 0},
				}.get(filters, {})
				if isinstance(field, (list, tuple)):
					return Row({k: item_meta.get(k) for k in field})
				return item_meta.get(field)
			if dt == "Product Bundle":
				return "BURGER-COMBO" if (filters == {"new_item_code": "BURGER-COMBO"} or filters == "BURGER-COMBO") else None
			if dt == "URY Restaurant":
				return "Main Menu"
			if dt == "URY Menu Item":
				return "Mains"
			return None

		def get_all(dt, filters=None, **kwargs):
			if dt == "URY Production Unit":
				return [Row(name="Hot Kitchen"), Row(name="Bar"), Row(name="Cold Larder")]
			if dt == "Product Bundle Item" and (filters or {}).get("parent") == "BURGER-COMBO":
				return [
					Row(item_code="BURGER", qty=1, uom="Nos", description="Classic Burger"),
					Row(item_code="FRIES", qty=1, uom="Nos", description="French Fries"),
					Row(item_code="COLA", qty=1, uom="Nos", description="Cola"),
				]
			return []

		self.framework.get_doc.side_effect = get_doc
		self.framework.db.get_value.side_effect = get_value
		self.framework.db.get_all.side_effect = get_all
		self.framework.get_all.side_effect = get_all
		self.framework.db.exists.side_effect = lambda dt, f=None: dt == "Product Bundle" and (f == "BURGER-COMBO" or f == {"new_item_code": "BURGER-COMBO"})
		self.framework.db.get_list.return_value = []

		self.modules = {
			"frappe": self.framework,
			"frappe.utils": utils,
			"ury.ury.api.ury_kot_routing": self.routing_mod,
			"ury.ury.api.ury_production_context": self.prod_ctx_mod,
			"ury.ury.api.ury_waiter_print": self.waiter_print_mod,
			"ury.ury.api.ury_mto_work_order_service": self.mto_mod,
			"ury.ury.api.ury_order_reservation_service": _stub(
				"ury.ury.api.ury_order_reservation_service",
				_line_ref=lambda row: row.get("reservation_line_key"),
				_line_context=lambda row: {"comments": row.get("comments", "")},
				_line_key=lambda item_code, row, occ: f"ref:{item_code}:{row.get('reservation_line_key') or occ}",
			),
		}

	def test_opt_22_priced_modifier_inherits_parent_kot_station_and_orders_beneath_parent(self):
		with patch.dict(sys.modules, self.modules):
			_load("ury.ury.api.ury_bundle_resolver", "ury/api/ury_bundle_resolver.py")
			kot_gen = _load("ury.ury.api.ury_kot_generate", "ury/api/ury_kot_generate.py")

			items = [
				{
					"item": "RIBEYE",
					"item_name": "Ribeye Steak",
					"qty": 1,
					"comments": "Medium Rare · No Onion",
					"reservation_line_key": "line-steak-1",
					"custom_is_modifier": 0,
					"custom_modifiers_json": json.dumps([
						{"group": "Meat Temperature", "option_code": "MED-RARE", "label": "Medium Rare"},
						{"group": "Steak Extras", "option_code": "ONION", "label": "Onion", "prefix": "No"},
					]),
				},
				{
					"item": "EXTRA-CHEESE",
					"item_name": "Cheese",
					"qty": 1,
					"comments": "",
					"reservation_line_key": "line-cheese-1",
					"custom_parent_line_key": "line-steak-1",
					"custom_is_modifier": 1,
					"custom_modifier_prefix": "Extra",
					"custom_inherit_parent_routing": 1,
				},
			]

			created = kot_gen.process_items_for_kot(
				"POS-009", "Walk-in", "T-1", items, "", "Main POS", "KOT-.#####", "New Order"
			)

			# Only ONE KOT on Hot Kitchen — Cold Larder must NOT get EXTRA-CHEESE!
			self.assertEqual(len(created), 1)
			self.assertEqual(len(self.tickets), 1)
			ticket = self.tickets[0]
			self.assertEqual(ticket.production, "Hot Kitchen")
			self.assertEqual([r.item for r in ticket.kot_items], ["RIBEYE", "EXTRA-CHEESE"])
			self.assertEqual(ticket.kot_items[0].modifier_summary, "Medium Rare · No Onion")
			self.assertEqual(ticket.kot_items[1].parent_line_key, "line-steak-1")
			self.assertEqual(ticket.kot_items[1].is_modifier, 1)
			self.assertIn("Extra", ticket.kot_items[1].modifier_summary)

	def test_combo_meal_routes_drink_to_bar_when_inherit_parent_routing_is_zero(self):
		with patch.dict(sys.modules, self.modules):
			_load("ury.ury.api.ury_bundle_resolver", "ury/api/ury_bundle_resolver.py")
			kot_gen = _load("ury.ury.api.ury_kot_generate", "ury/api/ury_kot_generate.py")

			overrides = [
				{
					"group": "Choose Side",
					"slot_item": "FRIES",
					"chosen_item": "ONION-RINGS",
					"label": "Onion Rings",
					"qty": 1,
					"inherit_parent_routing": 1,
				},
				{
					"group": "Choose Drink",
					"slot_item": "COLA",
					"chosen_item": "MANGO-JUICE",
					"label": "Fresh Mango Juice",
					"qty": 1,
					"inherit_parent_routing": 0,
				},
			]
			items = [
				{
					"item": "BURGER-COMBO",
					"item_name": "Burger Combo",
					"qty": 1,
					"comments": "Side: Onion Rings · Drink: Fresh Mango Juice",
					"reservation_line_key": "combo-1",
					"custom_line_role": "Combo Header",
					"custom_bundle_overrides_json": json.dumps(overrides),
				},
				{
					"item": "COMBO-UPGRADE-FEE",
					"item_name": "Upgrade: Onion Rings",
					"qty": 1,
					"reservation_line_key": "combo-1-side",
					"custom_parent_line_key": "combo-1",
					"custom_is_modifier": 1,
					"custom_line_role": "Combo Upcharge",
					"custom_inherit_parent_routing": 1,
				},
				{
					"item": "COMBO-UPGRADE-FEE",
					"item_name": "Upgrade: Fresh Mango Juice",
					"qty": 1,
					"reservation_line_key": "combo-1-drink",
					"custom_parent_line_key": "combo-1",
					"custom_is_modifier": 1,
					"custom_line_role": "Combo Upcharge",
					"custom_inherit_parent_routing": 0,
				},
			]

			created = kot_gen.process_items_for_kot(
				"POS-009", "Walk-in", "T-1", items, "", "Main POS", "KOT-.#####", "New Order"
			)
			self.assertEqual(len(created), 2)
			by_station = {t.production: t for t in self.tickets}
			self.assertIn("Hot Kitchen", by_station)
			self.assertIn("Bar", by_station)

			# Hot Kitchen sees combo + Onion Rings (NOT non-stock fee SKU and NOT Mango Juice)
			hk_items = [r.item for r in by_station["Hot Kitchen"].kot_items]
			self.assertIn("BURGER-COMBO", hk_items)
			self.assertIn("ONION-RINGS", hk_items)
			self.assertNotIn("MANGO-JUICE", hk_items)
			self.assertNotIn("COMBO-UPGRADE-FEE", hk_items)

			# Bar sees Fresh Mango Juice linked to combo-1
			bar_items = by_station["Bar"].kot_items
			self.assertEqual([r.item for r in bar_items], ["MANGO-JUICE"])
			self.assertEqual(bar_items[0].parent_line_key, "combo-1")

	def test_cancel_kot_emits_swapped_combo_components_to_all_stations(self):
		"""R1 H3 RED: Reducing a swapped combo (2 -> 1) must emit cancel KOTs for swapped components
		on EVERY station that received them (Hot Kitchen ONION-RINGS AND Bar MANGO-JUICE)."""
		with patch.dict(sys.modules, self.modules):
			_load("ury.ury.api.ury_bundle_resolver", "ury/api/ury_bundle_resolver.py")
			kot_gen = _load("ury.ury.api.ury_kot_generate", "ury/api/ury_kot_generate.py")

			overrides = [
				{
					"group": "Choose Side",
					"slot_item": "FRIES",
					"chosen_item": "ONION-RINGS",
					"label": "Onion Rings",
					"qty": 1,
					"inherit_parent_routing": 1,
				},
				{
					"group": "Choose Drink",
					"slot_item": "COLA",
					"chosen_item": "MANGO-JUICE",
					"label": "Fresh Mango Juice",
					"qty": 1,
					"inherit_parent_routing": 0,
				},
			]
			cancel_items = [
				{
					"item_code": "BURGER-COMBO",
					"item_name": "Burger Combo",
					"qty": -1,
					"comments": "Onion Rings · Fresh Mango Juice",
					"reservation_line_key": "ref:BURGER-COMBO:combo-1",
					"custom_line_role": "Combo Header",
					"custom_bundle_overrides_json": json.dumps(overrides),
				},
				{
					"item_code": "COMBO-UPGRADE-FEE",
					"item_name": "Upgrade: Onion Rings",
					"qty": -1,
					"comments": "",
					"reservation_line_key": "ref:COMBO-UPGRADE-FEE:combo-1-side",
					"custom_parent_line_key": "combo-1",
					"custom_is_modifier": 1,
					"custom_line_role": "Combo Upcharge",
					"custom_inherit_parent_routing": 1,
				},
			]
			invoice_items = [
				{
					"item_code": "BURGER-COMBO",
					"item_name": "Burger Combo",
					"qty": 2.0,
					"comments": "Onion Rings · Fresh Mango Juice",
					"reservation_line_key": "ref:BURGER-COMBO:combo-1",
					"custom_line_role": "Combo Header",
					"custom_bundle_overrides_json": json.dumps(overrides),
				},
				{
					"item_code": "COMBO-UPGRADE-FEE",
					"item_name": "Upgrade: Onion Rings",
					"qty": 2.0,
					"comments": "",
					"reservation_line_key": "ref:COMBO-UPGRADE-FEE:combo-1-side",
					"custom_parent_line_key": "combo-1",
					"custom_is_modifier": 1,
					"custom_line_role": "Combo Upcharge",
					"custom_inherit_parent_routing": 1,
				},
			]

			created = kot_gen.process_items_for_cancel_kot(
				"POS-009",
				"Walk-in",
				"T-1",
				cancel_items,
				"Guest changed 2 to 1",
				"Main POS",
				"CNCL-KOT-.#####",
				"Partially cancelled",
				invoice_items,
			)
			self.assertEqual(len(created), 2)
			by_station = {t.production: t for t in self.tickets}
			self.assertIn("Hot Kitchen", by_station)
			self.assertIn("Bar", by_station)

			hk_rows = by_station["Hot Kitchen"].kot_items
			self.assertEqual([r.item for r in hk_rows], ["BURGER-COMBO", "ONION-RINGS"])
			self.assertEqual(hk_rows[0].cancelled_qty, 1.0)
			self.assertEqual(hk_rows[0].quantity, 2.0)
			self.assertEqual(hk_rows[1].cancelled_qty, 1.0)
			self.assertEqual(hk_rows[1].quantity, 2.0)
			self.assertEqual(hk_rows[1].is_modifier, 1)
			self.assertEqual(hk_rows[1].parent_line_key, "combo-1")

			bar_rows = by_station["Bar"].kot_items
			self.assertEqual([r.item for r in bar_rows], ["MANGO-JUICE"])
			self.assertEqual(bar_rows[0].cancelled_qty, 1.0)
			self.assertEqual(bar_rows[0].quantity, 2.0)
			self.assertEqual(bar_rows[0].is_modifier, 1)
			self.assertEqual(bar_rows[0].parent_line_key, "combo-1")

	def test_desk_kot_execute_preserves_child_modifier_metadata_and_routing_with_held_lines(self):
		"""R1 H4 RED: _execute_kot(desk=True) / kot_execute must NOT strip child modifier metadata
		when held/reservation lines exist on the POS Invoice."""
		with patch.dict(sys.modules, self.modules):
			_load("ury.ury.api.ury_bundle_resolver", "ury/api/ury_bundle_resolver.py")
			kot_gen = _load("ury.ury.api.ury_kot_generate", "ury/api/ury_kot_generate.py")

			self.pos_invoice.items = [
				Row(
					name="row-rib",
					item_code="RIBEYE",
					item_name="Ribeye Steak",
					qty=1,
					comment="Medium Rare",
					reservation_line_key="hk-rib",
					ury_held=0,
					custom_is_modifier=0,
					custom_line_role="Standard",
					custom_modifiers_json=json.dumps([
						{"group": "Meat Temperature", "option_code": "MED-RARE", "label": "Medium Rare"}
					]),
					custom_inherit_parent_routing=1,
				),
				Row(
					name="row-ch",
					item_code="EXTRA-CHEESE",
					item_name="Aged Cheddar",
					qty=1,
					comment="",
					reservation_line_key="hk-ch",
					ury_held=0,
					custom_parent_line_key="hk-rib",
					custom_is_modifier=1,
					custom_line_role="Modifier",
					custom_modifier_prefix="Extra",
					custom_inherit_parent_routing=1,
				),
				Row(
					name="row-held",
					item_code="CHICKEN",
					item_name="Grilled Chicken",
					qty=1,
					comment="",
					reservation_line_key="hk-chk",
					ury_held=1,
					custom_is_modifier=0,
					custom_line_role="Standard",
				),
			]

			kot_gen.kot_execute(
				"POS-009", "Walk-in", restaurant_table="T-1", current_items=[], previous_items=[]
			)

			self.assertEqual(len(self.tickets), 1)
			ticket = self.tickets[0]
			self.assertEqual(ticket.production, "Hot Kitchen")
			self.assertEqual([r.item for r in ticket.kot_items], ["RIBEYE", "EXTRA-CHEESE"])
			self.assertEqual(ticket.kot_items[1].is_modifier, 1)
			self.assertEqual(ticket.kot_items[1].parent_line_key, "hk-rib")
			self.assertIn("Extra", ticket.kot_items[1].modifier_summary)


class TestPrintFormatsModifiersAndOpt79Suppression(unittest.TestCase):
	def _render(self, fmt_name, doc):
		from jinja2 import DebugUndefined
		from jinja2.sandbox import SandboxedEnvironment

		# Load print_format.utils with minimal frappe double
		framework = MagicMock()
		framework.db.get_value.return_value = None
		utils_mod = _stub("frappe.utils", cint=lambda v: int(v or 0), fmt_money=lambda v, **kw: f"{v}")
		with patch.dict(sys.modules, {"frappe": framework, "frappe.utils": utils_mod}):
			pf_utils = _load("ury.ury.print_format.utils", "ury/print_format/utils.py")

		spec = json.loads((PRINT_DIR / fmt_name / f"{fmt_name}.json").read_text(encoding="utf-8"))
		env = SandboxedEnvironment(undefined=DebugUndefined, autoescape=False)
		env.globals.update(
			_=lambda s: s,
			format_receipt_item_notes=getattr(pf_utils, "format_receipt_item_notes", None),
			frappe=SimpleNamespace(
				get_fullname=lambda u=None: u or "",
				get_meta=lambda dt: SimpleNamespace(has_field=lambda f: False),
				db=SimpleNamespace(get_value=lambda *a, **kw: None),
				utils=SimpleNamespace(
					formatdate=str,
					format_time=lambda t, fmt=None: str(t or "")[:5],
					fmt_money=lambda v, currency=None, **kw: f"{v} {currency}" if currency else str(v),
					now=lambda: "2026-11-24 14:00:00",
				),
			),
		)
		return env.from_string(spec["html"]).render(doc=doc)

	def test_guest_check_indents_priced_modifiers_and_suppresses_opt79_unpriced_prep(self):
		class PrintRow(SimpleNamespace):
			def get(self, key, default=None):
				return getattr(self, key, default)

			def get_formatted(self, fieldname, *args, **kwargs):
				return str(getattr(self, fieldname, ""))

		doc = PrintRow(
			name="POS-INV-808",
			docstatus=0,
			status="Draft",
			is_return=0,
			company="Grand Terrace",
			company_address=None,
			pos_profile="Main POS",
			restaurant="Grand Terrace",
			branch="Main",
			posting_date="2026-11-24",
			posting_time="14:00:00",
			order_type="Dine In",
			restaurant_table="T-4",
			custom_merged_tables="",
			no_of_pax=2,
			custom_ury_order_number="808",
			custom_guest_label=None,
			customer="Walk-in",
			customer_name="Walk-in",
			mobile_number=None,
			waiter="Alice",
			cashier="Bob",
			currency="UGX",
			tax_id=None,
			custom_merged_pos_invoice=None,
			ury_print_partner=None,
			efris_e_invoice=None,
			taxes=[],
			payments=[],
			total=68000,
			net_total=68000,
			discount_amount=0,
			additional_discount_percentage=0,
			rounding_adjustment=0,
			grand_total=68000,
			rounded_total=68000,
			paid_amount=0,
			change_amount=0,
			outstanding_amount=0,
			items=[
				PrintRow(
					item_code="RIBEYE",
					item_name="Ribeye Steak",
					qty=1,
					rate=65000,
					amount=65000,
					comment="Medium Rare · No Onion · Allergy: nuts",
					custom_is_modifier=0,
					custom_modifier_prefix="",
					custom_hide_unpriced_on_receipt=1,
					custom_modifiers_json=json.dumps([
						{"group": "Meat Temperature", "option_code": "MED-RARE", "label": "Medium Rare", "price_mode": "Unpriced Prep", "hide_on_receipt": 1},
						{"group": "Steak Extras", "option_code": "ONION", "label": "Onion", "prefix": "No", "price_mode": "Unpriced Prep", "hide_on_receipt": 1},
					]),
				),
				PrintRow(
					item_code="EXTRA-CHEESE",
					item_name="Aged Cheddar",
					qty=1,
					rate=3000,
					amount=3000,
					comment="",
					custom_is_modifier=1,
					custom_modifier_prefix="Extra",
					custom_hide_unpriced_on_receipt=0,
					custom_modifiers_json="",
				),
			],
		)

		html = self._render("merged_pos_invoice_format", doc)
		# Priced child modifier renders indented under parent with prefix
		self.assertIn("mod-row", html)
		self.assertIn("Extra Aged Cheddar", html)
		# Opt 79: unpriced prep modifiers (hide_on_receipt=1) are suppressed on customer check,
		# while manual free-text note ("Allergy: nuts") is still preserved!
		self.assertNotIn("Medium Rare", html)
		self.assertNotIn("No Onion", html)
		self.assertIn("Allergy: nuts", html)

	def test_waiter_order_slip_always_prints_unpriced_prep_and_indents_child_modifiers(self):
		class SlipRow(SimpleNamespace):
			def get(self, key, default=None):
				return getattr(self, key, default)

			def get_formatted(self, fieldname, *args, **kwargs):
				return str(getattr(self, fieldname, ""))

		kot_doc = SlipRow(
			invoice="POS-INV-808",
			restaurant_table="T-4",
			custom_guest_label=None,
			customer_name="Walk-in",
			order_no="808",
			waiter="Alice",
			date="2026-11-24",
			time="14:00:00",
			kot_items=[
				SlipRow(
					display_mode="single_qty",
					cancelled_qty=0,
					quantity=1,
					item="RIBEYE",
					item_name="Ribeye Steak",
					comments="Medium Rare · No Onion",
					is_modifier=0,
					modifier_summary="Medium Rare · No Onion",
				),
				SlipRow(
					display_mode="single_qty",
					cancelled_qty=0,
					quantity=1,
					item="EXTRA-CHEESE",
					item_name="Aged Cheddar",
					comments="",
					is_modifier=1,
					modifier_summary="Extra",
				),
			],
		)

		html = self._render("ury_waiter_order_slip", kot_doc)
		self.assertIn("Medium Rare", html)
		self.assertIn("No Onion", html)
		self.assertIn("mod-row", html)
		self.assertIn("Extra", html)

	def test_waiter_order_slip_deduplicates_identical_modifier_summary_and_item_name(self):
		class SlipRow(SimpleNamespace):
			def get(self, key, default=None):
				return getattr(self, key, default)

			def get_formatted(self, fieldname, *args, **kwargs):
				return str(getattr(self, fieldname, ""))

		kot_doc = SlipRow(
			invoice="POS-INV-809",
			restaurant_table="T-5",
			custom_guest_label=None,
			customer_name="Walk-in",
			order_no="809",
			waiter="Alice",
			date="2026-11-25",
			time="10:00:00",
			kot_items=[
				SlipRow(
					display_mode="single_qty",
					cancelled_qty=0,
					quantity=1,
					item="ONION-RINGS",
					item_name="Onion Rings",
					comments="Burger Side",
					is_modifier=1,
					modifier_summary="Onion Rings",
				),
				SlipRow(
					display_mode="old_new",
					cancelled_qty=1,
					old_qty=2,
					new_qty=1,
					quantity=2,
					item="MANGO-JUICE",
					item_name="Mango Juice",
					comments="Burger Drink",
					is_modifier=1,
					modifier_summary="Mango Juice",
				),
				SlipRow(
					display_mode="single_qty",
					cancelled_qty=0,
					quantity=1,
					item="EXTRA-CHEESE",
					item_name="Aged Cheddar",
					comments="",
					is_modifier=1,
					modifier_summary="Extra",
				),
			],
		)

		html = self._render("ury_waiter_order_slip", kot_doc)
		self.assertIn("+ Onion Rings", html)
		self.assertNotIn("Onion Rings Onion Rings", html)
		self.assertIn("+ Mango Juice", html)
		self.assertNotIn("Mango Juice Mango Juice", html)
		self.assertIn("+ Extra Aged Cheddar", html)


if __name__ == "__main__":
	unittest.main()
