"""Add Check and till tender configuration; site-less (frappe double, no bench).

merge_bills is Simphony's Add/Transfer Check: it validates the selected open checks (same branch,
same order type, drafts the caller may write) and consolidates their lines onto the primary check
(ury_order.consolidate_checks). Nothing links two invoices, so a check can absorb any number of
others (G12).

getPosProfile gives the till its tender configuration instead of literals (G17, G21): the default
tender is the POS Profile payment row marked Default, and the room-charge tenders are the payment
methods whose company account is the Company's Guest Ledger Clearing account, the same rule the
hotel bridge enforces when it posts a room charge.
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]  # ury/


class Row(dict):
    __getattr__ = dict.get


def _stub(name, **attrs):
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class Refused(Exception):
    pass


class NotPermitted(Exception):
    pass


class Loaded(unittest.TestCase):
    def setUp(self):
        framework = MagicMock()
        framework.PermissionError = NotPermitted
        framework.ValidationError = Refused
        framework._ = lambda message, *args: message
        framework.whitelist = lambda *args, **kwargs: (lambda function: function)
        framework.session = SimpleNamespace(user="cashier@example.com")

        def throw(message, exception=None, **kwargs):
            raise (exception or Refused)(message)

        framework.throw = throw
        framework.get_traceback = lambda *args, **kwargs: "traceback"
        self.frappe = framework
        utils = _stub("frappe.utils", cint=lambda value: int(value or 0), flt=lambda value: float(value or 0),
                      nestedset=MagicMock(), validate_phone_number=MagicMock())
        framework.utils = utils
        self.consolidate = MagicMock()
        self.enterContext(patch.dict(sys.modules, {
            "frappe": framework, "frappe.utils": utils,
            "ury.ury_pos.default_customer": _stub("ury.ury_pos.default_customer",
                                                  get_default_customer_config=lambda profile: {}),
            "ury.ury.api.item_price": _stub("ury.ury.api.item_price", item_price_release_states=MagicMock(),
                                            price_valid_on=MagicMock()),
            "ury.ury.doctype.ury_order.ury_order": _stub("ury.ury.doctype.ury_order.ury_order",
                                                         consolidate_checks=self.consolidate),
        }))
        spec = importlib.util.spec_from_file_location("ury.ury_pos.api", ROOT / "ury_pos/api.py")
        self.api = importlib.util.module_from_spec(spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury_pos.api": self.api}))
        spec.loader.exec_module(self.api)


def open_check(name, **fields):
    check = SimpleNamespace(name=name, branch="Lakeside", order_type="Dine In", docstatus=0,
                            custom_merged_pos_invoice=None, items=[Row(item_code="soda", qty=1)])
    check.__dict__.update(fields)
    return check


class TestAddCheck(Loaded):
    def setUp(self):
        super().setUp()
        self.checks = {name: open_check(name) for name in ("CHK-1", "CHK-2", "CHK-3")}
        self.frappe.get_doc.side_effect = lambda doctype, name, *a, **k: self.checks[name]
        self.frappe.has_permission.return_value = True

    def merge(self, secondary):
        return self.api.merge_bills("CHK-1", secondary)

    def test_the_selected_check_is_consolidated_onto_the_primary(self):
        result = self.merge("CHK-2")
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["name"], "CHK-1")
        self.consolidate.assert_called_once_with(self.checks["CHK-1"], [self.checks["CHK-2"]])
        self.frappe.db.commit.assert_called_once()

    def test_no_merge_link_is_written(self):
        self.merge("CHK-2")
        links = [c for c in self.frappe.db.set_value.call_args_list if "custom_merged_pos_invoice" in str(c)]
        self.assertEqual(links, [])

    def test_several_checks_are_added_in_one_step(self):
        result = self.merge(json.dumps(["CHK-2", "CHK-3"]))
        self.assertEqual(result["status"], "success", result)
        self.consolidate.assert_called_once_with(self.checks["CHK-1"], [self.checks["CHK-2"], self.checks["CHK-3"]])

    def test_a_check_of_another_order_type_is_refused(self):
        self.checks["CHK-2"].order_type = "Take Away"
        result = self.merge("CHK-2")
        self.assertEqual(result["status"], "error")
        self.assertIn("order type", result["message"])
        self.consolidate.assert_not_called()
        self.frappe.db.rollback.assert_called()

    def test_the_primary_cannot_be_added_to_itself_among_others(self):
        result = self.merge(json.dumps(["CHK-2", "CHK-1"]))
        self.assertEqual(result["status"], "error")
        self.consolidate.assert_not_called()

    def test_a_check_of_another_branch_is_refused(self):
        self.checks["CHK-3"].branch = "Hilltop"
        with self.assertRaises(NotPermitted):
            self.merge(json.dumps(["CHK-2", "CHK-3"]))
        self.consolidate.assert_not_called()


class Profile(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


class TestTillTenderConfiguration(Loaded):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(self.api, "getBranch", return_value="Lakeside"))
        self.profile = Profile(
            name="Lakeside Till", branch="Lakeside", warehouse="Stores", company="Lakeside Hotel",
            table_attention_time=10, print_format="Receipt", paid_limit=50, custom_enable_discount=1,
            custom_enable_multiple_cashier=0, custom_edit_order_type=0, custom_enable_kot_reprint=0,
            custom_invoice_warning_time=0, qz_print=0, qz_host=None, printer_settings=[],
            applicable_for_users=[Row(user="cashier@example.com")],
            payments=[Row(mode_of_payment="Cash", default=0), Row(mode_of_payment="Mobile Money", default=1),
                      Row(mode_of_payment="Guest Folio", default=0)],
        )
        self.frappe.db.exists.return_value = "Lakeside Till"
        self.frappe.get_doc.side_effect = lambda doctype, name, *a, **k: self.profile
        self.frappe.get_single.return_value = SimpleNamespace(disable_rounded_total=0)
        self.accounts = {"Cash": "Cash - LH", "Mobile Money": "MoMo - LH", "Guest Folio": "Guest Ledger Clearing - LH"}
        self.clearing = "Guest Ledger Clearing - LH"
        self.has_clearing_field = True
        self.frappe.get_meta.side_effect = lambda doctype: SimpleNamespace(
            has_field=lambda field: doctype == "Company" and field == "custom_guest_ledger_clearing"
            and self.has_clearing_field)
        self.frappe.db.get_value.side_effect = self.value

    def value(self, doctype, filters=None, fieldname=None, *args, **kwargs):
        if doctype == "Mode of Payment Account":
            assert filters["company"] == "Lakeside Hotel", filters
            return self.accounts.get(filters["parent"])
        if doctype == "Company" and fieldname == "custom_guest_ledger_clearing":
            assert self.has_clearing_field, "read a Company field this site does not have"
            return self.clearing
        if doctype == "Company" and fieldname == "country":
            return "Uganda"
        return None

    def test_the_default_tender_is_the_profiles_default_payment_row(self):
        self.assertEqual(self.api.getPosProfile()["default_payment_mode"], "Mobile Money")

    def test_room_charge_tenders_are_the_methods_posting_to_guest_ledger_clearing(self):
        self.assertEqual(self.api.getPosProfile()["room_charge_modes"], ["Guest Folio"])

    def test_a_site_without_guest_ledger_clearing_has_no_room_charge_tender(self):
        self.has_clearing_field = False
        self.assertEqual(self.api.getPosProfile()["room_charge_modes"], [])

    def test_an_unset_clearing_account_never_matches_a_method_without_an_account(self):
        self.clearing = None
        self.accounts["Cash"] = None
        self.assertEqual(self.api.getPosProfile()["room_charge_modes"], [])


if __name__ == "__main__":
    unittest.main()
