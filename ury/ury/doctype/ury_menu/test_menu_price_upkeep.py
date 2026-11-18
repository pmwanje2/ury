"""A URY Menu save keeps Item Price history; site-less like test_cancel_order_reason.py.

SAP basis: a condition record is never overwritten. A price change is a new
record with its own validity; released history is never deleted.
"""

import sys
import unittest
from datetime import date
from types import ModuleType
from unittest.mock import MagicMock, patch

TODAY = date(2026, 11, 18)


def getdate(value=None):
    if value is None:
        return TODAY
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class Row(dict):
    __getattr__ = dict.get


def _site_less_frappe():
    framework = MagicMock()
    framework.ValidationError = type("ValidationError", (Exception,), {})
    framework._ = lambda *messages: messages[0]

    def throw(message, exception=None, **kwargs):
        raise (exception or framework.ValidationError)(message)

    framework.throw = throw
    utils = ModuleType("frappe.utils")
    utils.flt = lambda value: float(value or 0)
    utils.getdate = getdate
    document = ModuleType("frappe.model.document")
    document.Document = object
    return framework, {"frappe": framework, "frappe.utils": utils, "frappe.model.document": document}


_framework, _modules = _site_less_frappe()
with patch.dict(sys.modules, _modules):
    from ury.ury.doctype.ury_menu import ury_menu


