"""Price determination on the pricing date; site-less (frappe double, no bench).

SAP basis: price determination uses the condition record valid on the pricing
date; a condition record that is not released is not used in pricing. Here the
condition record is an ERPNext Item Price on the menu's price list. Candidate
filter and priority follow ERPNext get_item_price (erpnext 16.53.0,
erpnext/stock/get_item_details.py _get_item_price_query, _order_item_prices and
the stock-UOM retry in get_price_list_rate_for).

Ported from the fork (#87 7046c0f4, #89 05691301, #90 688b596b) onto the
version-16 ury_order import graph.
"""

import ast
import importlib.util
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

TODAY = date(2026, 11, 18)
APP = Path(__file__).resolve().parents[2]  # ury/ury
ITEM_PRICE = APP / "api/item_price.py"


def getdate(value=None):
    if value is None:
        return TODAY
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class Row(dict):
    __getattr__ = dict.get


def _stub(name, **attrs):
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _site_less_frappe():
    framework = MagicMock()
    framework.PermissionError = type("PermissionError", (Exception,), {})
    framework.ValidationError = type("ValidationError", (Exception,), {})
    framework._ = lambda *messages: messages[0]
    framework.whitelist = lambda *args, **kwargs: (lambda function: function)
    framework.cache = MagicMock()

    def throw(message, exception=None, **kwargs):
        raise (exception or framework.ValidationError)(message)

    framework.throw = throw
    utils = _stub(
        "frappe.utils",
        cint=lambda value: int(value or 0),
        flt=lambda value: float(value or 0),
        get_datetime=lambda value: value,
        get_system_timezone=lambda: "Africa/Kampala",
        now_datetime=lambda: None,
        getdate=getdate,
    )
    return framework, {
        "frappe": framework,
        "frappe.utils": utils,
        "frappe.model.document": _stub("frappe.model.document", Document=object),
        "erpnext.controllers.queries": _stub("erpnext.controllers.queries", item_query=MagicMock()),
        "ury.ury_pos.api": _stub(
            "ury.ury_pos.api", getBranch=MagicMock(), getBranchRoom=MagicMock(), getRoom=MagicMock(),
            posOpening=MagicMock(),
        ),
        "ury.ury_pos.default_customer": _stub(
            "ury.ury_pos.default_customer", get_default_customer_config=MagicMock(),
            resolve_default_customer=MagicMock(),
        ),
        "ury.ury.api.ury_kot_generate": _stub(
            "ury.ury.api.ury_kot_generate", _execute_kot=MagicMock(), process_items_for_cancel_kot=MagicMock(),
        ),
        "ury.ury.api.ury_order_reservation_service": _stub(
            "ury.ury.api.ury_order_reservation_service", _line_key=MagicMock(), _line_ref=MagicMock(),
            reconcile_order_reservations=MagicMock(), release_order_reservations=MagicMock(),
            resolve_production_context=MagicMock(return_value=None), _warehouse_for_context=MagicMock(),
        ),
        "ury.ury.api.ury_stock_policy": _stub("ury.ury.api.ury_stock_policy", get_branch_stock_policy=MagicMock()),
        "ury.ury.doctype.alert_settings.alert_settings": _stub(
            "ury.ury.doctype.alert_settings.alert_settings", get_alert_rule=MagicMock(),
        ),
        "ury.ury.api.ury_kot_notification": _stub(
            "ury.ury.api.ury_kot_notification", create_system_notification=MagicMock(),
            get_users_with_role=MagicMock(),
        ),
    }


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_framework, _modules = _site_less_frappe()
with patch.dict(sys.modules, _modules):
    # A fresh item_price module bound to the double; absent until the shared price determination exists.
    item_price = _load("ury.ury.api.item_price", ITEM_PRICE) if ITEM_PRICE.is_file() else None
    ury_order = _load("ury.ury.doctype.ury_order.ury_order", APP / "doctype/ury_order/ury_order.py")


def _require(module, name):
    found = getattr(module, name, None) if module else None
    if found is None:
        raise AssertionError(f"{name} is missing: no shared Item Price determination")
    return found


