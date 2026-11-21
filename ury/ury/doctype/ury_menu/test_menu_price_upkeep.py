"""A URY Menu save keeps Item Price history; site-less (frappe double, no bench).

SAP basis: a condition record is never overwritten. A price change is a new
record with its own validity; released history is never deleted.

Ported from the fork (#87 7046c0f4, #89 05691301) onto the version-16 ury_menu
import graph.
"""

import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

TODAY = date(2026, 11, 18)
APP = Path(__file__).resolve().parents[2]  # ury/ury
ITEM_PRICE = APP / "api/item_price.py"


def getdate(value=None):
    if value is None:
        return TODAY
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
    framework.ValidationError = type("ValidationError", (Exception,), {})
    framework.PermissionError = type("PermissionError", (Exception,), {})
    framework._ = lambda *messages: messages[0]

    def throw(message, exception=None, **kwargs):
        raise (exception or framework.ValidationError)(message)

    framework.throw = throw
    return framework, {
        "frappe": framework,
        "frappe.utils": _stub("frappe.utils", flt=lambda value: float(value or 0), getdate=getdate),
        "frappe.model.document": _stub("frappe.model.document", Document=object),
        "ury.ury.api.ury_kot_notification": _stub(
            "ury.ury.api.ury_kot_notification", create_system_notification=MagicMock(),
            get_users_with_role=MagicMock(),
        ),
        "ury.ury.doctype.alert_settings.alert_settings": _stub(
            "ury.ury.doctype.alert_settings.alert_settings", get_alert_rule=MagicMock(),
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
    item_price = _load("ury.ury.api.item_price", ITEM_PRICE) if ITEM_PRICE.is_file() else None
    ury_menu = _load("ury.ury.doctype.ury_menu.ury_menu", APP / "doctype/ury_menu/ury_menu.py")


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

    def add(self, item, rate, valid_from, state=None, uom="Nos"):
        # Item Price.uom is mandatory and fetched from item_code.stock_uom (ERPNext item_price.json).
        row = Row(name=f"IP-{len(self.rows) + 1}", item_code=item, price_list="Menu A",
                  price_list_rate=rate, valid_from=valid_from, valid_upto=None, workflow_state=state, uom=uom,
                  customer=None, supplier=None, batch_no=None)
        self.rows.append(row)
        return row

    def use_workflow(self):
        # Frappe's standard Workflow State styles (frappe/utils/install.py): Approved Success, Rejected Danger.
        self.workflow = {"Draft": "", "Approved": "Success", "Rejected": "Danger"}

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
        if doctype == "Item":
            return {"stock_uom": "Nos", "sales_uom": None}[fieldname]
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
        self.framework.PermissionError = _framework.PermissionError
        self.framework.throw = _framework.throw
        self.framework._ = _framework._
        self.framework.utils.getdate = getdate
        self.prices = FakeItemPrices(self.framework)
        for module in (ury_menu, item_price):
            if module is not None:
                self.enterContext(patch.object(module, "frappe", self.framework))
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
        with self.assertRaisesRegex(_framework.ValidationError, r"\[URY-PRICE-RELEASED-TODAY\]"):
            self.save_menu(Fish=120)
        self.assertEqual((self.prices.inserted, self.prices.saved), ([], []))
        self.assertEqual(self.prices.rate_of(released["name"]), 100)

    def test_rejected_rate_can_be_proposed_again(self):
        self.prices.use_workflow()
        self.prices.add("Fish", 100, "2026-01-01", state="Approved")
        self.prices.add("Fish", 120, "2026-11-17", state="Rejected")
        self.save_menu(Fish=120)
        self.assertEqual([(r["price_list_rate"], r["workflow_state"], getdate(r["valid_from"]))
                          for r in self.prices.inserted], [(120, "Draft", TODAY)])

    def test_unchanged_rate_with_a_real_uom_on_the_row_writes_nothing(self):
        self.prices.add("Fish", 100, "2026-01-01", uom="Nos")
        self.save_menu(Fish=100)
        self.assertEqual((self.prices.inserted, self.prices.saved), ([], []))

    def test_unchanged_rate_under_release_workflow_queues_no_proposal(self):
        self.prices.use_workflow()
        self.prices.add("Fish", 100, "2026-01-01", state="Approved", uom="Nos")
        self.save_menu(Fish=100)
        self.assertEqual((self.prices.inserted, self.prices.saved), ([], []))

    def test_menu_rate_equal_to_released_price_leaves_pending_proposal(self):
        self.prices.use_workflow()
        self.prices.add("Fish", 100, "2026-01-01", state="Approved")
        pending = self.prices.add("Fish", 120, "2026-11-18", state="Draft")
        self.save_menu(Fish=100)
        self.assertEqual((self.prices.inserted, self.prices.saved), ([], []))
        self.assertEqual(self.prices.rate_of(pending["name"]), 120)


if __name__ == "__main__":
    unittest.main()