class FakeItemPrices:
    """Item Price rows, plus an optional Item Price Workflow whose first state is the draft."""

    def __init__(self, framework):
        self.rows = []
        self.workflow = None
        self.inserted = []
        self.saved = []
        framework.db.get_list.side_effect = self.get_list
        framework.db.get_value.side_effect = self.get_value
        framework.get_all.side_effect = self.get_all
        framework.get_cached_doc.side_effect = self.get_cached_doc
        framework.get_doc.side_effect = self.get_doc
        framework.db.sql.side_effect = AssertionError("a menu save must not run raw SQL on Item Price")
        framework.delete_doc.side_effect = AssertionError("a menu save must not delete an Item Price")

    def add(self, item, rate, valid_from, state=None):
        row = Row(name=f"IP-{len(self.rows) + 1}", item_code=item, price_list="Menu A",
                  price_list_rate=rate, valid_from=valid_from, valid_upto=None, workflow_state=state)
        self.rows.append(row)
        return row

    def use_workflow(self):
        self.workflow = {"Draft": "", "Approved": "Success"}

    def rate_of(self, name):
        return next(r for r in self.rows if r["name"] == name)["price_list_rate"]

    def matches(self, row, filters):
        for key, value in (filters or {}).items():
            if key == "valid_from":
                if row.get("valid_from") is None or getdate(row["valid_from"]) != getdate(value):
                    return False
            elif row.get(key) != value:
                return False
        return True

    def get_list(self, doctype, filters=None, fields=None, **kwargs):
        assert doctype == "Item Price", doctype
        return [Row({f: r.get(f) for f in fields}) for r in self.rows if self.matches(r, filters)]

    def get_all(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
        if doctype == "Workflow State":
            return [n for n in filters["name"][1] if self.workflow.get(n) == filters["style"]]
        assert doctype == "Item Price", doctype
        return [Row({f: r.get(f) for f in fields}) for r in self.rows if self.matches(r, filters)]

    def get_value(self, doctype, filters, fieldname=None, *args, **kwargs):
        assert doctype == "Workflow", doctype
        if not self.workflow:
            return None
        return {"workflow_state_field": "workflow_state", "name": "Menu Price Release"}[fieldname]

    def get_cached_doc(self, doctype, name):
        return Row(states=[Row(state=state) for state in self.workflow])

    def get_doc(self, doctype, name=None):
        if isinstance(doctype, dict):
            values = dict(doctype)
            doc = MagicMock()
            doc.insert.side_effect = lambda **kwargs: self.insert(values)
            return doc
        assert doctype == "Item Price", doctype
        row = next(r for r in self.rows if r["name"] == name)
        doc = MagicMock()
        doc.price_list_rate = row["price_list_rate"]
        doc.save.side_effect = lambda **kwargs: self.save(row, doc)
        return doc

    def insert(self, values):
        assert values["doctype"] == "Item Price"
        state = next(iter(self.workflow)) if self.workflow else None
        row = self.add(values["item_code"], values["price_list_rate"], values.get("valid_from"), state)
        assert row["price_list"] == values["price_list"]
        self.inserted.append(row)

    def save(self, row, doc):
        row["price_list_rate"] = doc.price_list_rate
        self.saved.append(row["name"])


class TestMenuSaveKeepsPriceHistory(unittest.TestCase):
    def setUp(self):
        self.framework = MagicMock()
        self.framework.ValidationError = _framework.ValidationError
        self.framework.throw = _framework.throw
        self.framework.utils.getdate = getdate
        self.prices = FakeItemPrices(self.framework)
        # Patch the item_price module that ury_menu bound; another suite may have loaded its own copy.
        shared = ury_menu.price_valid_on.__globals__
        for namespace in (vars(ury_menu), shared):
            patcher = patch.dict(namespace, {"frappe": self.framework})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.menu = ury_menu.URYMenu.__new__(ury_menu.URYMenu)
        self.menu.name = "Menu"
        self.menu.db_set = MagicMock()
        self.menu.get_price_list = MagicMock(return_value=Row(name="Menu A"))

    def save_menu(self, **rates):
        self.menu.items = [Row(item=item, rate=rate) for item, rate in rates.items()]
        self.menu.make_price_list()

    def test_unchanged_rates_write_nothing(self):
        self.prices.add("Fish", 100, "2026-01-01")
        self.prices.add("Tea", 20, "2026-01-01")
        self.save_menu(Fish=100, Tea=20)
        self.assertEqual((self.prices.inserted, self.prices.saved), ([], []))
        self.assertEqual(len(self.prices.rows), 2)

    def test_changed_rate_is_a_new_record_from_today(self):
        old = self.prices.add("Fish", 100, "2026-01-01")
        self.save_menu(Fish=120)
        self.assertEqual(self.prices.rate_of(old["name"]), 100)
        self.assertEqual([(r["item_code"], r["price_list_rate"], getdate(r["valid_from"]))
                          for r in self.prices.inserted], [("Fish", 120, TODAY)])

    def test_only_the_changed_line_is_written(self):
        self.prices.add("Fish", 100, "2026-01-01")
        self.prices.add("Tea", 20, "2026-01-01")
        self.save_menu(Fish=100, Tea=25)
        self.assertEqual([(r["item_code"], r["price_list_rate"]) for r in self.prices.inserted], [("Tea", 25)])

    def test_new_menu_item_gets_a_price(self):
        self.save_menu(Fish=100)
        self.assertEqual([(r["item_code"], r["price_list_rate"]) for r in self.prices.inserted], [("Fish", 100)])

    def test_removed_menu_item_keeps_its_prices(self):
        self.prices.add("Fish", 100, "2026-01-01")
        self.prices.add("Tea", 20, "2026-01-01")
        self.save_menu(Fish=100)
        self.assertEqual(sorted(r["item_code"] for r in self.prices.rows), ["Fish", "Tea"])

    def test_same_day_correction_without_release_workflow_updates_todays_row(self):
        self.prices.add("Fish", 100, "2026-01-01")
        today = self.prices.add("Fish", 120, "2026-11-18")
        self.save_menu(Fish=125)
        self.assertEqual(self.prices.inserted, [])
        self.assertEqual(self.prices.saved, [today["name"]])
        self.assertEqual(self.prices.rate_of(today["name"]), 125)
        self.assertEqual(self.prices.rate_of("IP-1"), 100)

    def test_release_workflow_change_is_a_draft_proposal_and_released_row_stays(self):
        self.prices.use_workflow()
        released = self.prices.add("Fish", 100, "2026-01-01", state="Approved")
        self.save_menu(Fish=120)
        self.assertEqual(self.prices.rate_of(released["name"]), 100)
        self.assertEqual([(r["price_list_rate"], r["workflow_state"]) for r in self.prices.inserted], [(120, "Draft")])

    def test_release_workflow_updates_todays_pending_proposal(self):
        self.prices.use_workflow()
        self.prices.add("Fish", 100, "2026-01-01", state="Approved")
        pending = self.prices.add("Fish", 120, "2026-11-18", state="Draft")
        self.save_menu(Fish=130)
        self.assertEqual(self.prices.inserted, [])
        self.assertEqual(self.prices.rate_of(pending["name"]), 130)

    def test_release_workflow_does_not_duplicate_an_earlier_pending_proposal(self):
        self.prices.use_workflow()
        self.prices.add("Fish", 100, "2026-01-01", state="Approved")
        self.prices.add("Fish", 120, "2026-11-17", state="Draft")
        self.save_menu(Fish=120)
        self.assertEqual((self.prices.inserted, self.prices.saved), ([], []))

    def test_release_workflow_refuses_to_change_a_price_released_today(self):
        self.prices.use_workflow()
        released = self.prices.add("Fish", 100, "2026-11-18", state="Approved")
        with self.assertRaises(_framework.ValidationError):
            self.save_menu(Fish=120)
        self.assertEqual((self.prices.inserted, self.prices.saved), ([], []))
        self.assertEqual(self.prices.rate_of(released["name"]), 100)

    def test_menu_rate_equal_to_released_price_leaves_pending_proposal(self):
        self.prices.use_workflow()
        self.prices.add("Fish", 100, "2026-01-01", state="Approved")
        pending = self.prices.add("Fish", 120, "2026-11-18", state="Draft")
        self.save_menu(Fish=100)
        self.assertEqual((self.prices.inserted, self.prices.saved), ([], []))
        self.assertEqual(self.prices.rate_of(pending["name"]), 120)


if __name__ == "__main__":
    unittest.main()