class FakePriceBook:
    """Item Price rows in creation order, plus an optional Item Price Workflow."""

    def __init__(self, framework):
        self.rows = []
        self.sales_uom = None
        self.workflow = None  # {"name", "field", "states": {state: style}}
        framework.db.get_list.side_effect = self.get_list
        framework.db.get_value.side_effect = self.get_value
        framework.get_all.side_effect = self.get_all
        framework.get_cached_doc.side_effect = self.get_cached_doc

    def add(self, rate, valid_from=None, valid_upto=None, state=None, item="Fish", price_list="Menu A",
            uom="Nos", customer=None, supplier=None, batch_no=None):
        self.rows.append(Row(
            name=f"IP-{len(self.rows) + 1}", item_code=item, price_list=price_list,
            price_list_rate=rate, valid_from=valid_from, valid_upto=valid_upto, workflow_state=state,
            uom=uom, customer=customer, supplier=supplier, batch_no=batch_no,
        ))

    def use_workflow(self, states, field="workflow_state"):
        self.workflow = {"name": "Menu Price Release", "field": field, "states": states}

    def get_list(self, doctype, filters=None, fields=None, **kwargs):
        assert doctype == "Item Price", doctype
        rows = [r for r in self.rows if all(r.get(k) == v for k, v in (filters or {}).items())]
        return [Row({f: r.get(f) for f in fields}) for r in rows]

    def get_value(self, doctype, filters, fieldname=None, *args, **kwargs):
        if doctype == "Workflow":
            assert filters == {"document_type": "Item Price", "is_active": 1}, filters
            if not self.workflow:
                return None
            return {"workflow_state_field": self.workflow["field"], "name": self.workflow["name"]}[fieldname]
        if doctype == "POS Profile":
            return "CC"
        if doctype == "Item":
            return {"stock_uom": "Nos", "sales_uom": self.sales_uom}[fieldname]
        return None

    def get_cached_doc(self, doctype, name):
        assert (doctype, name) == ("Workflow", self.workflow["name"]), (doctype, name)
        return Row(states=[Row(state=state) for state in self.workflow["states"]])

    def get_all(self, doctype, filters=None, pluck=None, **kwargs):
        assert doctype == "Workflow State", doctype
        assert pluck == "name", pluck
        names = filters["name"][1]
        return [n for n in names if self.workflow["states"].get(n) == filters["style"]]


class PricedOnTheDouble(unittest.TestCase):
    def setUp(self):
        self.framework = MagicMock()
        self.framework.ValidationError = _framework.ValidationError
        self.framework.PermissionError = _framework.PermissionError
        self.framework.throw = _framework.throw
        self.framework._ = _framework._
        self.framework.utils.getdate = getdate
        self.book = FakePriceBook(self.framework)
        for module in (ury_order, item_price):
            if module is not None:
                self.enterContext(patch.object(module, "frappe", self.framework))


