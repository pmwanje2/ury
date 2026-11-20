"""Site-less contracts for till clarity (ported from the fork, re-pointed to v16).

M02 / #49, #65, #68: an optional room lookup method on the POS Profile, checked
before a Room Charge, and the cashier's room/folio text sent to the POS Invoice
as room_number. The lookup method is configuration, never a hard-coded hotel
system: it must be a whitelisted method or an enabled API Server Script.

Inert by default: an empty room lookup method keeps the free-text room box."""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe

from ury import hooks
from ury.ury.doctype.ury_order import ury_order
from ury.ury.hooks import ury_pos_profile
from ury.ury_pos import api

ROOT = Path(__file__).resolve().parents[2]
ROOM_LOOKUP_INVALID = "[URY-ROOM-LOOKUP-METHOD-INVALID]"


def _raise(message, exc=frappe.ValidationError, *args, **kwargs):
    raise exc(message)


def _fixture_fields():
    rows = json.loads((ROOT / "fixtures" / "custom_field.json").read_text())
    return {row["name"]: row for row in rows}


def _exported_names():
    fixture = next(row for row in hooks.fixtures if isinstance(row, dict) and row.get("doctype") == "Custom Field")
    return next(row[2] for row in fixture["filters"] if row[:2] == ["name", "in"])


class TestRoomLookupProfileField(unittest.TestCase):
    def setUp(self):
        self.framework = self.enterContext(patch.object(api, "frappe"))
        self.enterContext(patch.object(api, "getBranch", return_value="Branch A"))
        self.enterContext(patch.object(api, "get_default_customer_config", return_value={}))
        self.framework.session.user = "cashier@example.com"
        self.framework.db.exists.return_value = "Till A"
        self.framework.get_single.return_value = frappe._dict(disable_rounded_total=0)
        self.profile = frappe._dict(
            name="Till A", branch="Branch A", warehouse="Stores", company="Company A",
            country="Uganda", table_attention_time=10, print_format="Receipt", paid_limit=0,
            custom_enable_discount=0, custom_enable_multiple_cashier=0,
            custom_edit_order_type=0, custom_enable_kot_reprint=0, qz_print=0,
            applicable_for_users=[frappe._dict(user="cashier@example.com")],
            printer_settings=[],
        )
        self.framework.get_doc.return_value = self.profile

    def test_fixture_declares_an_optional_data_field(self):
        field = _fixture_fields().get("POS Profile-custom_room_lookup_method", {})
        self.assertEqual(field.get("dt"), "POS Profile")
        self.assertEqual(field.get("fieldname"), "custom_room_lookup_method")
        self.assertEqual(field.get("fieldtype"), "Data")
        self.assertFalse(field.get("default"))
        self.assertFalse(field.get("reqd"))
        self.assertIn("POS Profile-custom_room_lookup_method", _exported_names())

    def test_profile_context_exposes_the_configured_method(self):
        self.profile.custom_room_lookup_method = "  pms_app.room_charge_lookup "
        result = api.getPosProfile()
        self.assertEqual(result.get("room_lookup_method"), "pms_app.room_charge_lookup")

    def test_unset_method_is_empty_so_the_till_keeps_free_text(self):
        result = api.getPosProfile()
        self.assertIn("room_lookup_method", result)
        self.assertEqual(result.get("room_lookup_method"), "")


def _whitelisted_lookup(pos_profile=None, room=None):
    return {"room_number": room}


def _plain_function(pos_profile=None, room=None):
    return {"room_number": room}


