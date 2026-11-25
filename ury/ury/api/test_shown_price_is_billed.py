"""A price shown to a guest or an aggregator till is the price billed; site-less (frappe double, no bench).

SAP basis: every channel uses one price determination, so the quoted price is the
billed price. Here that is ury.ury.api.item_price.price_valid_on, which billing
(price_items_for_invoice) uses: the Item Price valid on the pricing date,
released by the Item Price workflow if one is active.

Ported from the fork (#89 05691301, #90 688b596b) onto the version-16
self_ordering and ury_pos.api import graphs. Version-16 get_customer_product
refuses an item that is not on the guest's menu (U12), so the menu double
lists the item.
"""

import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

TODAY = date(2026, 11, 18)
ROOT = Path(__file__).resolve().parents[2]  # ury/


def getdate(value=None):
    if value is None:
        return TODAY
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class Row(dict):
    __getattr__ = dict.get


class PriceBook:
    """Item Price rows in creation order plus an optional release workflow; other reads are scripted."""

    def __init__(self, framework):
        self.rows = []
        self.workflow = None
        self.values = {}
        framework.db.get_list.side_effect = self.get_list
        framework.get_all.side_effect = self.get_all
        framework.db.get_value.side_effect = self.get_value
        framework.get_cached_doc.side_effect = lambda doctype, name: Row(
            states=[Row(state=state) for state in self.workflow["states"]])

    def add(self, item, rate, valid_from=None, valid_upto=None, state=None, price_list="Menu A", uom="Nos",
            customer=None):
        self.rows.append(Row(
            name=f"IP-{len(self.rows) + 1}", item_code=item, item_name=item, price_list=price_list,
            price_list_rate=rate, valid_from=valid_from, valid_upto=valid_upto, workflow_state=state,
            uom=uom, customer=customer, supplier=None, batch_no=None, selling=1,
        ))

    def use_workflow(self):
        self.workflow = {"field": "workflow_state", "states": {"Draft": "", "Approved": "Success"}}

    def item_prices(self, filters, fields):
        rows = [r for r in self.rows if all(r.get(k) == v for k, v in (filters or {}).items())]
        return [Row({f: r.get(f) for f in fields}) for r in rows] if fields else rows

    def get_list(self, doctype, filters=None, fields=None, **kwargs):
        assert doctype == "Item Price", doctype
        return self.item_prices(filters, fields)

    def get_all(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
        if doctype == "Workflow State":
            return [n for n in filters["name"][1] if self.workflow["states"].get(n) == filters["style"]]
        if doctype == "Item Price":
            return self.item_prices(filters, fields)
        return self.values.get(("get_all", doctype), [])

    def get_value(self, doctype, filters=None, fieldname=None, *args, **kwargs):
        if doctype == "Workflow":
            if not self.workflow:
                return None
            return {"workflow_state_field": self.workflow["field"], "name": "Menu Price Release"}[fieldname]
        if doctype == "Item Price":
            # The pre-fix lookup: the first row on the price list, whatever its validity.
            rows = self.item_prices(filters, None)
            return rows[0][fieldname] if rows else None
        if doctype == "Item" and fieldname in ("stock_uom", "sales_uom"):
            return {"stock_uom": "Nos", "sales_uom": None}[fieldname]
        return self.values.get((doctype, fieldname))


def _framework():
    framework = MagicMock()
    framework.ValidationError = type("ValidationError", (Exception,), {})
    framework.PermissionError = type("PermissionError", (Exception,), {})
    framework.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
    framework._ = lambda text, *args: text
    framework.whitelist = lambda *args, **kwargs: (lambda function: function)
    framework.session = SimpleNamespace(user="Guest")

    def throw(message, exception=None, **kwargs):
        raise (exception or framework.ValidationError)(message)

    framework.throw = throw
    framework.utils.getdate = getdate
    return framework


def _stub(name, **attrs):
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class Loaded(unittest.TestCase):
    def setUp(self):
        self.framework = _framework()
        self.book = PriceBook(self.framework)
        utils = _stub("frappe.utils", getdate=getdate, cint=lambda value: int(value or 0),
                      flt=lambda value: float(value or 0))
        for name in ("add_to_date", "now_datetime", "get_datetime", "nestedset", "validate_phone_number",
                     "today", "get_system_timezone"):
            setattr(utils, name, MagicMock())
        order = _stub(
            "ury.ury.doctype.ury_order.ury_order",
            _kitchen_snapshot=MagicMock(), _resolve_or_create_pos_invoice=MagicMock(), _ensure_invoice_reservation_ref=MagicMock(),
            invoice_pricing_date=MagicMock(), price_items_for_invoice=MagicMock(),
            # The menu resolvers _resolve_or_create_pos_invoice uses: by table (room-wise) and by order type.
            get_restaurant_and_menu_name=MagicMock(return_value=("Branch A", "Menu Room", "Restaurant A")),
            get_menu_name=MagicMock(return_value="Menu Pickup"),
        )
        self.order = order
        self.enterContext(patch.dict(sys.modules, {
            "frappe": self.framework, "frappe.utils": utils,
            # self_ordering decorates the guest feedback endpoints with the native limiter.
            "frappe.rate_limiter": _stub("frappe.rate_limiter",
                                         rate_limit=lambda *args, **kwargs: (lambda function: function)),
            "ury.ury.doctype.ury_order.ury_order": order,
            "ury.ury.api.ury_kot_generate": _stub("ury.ury.api.ury_kot_generate", _execute_kot=MagicMock()),
            "ury.ury.api.ury_order_reservation_service": _stub(
                "ury.ury.api.ury_order_reservation_service", reconcile_order_reservations=MagicMock()),
            "ury.ury.api.ury_stock_policy": _stub("ury.ury.api.ury_stock_policy",
                                                  get_branch_stock_policy=MagicMock()),
            "ury.ury_pos.default_customer": _stub("ury.ury_pos.default_customer",
                                                  get_default_customer_config=MagicMock()),
        }))
        if (ROOT / "ury/api/item_price.py").is_file():
            self.load("ury.ury.api.item_price", "ury/api/item_price.py")
        self.pos_api = self.load("ury.ury_pos.api", "ury_pos/api.py")
        self.guest = self.load("ury.ury.api.self_ordering", "ury/api/self_ordering.py")

    def load(self, name, relative):
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        module = importlib.util.module_from_spec(spec)
        self.enterContext(patch.dict(sys.modules, {name: module}))
        spec.loader.exec_module(module)
        return module


class TestGuestOptionPrices(Loaded):
    """A variant or add-on priced on an invoice price list shows the rate billing uses on that list."""

    def setUp(self):
        super().setUp()
        self.framework.db.exists.return_value = True
        self.framework.db.get_value.side_effect = self.value
        self.framework.get_all.side_effect = lambda doctype, **kwargs: (
            [Row(item="Chips")] if doctype == "Item Add On" else self.book.get_all(doctype, **kwargs))

    def value(self, doctype, filters=None, fieldname=None, *args, **kwargs):
        if doctype == "Item" and isinstance(fieldname, list):
            return Row(item_code=filters, item_name=filters, image=None)
        return self.book.get_value(doctype, filters, fieldname, *args, **kwargs)

    def addon_rate(self):
        options = self.guest._linked_item_options("Item Add On", "Fish", invoice_price_list="Menu A")
        return [option["rate"] for option in options]

    def test_addon_shows_the_price_valid_today(self):
        self.book.add("Chips", 5, "2026-01-01")
        self.book.add("Chips", 7, "2026-11-01")
        self.assertEqual(self.addon_rate(), [7])

    def test_addon_does_not_show_an_unreleased_price(self):
        self.book.use_workflow()
        self.book.add("Chips", 9, "2026-11-10", state="Draft")
        self.book.add("Chips", 5, "2026-01-01", state="Approved")
        self.assertEqual(self.addon_rate(), [5])

    def test_addon_without_a_valid_price_shows_no_rate(self):
        self.book.add("Chips", 5, "2027-01-01")
        self.assertEqual(self.addon_rate(), [None])

    def test_addon_without_an_invoice_price_list_shows_no_rate(self):
        self.book.add("Chips", 5, "2026-01-01")
        options = self.guest._linked_item_options("Item Add On", "Fish", invoice_price_list=None)
        self.assertEqual([option["rate"] for option in options], [None])


class TestAggregatorItemPrices(Loaded):
    """The aggregator till lists each item once, at the rate sync_order bills on the aggregator price list."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(self.pos_api, "getBranch", return_value="Branch A"))
        self.book.values[("Aggregator Settings", "price_list")] = "Menu A"

    def listed(self):
        return {row["item"]: row["rate"] for row in self.pos_api.getAggregatorItem("Glovo")}

    def test_each_item_is_listed_once_at_the_price_valid_today(self):
        self.book.add("Fish", 100, "2026-01-01")
        self.book.add("Fish", 130, "2026-11-01")
        rows = self.pos_api.getAggregatorItem("Glovo")
        self.assertEqual([(row["item"], row["rate"]) for row in rows], [("Fish", 130)])

    def test_unreleased_price_is_not_listed(self):
        self.book.use_workflow()
        self.book.add("Fish", 100, "2026-01-01", state="Approved")
        self.book.add("Fish", 150, "2026-11-10", state="Draft")
        self.assertEqual(self.listed(), {"Fish": 100})

    def test_the_aggregators_own_price_is_listed(self):
        self.book.add("Fish", 100, "2026-11-10")
        self.book.add("Fish", 115, "2026-01-01", customer="Glovo")
        self.book.add("Fish", 90, "2026-01-01", customer="SafeBoda")
        self.assertEqual(self.listed(), {"Fish": 115})

    def test_item_without_a_valid_price_is_not_listed(self):
        self.book.add("Fish", 100, "2026-01-01")
        self.book.add("Tea", 20, "2027-01-01")
        self.assertEqual(self.listed(), {"Fish": 100})


class TestGuestProductPricedLikeTheBill(Loaded):
    """get_customer_product shows each option at the rate add_customer_items bills: the guest invoice's price list
    and customer, the menu price list and profile default customer when no invoice exists yet."""

    def setUp(self):
        super().setUp()
        self.profile = SimpleNamespace(default_customer="Walk-in", branch="Branch A", show_item_descriptions=0,
                                       show_item_images=0)
        self.session = SimpleNamespace(ordering_profile="Guest Profile", table="T1", invoice=None)
        self.framework.get_doc.side_effect = self.get_doc
        self.framework.db.exists.return_value = True
        self.framework.get_all.side_effect = lambda doctype, **kwargs: (
            [Row(item="Chips")] if doctype == "Item Add On" else self.book.get_all(doctype, **kwargs))
        self.framework.db.get_value.side_effect = self.value
        self.enterContext(patch.object(self.guest, "_resolve_session", return_value=self.session))
        self.enterContext(patch.object(self.guest, "_elevated", MagicMock()))
        self.invoice = SimpleNamespace(customer="Glovo", selling_price_list="Menu B")

    def get_doc(self, doctype, name=None):
        if doctype == "URY Self Ordering Profile":
            return self.profile
        assert (doctype, name) == ("POS Invoice", "POS-1"), (doctype, name)
        return self.invoice

    def value(self, doctype, filters=None, fieldname=None, *args, **kwargs):
        if doctype == "Item" and isinstance(fieldname, list):
            return Row(item_code=filters, item_name=filters, image=None, description=None)
        if doctype == "Price List":
            assert set(filters) == {"restaurant_menu", "enabled"} and filters["enabled"] == 1, filters
            # "Menu Active" is the restaurant's active menu; it carries no Chips price.
            return {"Menu Room": "Menu A", "Menu Pickup": "Menu P", "Menu Active": "Menu Z"}.get(
                filters["restaurant_menu"])
        return self.book.get_value(doctype, filters, fieldname, *args, **kwargs)

    def shown(self):
        menu = {"items": [{"item": "Fish"}], "name": "Menu Active"}
        self.enterContext(patch.object(self.guest, "resolve_restaurant_menu", return_value=menu))
        return [option["rate"] for option in self.guest.get_customer_product("SESSION", "Fish")["addons"]]

    def test_before_the_first_order_options_show_the_menu_price_for_the_default_customer(self):
        self.book.add("Chips", 5, "2026-01-01")
        self.book.add("Chips", 4, "2026-01-01", customer="Walk-in")
        self.book.add("Chips", 9, "2026-01-01", price_list="Menu B")
        self.assertEqual(self.shown(), [4])

    def test_after_the_first_order_options_show_the_invoice_price_for_the_invoice_customer(self):
        self.session.invoice = "POS-1"
        self.book.add("Chips", 5, "2026-01-01")
        self.book.add("Chips", 9, "2026-01-01", price_list="Menu B")
        self.book.add("Chips", 7, "2026-01-01", price_list="Menu B", customer="Glovo")
        self.assertEqual(self.shown(), [7])


class TestGuestPricingFollowsTheInvoiceMenu(TestGuestProductPricedLikeTheBill):
    """Before the first order the option price list is the one the new invoice will get: the table's (room-wise)
    menu with a table, the order-type menu of the profile branch without one. Reading it writes nothing."""

    def setUp(self):
        super().setUp()
        for name in ("set_value", "sql", "delete", "commit"):
            getattr(self.framework.db, name).side_effect = AssertionError(f"guest pricing wrote: db.{name}")
        for name in ("new_doc", "delete_doc"):
            getattr(self.framework, name).side_effect = AssertionError(f"guest pricing wrote: {name}")

    def test_a_table_guest_uses_the_tables_menu(self):
        self.book.add("Chips", 5, "2026-01-01")
        self.assertEqual(self.shown(), [5])
        self.order.get_restaurant_and_menu_name.assert_called_once_with("T1")

    def test_a_pickup_guest_uses_the_order_type_menu_of_the_profile_branch(self):
        self.session.table = None
        self.book.add("Chips", 6, "2026-01-01", price_list="Menu P")
        self.assertEqual(self.shown(), [6])
        self.order.get_menu_name.assert_called_once_with("Take Away", branch="Branch A")

    def test_pricing_creates_and_saves_nothing(self):
        self.book.add("Chips", 5, "2026-01-01")
        self.shown()
        self.session.invoice = "POS-1"
        self.book.add("Chips", 9, "2026-01-01", price_list="Menu B")
        self.shown()
        self.order._resolve_or_create_pos_invoice.assert_not_called()


class TestOneUomRule(unittest.TestCase):
    def test_the_selling_uom_rule_lives_only_inside_price_valid_on(self):
        path = ROOT / "ury/api/item_price.py"
        self.assertTrue(path.is_file(), "no shared Item Price determination module")
        self.assertNotIn("def selling_uom(", path.read_text(), "price_valid_on derives the UOM itself")


if __name__ == "__main__":
    unittest.main()