class TestPriceDetermination(PricedOnTheDouble):
    def bill(self, posting_date=None, customer=None):
        extra = {"posting_date": posting_date} if posting_date else {}
        if customer:
            extra["customer"] = customer
        rows = ury_order.price_items_for_invoice(
            [{"item": "Fish", "item_name": "Fish", "qty": 1}], "Menu A", "POS A", "Branch A", "Menu", **extra,
        )
        return rows[0]["rate"]

    def test_most_recent_valid_from_wins(self):
        self.book.add(100, "2026-01-01")
        self.book.add(130, "2026-11-01")
        self.assertEqual(self.bill(), 130)

    def test_expired_record_is_not_used(self):
        self.book.add(90, "2025-01-01", valid_upto="2026-06-30")
        self.book.add(110, "2026-07-01")
        self.assertEqual(self.bill(), 110)

    def test_expired_promotion_falls_back_to_the_base_price(self):
        self.book.add(110, "2026-01-01")
        self.book.add(90, "2026-11-01", valid_upto="2026-11-10")
        self.assertEqual(self.bill(), 110)

    def test_record_valid_through_the_pricing_date_is_used(self):
        self.book.add(90, "2025-01-01", valid_upto="2026-11-18")
        self.assertEqual(self.bill(), 90)

    def test_future_record_is_not_used_yet(self):
        self.book.add(150, "2027-01-01")
        self.book.add(100, "2026-01-01")
        self.assertEqual(self.bill(), 100)

    def test_record_without_valid_from_is_the_oldest(self):
        self.book.add(120, "2026-03-01")
        self.book.add(80, None)
        self.assertEqual(self.bill(), 120)

    def test_pricing_date_is_the_posting_date(self):
        self.book.add(100, "2026-01-01")
        self.book.add(130, "2026-11-01")
        self.assertEqual(self.bill(posting_date=date(2026, 10, 1)), 100)

    def test_no_record_valid_on_the_pricing_date_refuses(self):
        self.book.add(150, "2027-01-01")
        with self.assertRaisesRegex(_framework.ValidationError, r"\[URY-PRICE-NOT-VALID\]"):
            self.bill()

    def test_release_workflow_skips_unreleased_records(self):
        self.book.use_workflow({"Draft": "", "Approved": "Success", "Rejected": "Danger"})
        self.book.add(80, "2025-01-01", state="Draft")
        self.book.add(100, "2026-01-01", state="Approved")
        self.book.add(140, "2026-11-10", state="Draft")
        self.book.add(160, "2026-11-12", state="Rejected")
        self.assertEqual(self.bill(), 100)

    def test_release_workflow_reads_its_own_state_field(self):
        self.book.use_workflow({"Draft": "", "Released": "Success"}, field="custom_release")
        self.book.add(100, "2026-01-01")
        self.book.rows[-1]["custom_release"] = "Released"
        self.book.add(140, "2026-11-10")
        self.book.rows[-1]["custom_release"] = "Draft"
        self.assertEqual(self.bill(), 100)

    def test_release_workflow_without_a_released_state_refuses(self):
        self.book.use_workflow({"Draft": "", "Pending": "Warning"})
        self.book.add(100, "2026-01-01", state="Pending")
        with self.assertRaisesRegex(_framework.ValidationError, r"\[URY-PRICE-NO-RELEASE-STATE\]"):
            self.bill()

    def test_same_valid_from_latest_record_wins(self):
        self.book.add(100, "2026-11-01")
        self.book.add(105, "2026-11-01")
        self.assertEqual(self.bill(), 105)

    def test_without_a_release_workflow_every_record_is_released(self):
        self.book.add(100, "2026-01-01", state="Draft")
        self.assertEqual(self.bill(), 100)

    def test_record_valid_from_the_pricing_date_is_used(self):
        self.book.add(100, "2026-01-01")
        self.book.add(130, "2026-11-18")
        self.assertEqual(self.bill(), 130)

    # ERPNext get_item_price is the native rule the till must match.

    def test_price_in_another_uom_is_not_used(self):
        self.book.add(100, "2026-01-01")
        self.book.add(60, "2026-11-01", uom="Plate")
        self.assertEqual(self.bill(), 100)

    def test_price_without_a_uom_is_used(self):
        self.book.add(100, "2026-01-01", uom=None)
        self.assertEqual(self.bill(), 100)

    def test_another_customers_price_is_not_used(self):
        self.book.add(100, "2026-01-01")
        self.book.add(80, "2026-11-01", customer="Glovo")
        self.assertEqual(self.bill(customer="Walk-in"), 100)

    def test_a_bill_without_a_customer_uses_only_party_less_prices(self):
        self.book.add(100, "2026-01-01")
        self.book.add(80, "2026-11-01", customer="Glovo")
        self.assertEqual(self.bill(), 100)

    def test_supplier_price_is_not_used_for_a_sale(self):
        self.book.add(100, "2026-01-01")
        self.book.add(70, "2026-11-01", supplier="Fresh Fish Ltd")
        self.assertEqual(self.bill(customer="Walk-in"), 100)

    def test_this_customers_price_wins_over_a_newer_general_price(self):
        self.book.add(100, "2026-11-10")
        self.book.add(80, "2026-01-01", customer="Glovo")
        self.assertEqual(self.bill(customer="Glovo"), 80)

    def test_same_valid_from_prefers_the_row_in_the_line_uom_over_a_uom_less_row(self):
        # ERPNext _order_item_prices: ... valid_from desc, then uom desc (a UOM row before an empty one).
        self.book.add(105, "2026-11-01", uom="Nos")
        self.book.add(100, "2026-11-01", uom=None)
        self.assertEqual(self.bill(), 105)

    def test_no_price_in_the_sales_uom_retries_in_the_stock_uom(self):
        # ERPNext get_price_list_rate_for: no row for the line UOM -> retry with the stock UOM.
        self.book.sales_uom = "Plate"
        self.book.add(100, "2026-01-01", uom="Nos")
        self.assertEqual(self.bill(), 100)

    def test_a_price_in_the_sales_uom_wins_over_the_stock_uom(self):
        self.book.sales_uom = "Plate"
        self.book.add(100, "2026-01-01", uom="Nos")
        self.book.add(160, "2026-01-01", uom="Plate")
        self.assertEqual(self.bill(), 160)

    def test_batch_price_is_not_used_for_a_line_without_a_batch(self):
        self.book.add(100, "2026-01-01")
        self.book.add(50, "2026-11-01", batch_no="B-1")
        self.assertEqual(self.bill(), 100)


