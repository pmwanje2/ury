"""Price determination on the pricing date; site-less like test_cancel_order_reason.py.

SAP basis: price determination uses the condition record valid on the pricing
date; a condition record that is not released is not used in pricing. Here the
condition record is an ERPNext Item Price on the menu's price list.
"""

import ast
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

TODAY = date(2026, 11, 18)


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
    utils = ModuleType("frappe.utils")
    utils.cint = lambda value: int(value or 0)
    utils.flt = lambda value: float(value or 0)
    utils.get_datetime = lambda value: value
    utils.get_system_timezone = lambda: "Africa/Kampala"
    utils.now_datetime = lambda: None
    utils.getdate = getdate
    document = ModuleType("frappe.model.document")
    document.Document = object
    queries = ModuleType("erpnext.controllers.queries")
    queries.item_query = MagicMock()
    pos_api = ModuleType("ury.ury_pos.api")
    for name in ("getBranch", "getBranchRoom", "getRoom", "posOpening"):
        setattr(pos_api, name, MagicMock())
    kot = ModuleType("ury.ury.api.ury_kot_generate")
    kot.kot_execute = MagicMock()
    kot.process_items_for_cancel_kot = MagicMock()
    return framework, {
        "frappe": framework,
        "frappe.utils": utils,
        "frappe.model.document": document,
        "erpnext.controllers.queries": queries,
        "ury.ury_pos.api": pos_api,
        "ury.ury.api.ury_kot_generate": kot,
    }


_framework, _modules = _site_less_frappe()
with patch.dict(sys.modules, _modules):
    from ury.ury.api import item_price
    from ury.ury.doctype.ury_order import ury_order


class FakePriceBook:
    """Item Price rows in creation order, plus an optional Item Price Workflow."""

    def __init__(self, framework):
        self.rows = []
        self.workflow = None  # {"name", "field", "states": {state: style}}
        framework.db.get_list.side_effect = self.get_list
        framework.db.get_value.side_effect = self.get_value
        framework.get_all.side_effect = self.get_all
        framework.get_cached_doc.side_effect = self.get_cached_doc

    def add(self, rate, valid_from=None, valid_upto=None, state=None, item="Fish", price_list="Menu A"):
        self.rows.append(Row(
            name=f"IP-{len(self.rows) + 1}", item_code=item, price_list=price_list,
            price_list_rate=rate, valid_from=valid_from, valid_upto=valid_upto, workflow_state=state,
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
        return None

    def get_cached_doc(self, doctype, name):
        assert (doctype, name) == ("Workflow", self.workflow["name"]), (doctype, name)
        return Row(states=[Row(state=state) for state in self.workflow["states"]])

    def get_all(self, doctype, filters=None, pluck=None, **kwargs):
        assert doctype == "Workflow State", doctype
        assert pluck == "name", pluck
        names = filters["name"][1]
        return [n for n in names if self.workflow["states"].get(n) == filters["style"]]


class TestPriceDetermination(unittest.TestCase):
    def setUp(self):
        self.framework = MagicMock()
        self.framework.ValidationError = _framework.ValidationError
        self.framework.throw = _framework.throw
        self.framework.utils.getdate = getdate
        self.book = FakePriceBook(self.framework)
        for module in (ury_order, item_price):
            patcher = patch.object(module, "frappe", self.framework)
            patcher.start()
            self.addCleanup(patcher.stop)

    def bill(self, posting_date=None):
        extra = {"posting_date": posting_date} if posting_date else {}
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
        with self.assertRaises(_framework.ValidationError):
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
        with self.assertRaises(_framework.ValidationError):
            self.bill()

    def test_same_valid_from_latest_record_wins(self):
        self.book.add(100, "2026-11-01")
        self.book.add(105, "2026-11-01")
        self.assertEqual(self.bill(), 105)

    def test_without_a_release_workflow_every_record_is_released(self):
        self.book.add(100, "2026-01-01", state="Draft")
        self.assertEqual(self.bill(), 100)


class TestInvoicePricingDate(unittest.TestCase):
    def setUp(self):
        framework = MagicMock()
        framework.utils.getdate = getdate
        patcher = patch.object(ury_order, "frappe", framework)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_open_invoice_prices_on_today(self):
        invoice = Row(posting_date="2026-11-01", set_posting_time=0)
        self.assertEqual(ury_order.invoice_pricing_date(invoice), TODAY)

    def test_back_dated_invoice_prices_on_its_posting_date(self):
        invoice = Row(posting_date="2026-11-01", set_posting_time=1)
        self.assertEqual(ury_order.invoice_pricing_date(invoice), date(2026, 11, 1))


class TestCallersPassThePricingDate(unittest.TestCase):
    def test_every_caller_passes_posting_date(self):
        root = Path(__file__).resolve().parents[2]
        for relative in ("doctype/ury_order/ury_order.py", "api/self_ordering.py"):
            tree = ast.parse((root / relative).read_text())
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "price_items_for_invoice"
            ]
            self.assertTrue(calls, relative)
            for call in calls:
                self.assertIn("posting_date", [k.arg for k in call.keywords], relative)


if __name__ == "__main__":
    unittest.main()