class TestRoomLookupMethodIsValidated(unittest.TestCase):
    """POS Profile validate refuses a lookup method the till could not call:
    only a whitelisted method or an enabled API Server Script is accepted."""

    def setUp(self):
        self.enterContext(patch.object(ury_pos_profile, "_", side_effect=lambda message: message))
        self.enterContext(patch.object(frappe, "throw", side_effect=_raise))
        self.enterContext(patch.object(frappe, "msgprint"))
        self.api_scripts = {}
        self.enterContext(patch(
            "frappe.core.doctype.server_script.server_script_utils.get_server_script_map",
            side_effect=lambda: {"_api": dict(self.api_scripts)},
        ))
        self.methods = {
            "tests.whitelisted_lookup": _whitelisted_lookup,
            "tests.plain_function": _plain_function,
        }
        self.enterContext(patch.object(frappe, "get_attr", side_effect=self._get_attr))
        self.enterContext(patch.object(frappe, "whitelisted", {_whitelisted_lookup}))

    def _get_attr(self, path):
        if path not in self.methods:
            raise frappe.AppNotInstalledError(f"App {path.split('.')[0]} is not installed")
        return self.methods[path]

    def _validate(self, value):
        doc = frappe._dict(
            name="Till A", custom_room_lookup_method=value,
            printer_settings=[], custom_order_type_customers=[],
        )
        ury_pos_profile.validate(doc, "validate")
        return doc

    def _assert_refused(self, value):
        with self.assertRaises(frappe.ValidationError) as caught:
            self._validate(value)
        self.assertNotIsInstance(caught.exception, frappe.PermissionError)
        self.assertIn(ROOM_LOOKUP_INVALID, str(caught.exception))

    def test_blank_method_is_accepted(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self._validate(value)

    def test_enabled_api_server_script_is_accepted(self):
        self.api_scripts["pms_bridge.room_charge_lookup"] = "Room Charge Lookup"
        self._validate("pms_bridge.room_charge_lookup")

    def test_whitelisted_python_method_is_accepted(self):
        self._validate("tests.whitelisted_lookup")

    def test_a_function_that_is_not_whitelisted_is_refused(self):
        self._assert_refused("tests.plain_function")

    def test_a_method_that_does_not_resolve_is_refused(self):
        self._assert_refused("pms_bridge.room_charge_lookup")

    def test_a_disabled_or_missing_server_script_is_refused(self):
        self.api_scripts["other_bridge.lookup"] = "Other Lookup"
        self._assert_refused("pms_bridge.room_charge_lookup")


class TestRoomChargeSettlement(unittest.TestCase):
    """make_invoice records the cashier's room/folio text as room_number when
    the POS Invoice carries that field (a hotel bridge adds it), before the
    payments are applied; without the field nothing is written."""

    def setUp(self):
        self.invoice = MagicMock()
        self.invoice.name = "INV-1"
        self.invoice.custom_merged_pos_invoice = None
        self.invoice.restaurant_table = None
        self.seen_at_payment = []
        self.invoice.append.side_effect = lambda *args, **kwargs: self.seen_at_payment.append(
            self.invoice.__dict__.get("room_number")
        )
        for target, value in (
            ("_validate_additional_discount", MagicMock(return_value=0)),
            ("get_order_invoice", MagicMock(return_value=self.invoice)),
            ("_submit_and_release_tables", MagicMock()),
        ):
            self.enterContext(patch.object(ury_order, target, value))
        self.enterContext(patch.object(ury_order.frappe, "get_value", return_value="Take Away"))
        self.enterContext(patch.object(
            ury_order.frappe, "get_cached_doc",
            return_value=frappe._dict(company="Company A", currency="UGX"),
        ))
        self.meta = MagicMock()
        self.get_meta = self.enterContext(patch.object(ury_order.frappe, "get_meta", return_value=self.meta))

    def _pay(self, **kwargs):
        # frappe.call is the request path: arguments the method does not
        # declare are dropped, exactly as for a POST from the till.
        return frappe.call(
            ury_order.make_invoice,
            customer="Walk-in", payments=[{"mode_of_payment": "Room Charge", "amount": 100}],
            cashier="cashier@example.com", pos_profile="Till A", owner="cashier@example.com",
            invoice="INV-1", **kwargs,
        )

    def test_room_number_is_set_before_payment_when_the_invoice_has_the_field(self):
        self.meta.has_field.return_value = True
        self._pay(room_number="ROOM-101")
        self.get_meta.assert_called_once_with("POS Invoice")
        self.meta.has_field.assert_called_once_with("room_number")
        self.assertEqual(self.seen_at_payment, ["ROOM-101"])
        self.assertEqual(self.invoice.room_number, "ROOM-101")

    def test_room_number_is_not_written_when_the_invoice_has_no_such_field(self):
        self.meta.has_field.return_value = False
        self._pay(room_number="ROOM-101")
        self.assertEqual(self.seen_at_payment, [None])
        self.assertNotIn("room_number", self.invoice.__dict__)

    def test_settlement_without_a_room_does_not_touch_the_field(self):
        self.meta.has_field.return_value = True
        self._pay()
        self.get_meta.assert_not_called()
        self.assertNotIn("room_number", self.invoice.__dict__)


if __name__ == "__main__":
    unittest.main()