class TestReleaseStates(PricedOnTheDouble):
    """item_price_release_states itself refuses, so no caller can bill through an unreleasable workflow."""

    def test_workflow_without_a_released_state_refuses(self):
        release_states = _require(item_price, "item_price_release_states")
        self.book.use_workflow({"Draft": "", "Pending": "Warning"})
        with self.assertRaisesRegex(_framework.ValidationError, r"\[URY-PRICE-NO-RELEASE-STATE\]"):
            release_states()

    def test_workflow_with_a_released_state_returns_its_field_and_states(self):
        release_states = _require(item_price, "item_price_release_states")
        self.book.use_workflow({"Draft": "", "Approved": "Success"}, field="custom_release")
        self.assertEqual(release_states(), ("custom_release", {"Approved"}))

    def test_no_active_workflow_releases_every_record(self):
        release_states = _require(item_price, "item_price_release_states")
        self.assertEqual(release_states(), (None, None))


class TestInvoicePricingDate(unittest.TestCase):
    def setUp(self):
        framework = MagicMock()
        framework.utils.getdate = getdate
        self.enterContext(patch.object(ury_order, "frappe", framework))

    def test_open_invoice_prices_on_today(self):
        pricing_date = _require(ury_order, "invoice_pricing_date")
        invoice = Row(posting_date="2026-11-01", set_posting_time=0)
        self.assertEqual(pricing_date(invoice), TODAY)

    def test_back_dated_invoice_prices_on_its_posting_date(self):
        pricing_date = _require(ury_order, "invoice_pricing_date")
        invoice = Row(posting_date="2026-11-01", set_posting_time=1)
        self.assertEqual(pricing_date(invoice), date(2026, 11, 1))


class TestCallersPassThePricingDate(unittest.TestCase):
    def test_every_caller_passes_posting_date_and_customer(self):
        for relative in ("doctype/ury_order/ury_order.py", "api/self_ordering.py"):
            tree = ast.parse((APP / relative).read_text())
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "price_items_for_invoice"
            ]
            self.assertTrue(calls, relative)
            for call in calls:
                self.assertIn("posting_date", [k.arg for k in call.keywords], relative)
                self.assertIn("customer", [k.arg for k in call.keywords], relative)


class TestMenuNameForABranch(unittest.TestCase):
    """get_menu_name(order_type, branch=...) resolves the menu of that branch, so a guest invoice
    (override_branch) and its pre-order price use the profile branch, not the elevated user's."""

    def setUp(self):
        self.framework = MagicMock()
        restaurants = {"Branch A": "Restaurant A", "Branch B": "Restaurant B"}
        self.framework.get_value.side_effect = lambda doctype, filters, field: restaurants[filters["branch"]]
        menus = {("Restaurant A", "active_menu"): "Menu A", ("Restaurant B", "active_menu"): "Menu B",
                 ("Restaurant A", "order_type_wise_menu"): 0, ("Restaurant B", "order_type_wise_menu"): 1}
        self.framework.db.get_value.side_effect = lambda doctype, filters, field: (
            "Menu B Pickup" if doctype == "Order Type Menu" else menus[(filters, field)])
        self.enterContext(patch.object(ury_order, "frappe", self.framework))
        self.enterContext(patch.object(ury_order, "getBranch", return_value="Branch A"))

    def test_named_branch_wins_over_the_session_users_branch(self):
        self.assertEqual(ury_order.get_menu_name("Take Away", branch="Branch B"), "Menu B Pickup")

    def test_without_a_branch_the_session_users_branch_is_used(self):
        self.assertEqual(ury_order.get_menu_name("Take Away"), "Menu A")

    def test_guest_invoice_resolves_its_menu_for_the_override_branch(self):
        tree = ast.parse((APP / "doctype/ury_order/ury_order.py").read_text())
        resolver = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "_resolve_or_create_pos_invoice")
        calls = [node for node in ast.walk(resolver)
                 if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "get_menu_name"]
        self.assertEqual(len(calls), 1)
        self.assertEqual([k.arg for k in calls[0].keywords], ["branch"])


if __name__ == "__main__":
    unittest.main()
