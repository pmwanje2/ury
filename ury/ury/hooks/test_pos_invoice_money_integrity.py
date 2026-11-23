"""POS Invoice validate/submit hooks that decide what the guest is charged; site-less (frappe double, no bench).

Pinned here:

1. Price list (G41). The check keeps the price list its lines were priced from. Resolution mirrors
   how sync_order prices a line: an aggregator check uses its Aggregator Settings price list; a
   table check uses its room's menu when the restaurant prices by room, else the active menu; a
   counter check uses its order type's menu when the restaurant prices by order type, else the
   active menu. The menu's enabled Price List is the check's price list. Simphony prices each
   revenue center or order type from its own menu level; one later branch must never overwrite it.
2. Printed-check lines (G8). Lines are compared by line identity (the stable reservation_line_key,
   else the row), never by item code, so two lines of one item are two lines. A new document
   (including a native POS return) has no stored copy to compare against.
3. Legacy linked bills (G12). Bills linked before Add Check moved lines onto one check still
   settle as two invoices. Submitting the primary must not rewrite the partner's own tenders, a
   partner that cannot post must stop the primary posting, and a partner that already posted is
   not saved again.
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

APP = Path(__file__).resolve().parents[1]  # ury/ury
HOOKS = APP / "hooks/ury_pos_invoice.py"


class Row(dict):
    __getattr__ = dict.get


def _stub(name, **attrs):
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class Refused(Exception):
    pass


class DoesNotExist(Exception):
    pass


def _framework():
    framework = MagicMock()
    framework.ValidationError = Refused
    framework.DoesNotExistError = DoesNotExist
    framework._ = lambda message, *args: message
    framework.flags = SimpleNamespace()

    def throw(message, exception=None, **kwargs):
        raise (exception or Refused)(message)

    framework.throw = throw
    framework.get_traceback = lambda *args, **kwargs: "traceback"
    utils = _stub(
        "frappe.utils",
        flt=lambda value, precision=None: float(value or 0),
        now_datetime=MagicMock(), get_time=MagicMock(), now=MagicMock(), getdate=MagicMock(),
        get_first_day=MagicMock(), get_last_day=MagicMock(), get_datetime=MagicMock(),
    )
    return framework, {
        "frappe": framework,
        "frappe.utils": utils,
        "ury.ury.doctype.ury_order.ury_order": _stub(
            "ury.ury.doctype.ury_order.ury_order", release_merge_cluster_tables=MagicMock()),
        "ury.ury.doctype.staff_discount_policy.staff_discount_policy": _stub(
            "ury.ury.doctype.staff_discount_policy.staff_discount_policy", get_applicable_policy=MagicMock()),
    }


class Loaded(unittest.TestCase):
    def setUp(self):
        self.frappe, modules = _framework()
        self.enterContext(patch.dict(sys.modules, modules))
        spec = importlib.util.spec_from_file_location("ury.ury.hooks.ury_pos_invoice", HOOKS)
        self.hooks = importlib.util.module_from_spec(spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury.hooks.ury_pos_invoice": self.hooks}))
        spec.loader.exec_module(self.hooks)


class Records:
    """Configuration records the price-list resolution reads, keyed as frappe.db.get_value is called."""

    def __init__(self):
        self.restaurant = {"active_menu": "Main Menu", "room_wise_menu": 0, "order_type_wise_menu": 0}
        self.table_rooms = {"Pool 1": "Pool Bar", "Hall 1": "Hall"}
        self.room_menus = {}
        self.order_type_menus = {}
        self.price_lists = {"Main Menu": "Main Prices", "Pool Menu": "Pool Prices", "Pickup Menu": "Pickup Prices"}
        self.aggregator_price_lists = {}

    def get_value(self, doctype, filters=None, fieldname=None, *args, **kwargs):
        if doctype == "URY Restaurant":
            return self.restaurant.get(fieldname)
        if doctype == "URY Table":
            assert fieldname == "restaurant_room", fieldname
            return self.table_rooms.get(filters)
        if doctype == "Menu for Room":
            return self.room_menus.get(filters["room"])
        if doctype == "Order Type Menu":
            return self.order_type_menus.get(filters["order_type"])
        if doctype == "Price List":
            assert filters.get("enabled") == 1, filters
            return self.price_lists.get(filters.get("restaurant_menu"))
        if doctype == "Aggregator Settings":
            assert fieldname == "price_list", fieldname
            return self.aggregator_price_lists.get(filters["customer"])
        raise AssertionError(f"unexpected read {doctype} {filters} {fieldname}")


class TestPriceListResolution(Loaded):
    """G41: the check keeps the price list of the menu it was rung on."""

    def setUp(self):
        super().setUp()
        self.records = Records()
        self.frappe.db.get_value.side_effect = self.records.get_value

    def check(self, order_type="Dine In", table=None, customer="Walk-in"):
        return SimpleNamespace(
            restaurant="Lakeside", restaurant_table=table, order_type=order_type, customer=customer,
            branch="Lakeside Branch", selling_price_list="Rung Prices",
        )

    def resolve(self, doc):
        self.hooks.validate_price_list(doc, "validate")
        return doc.selling_price_list

    def test_a_table_in_a_room_priced_restaurant_keeps_its_rooms_price_list(self):
        self.records.restaurant["room_wise_menu"] = 1
        self.records.room_menus["Pool Bar"] = "Pool Menu"
        self.assertEqual(self.resolve(self.check(table="Pool 1")), "Pool Prices")

    def test_a_counter_check_in_an_order_type_priced_restaurant_keeps_its_order_types_price_list(self):
        self.records.restaurant["order_type_wise_menu"] = 1
        self.records.order_type_menus["Take Away"] = "Pickup Menu"
        self.assertEqual(self.resolve(self.check(order_type="Take Away")), "Pickup Prices")

    def test_a_table_check_ignores_the_order_type_menu(self):
        """sync_order prices a table check from its room or the active menu, never the order-type menu."""
        self.records.restaurant["order_type_wise_menu"] = 1
        self.records.order_type_menus["Dine In"] = "Pickup Menu"
        self.assertEqual(self.resolve(self.check(table="Hall 1")), "Main Prices")

    def test_a_table_without_room_pricing_uses_the_active_menu(self):
        self.records.room_menus["Pool Bar"] = "Pool Menu"
        self.assertEqual(self.resolve(self.check(table="Pool 1")), "Main Prices")

    def test_a_room_without_its_own_menu_uses_the_active_menu(self):
        self.records.restaurant["room_wise_menu"] = 1
        self.assertEqual(self.resolve(self.check(table="Hall 1")), "Main Prices")

    def test_a_counter_check_without_order_type_pricing_uses_the_active_menu(self):
        self.records.order_type_menus["Take Away"] = "Pickup Menu"
        self.assertEqual(self.resolve(self.check(order_type="Take Away")), "Main Prices")

    def test_an_aggregator_check_uses_the_aggregators_price_list(self):
        self.records.restaurant["room_wise_menu"] = 1
        self.records.room_menus["Pool Bar"] = "Pool Menu"
        self.records.aggregator_price_lists["Delivery Partner"] = "Partner Prices"
        doc = self.check(order_type="Aggregators", customer="Delivery Partner")
        self.assertEqual(self.resolve(doc), "Partner Prices")

    def test_an_aggregator_without_a_price_list_is_refused(self):
        with self.assertRaises(Refused):
            self.resolve(self.check(order_type="Aggregators", customer="Unknown Partner"))

    def test_a_check_without_a_restaurant_keeps_its_price_list(self):
        doc = self.check()
        doc.restaurant = None
        self.assertEqual(self.resolve(doc), "Rung Prices")


class Check:
    """A POS Invoice double: header fields as attributes, child rows as Row dicts."""

    def __init__(self, name="CHK-1", items=(), new=False, **fields):
        self.name = name
        self.items = list(items)
        self.payments = []
        self.flags = SimpleNamespace()
        self.waiter = "waiter@example.com"
        self.pos_profile = "Lakeside Till"
        self.invoice_printed = 1
        self.is_return = 0
        self.docstatus = 0
        self._new = new
        self.saved = 0
        self.submitted = 0
        self.__dict__.update(fields)

    def is_new(self):
        return self._new

    def get(self, key, default=None):
        return getattr(self, key, default)

    def set(self, key, value):
        setattr(self, key, value)

    def append(self, key, row):
        getattr(self, key).append(Row(row))

    def save(self, **kwargs):
        self.saved += 1

    def submit(self):
        self.submitted += 1
        self.docstatus = 1


def line(name, item_code, qty, key=None):
    return Row(name=name, item_code=item_code, item_name=item_code.title(), qty=qty, reservation_line_key=key)


class TestPrintedCheckLineIdentity(Loaded):
    """G8: a printed check refuses a removed or reduced line, judged line by line."""

    def setUp(self):
        super().setUp()
        self.frappe.db.get_value.side_effect = lambda doctype, name, field, *a, **k: (
            0 if (doctype, field) == ("POS Profile", "remove_items") else None)
        self.stored = None
        self.frappe.get_doc.side_effect = self.load

    def load(self, doctype, name, *args, **kwargs):
        if self.stored is None:
            raise DoesNotExist(f"{doctype} {name} not found")
        return self.stored

    def validate(self, doc):
        self.hooks.validate_invoice(doc, "validate")

    def test_removing_one_of_two_lines_of_one_item_is_refused(self):
        self.stored = Check(items=[line("r1", "soda", 2, "k1"), line("r2", "soda", 1, "k2")])
        with self.assertRaisesRegex(Refused, "Removed items"):
            self.validate(Check(items=[line("r1", "soda", 2, "k1")]))

    def test_reducing_one_of_two_lines_of_one_item_is_refused(self):
        self.stored = Check(items=[line("r1", "soda", 2, "k1"), line("r2", "soda", 1, "k2")])
        with self.assertRaisesRegex(Refused, "qty reduced from 2"):
            self.validate(Check(items=[line("r1", "soda", 1, "k1"), line("r2", "soda", 1, "k2")]))

    def test_adding_a_line_to_a_printed_check_is_allowed(self):
        self.stored = Check(items=[line("r1", "soda", 2, "k1")])
        self.validate(Check(items=[line("r1", "soda", 2, "k1"), line("r3", "soda", 1, "k3")]))

    def test_a_line_resaved_under_a_new_row_keeps_its_identity(self):
        """sync_order rebuilds rows; the stable line key, not the row name, is the line."""
        self.stored = Check(items=[line("r1", "soda", 2, "k1"), line("r2", "fish", 1, "k2")])
        self.validate(Check(items=[line("r8", "soda", 2, "k1"), line("r9", "fish", 1, "k2")]))

    def test_a_line_without_a_stable_key_is_matched_by_its_row(self):
        self.stored = Check(items=[line("r1", "soda", 1), line("r2", "soda", 1)])
        with self.assertRaisesRegex(Refused, "Removed items"):
            self.validate(Check(items=[line("r1", "soda", 1)]))

    def test_a_new_native_return_is_not_compared_with_a_stored_copy(self):
        """A return copies invoice_printed from the paid check but is a new document with no stored copy."""
        self.validate(Check(name="RET-1", new=True, is_return=1, items=[line("r1", "soda", -1, "k1")]))
        self.frappe.get_doc.assert_not_called()

    def test_a_new_document_is_not_compared_with_a_stored_copy(self):
        self.validate(Check(name="CHK-NEW", new=True, items=[line("r1", "soda", 1, "k1")]))
        self.frappe.get_doc.assert_not_called()


class TestLegacyLinkedBillSync(Loaded):
    """G12 for bills linked before Add Check consolidated lines: both post exactly once with their own tenders."""

    def setUp(self):
        super().setUp()
        self.frappe.db.exists.return_value = True
        self.partner = Check(name="CHK-2", invoice_printed=0, rounded_total=80, paid_amount=80)
        self.partner.payments = [Row(mode_of_payment="Card", amount=80)]
        self.frappe.get_doc.side_effect = lambda doctype, name, *a, **k: self.partner
        self.primary = Check(name="CHK-1", custom_merged_pos_invoice="CHK-2", docstatus=1, paid_amount=150)
        self.primary.payments = [Row(mode_of_payment="Cash", amount=100), Row(mode_of_payment="Card", amount=50)]

    def test_submitting_the_primary_keeps_the_partners_own_tenders(self):
        self.hooks.sync_merged_invoice(self.primary)
        self.assertEqual(self.partner.submitted, 1)
        self.assertEqual(
            [(row.mode_of_payment, row.amount) for row in self.partner.payments], [("Card", 80)])
        self.assertEqual(self.partner.paid_amount, 80)

    def test_a_partner_that_cannot_post_stops_the_primary_posting(self):
        def refuse():
            raise Refused("Partner refused")

        self.partner.submit = refuse
        with self.assertRaisesRegex(Refused, "Partner refused"):
            self.hooks.sync_merged_invoice(self.primary)

    def test_a_partner_that_already_posted_is_not_saved_again(self):
        self.partner.docstatus = 1
        self.hooks.sync_merged_invoice(self.primary)
        self.assertEqual(self.partner.saved, 0)
        self.assertEqual(self.partner.submitted, 0)

    def test_a_draft_save_still_mirrors_the_printed_flag(self):
        self.primary.docstatus = 0
        self.primary.invoice_printed = 1
        self.hooks.sync_merged_invoice(self.primary)
        self.assertEqual(self.partner.invoice_printed, 1)
        self.assertEqual(self.partner.saved, 1)


if __name__ == "__main__":
    unittest.main()
