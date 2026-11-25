"""Site-less contracts for POS settlement through a Frappe API Server Script."""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ury import hooks
from ury.ury_pos import room_charge


METHOD = "hospitality_kamra.room_charge_lookup"
SCRIPT = "Room Charge Lookup"


class ValidationError(Exception):
    pass


class PermissionError(Exception):
    pass


class AppNotInstalledError(Exception):
    pass


class Framework(SimpleNamespace):
    @property
    def form_dict(self):
        return self.local.form_dict

    @property
    def response(self):
        return self.local.response


def throw(message, exc):
    raise exc(message)


class TestRoomChargeScriptInterop(unittest.TestCase):
    def setUp(self):
        self.form = {"cmd": "submit_invoice", "room": "outer room"}
        self.response = {"message": {"invoice": "outer invoice"}, "docs": ["outer doc"]}
        self.local = SimpleNamespace(form_dict=self.form, response=self.response, message_log=["outer message"])
        self.payload = {
            "ok": 1,
            "room_number": "101",
            "guest_name": "Alice",
            "departure": "2026-11-26",
            "no_post": 0,
        }
        self.script_row = {"name": SCRIPT, "script_type": "API", "api_method": METHOD, "disabled": 0}
        self.script = SimpleNamespace(execute_method=Mock(side_effect=self.execute_script))
        self.framework = Framework(
            local=self.local,
            _dict=dict,
            ValidationError=ValidationError,
            PermissionError=PermissionError,
            AppNotInstalledError=AppNotInstalledError,
            throw=throw,
            get_attr=Mock(side_effect=self.raise_app_not_installed),
            get_doc=Mock(return_value=self.script),
            get_meta=Mock(return_value=SimpleNamespace(has_field=lambda field: True)),
            db=SimpleNamespace(get_value=Mock(side_effect=self.db_value)),
        )
        self.enterContext(patch.object(room_charge, "frappe", self.framework))
        self.enterContext(patch.object(room_charge, "_", lambda message: message))
        self.profile = SimpleNamespace(
            name="Hotel Till",
            custom_room_lookup_method=METHOD,
            payments=[{"mode_of_payment": "Room Charge"}],
        )
        self.invoice = SimpleNamespace(name="POS-1", company="Hotel", currency="UGX", flags=SimpleNamespace())
        self.script_args = None

    def raise_app_not_installed(self, path):
        self.local.message_log.append(f"App for {path} is not installed")
        raise AppNotInstalledError(path)

    def db_value(self, doctype, filters, field):
        if doctype in ("Company", "Mode of Payment Account"):
            return "Guest Ledger"
        if doctype == "Server Script" and self.script_row:
            if all(self.script_row.get(key) == val for key, val in filters.items()):
                return self.script_row[field]

    def execute_script(self):
        self.script_args = dict(self.framework.form_dict)
        self.framework.form_dict["room"] = "mutated in script"
        # Frappe's safe-exec globals only expose a truthy response.
        if self.framework.response:
            self.framework.response["message"] = self.payload
            self.framework.response["http_status_code"] = 202
            self.framework.response["docs"].append("script doc")
        return {}

    def settle(self, **kwargs):
        return room_charge.enforce_room_charge_settlement(
            self.invoice,
            self.profile,
            [{"mode_of_payment": "Room Charge", "amount": 25000}],
            room_number="101",
            **kwargs,
        )

    def assert_request_restored(self):
        self.assertIs(self.framework.form_dict, self.form)
        self.assertIs(self.framework.response, self.response)
        self.assertEqual(self.form, {"cmd": "submit_invoice", "room": "outer room"})
        self.assertEqual(self.response, {"message": {"invoice": "outer invoice"}, "docs": ["outer doc"]})
        self.assertEqual(self.local.message_log, ["outer message"])

    def test_enabled_script_lookup_isolated_from_outer_request(self):
        for error in (AppNotInstalledError, ImportError):
            with self.subTest(error=error.__name__):
                self.framework.get_attr.reset_mock()
                self.framework.get_doc.reset_mock()
                self.framework.get_attr.side_effect = self.raise_app_not_installed if error is AppNotInstalledError else error
                result = self.settle(room_charge_stay="RES-101")
                self.framework.get_attr.assert_called_once_with(METHOD)
                self.framework.get_doc.assert_called_once_with("Server Script", SCRIPT)
                self.assertEqual(self.script_args, {
                    "room": "101", "room_number": "101", "reservation": "RES-101",
                    "pos_profile": "Hotel Till", "amount": 25000, "currency": "UGX",
                })
                self.assert_request_restored()
                self.assertEqual(result, {
                    "reservation": "RES-101", "room_number": "101", "amount": 25000, "currency": "UGX",
                    "override_approver": None, "override_reason": None,
                })
                self.assertEqual(self.invoice.custom_room_charge_stay, "RES-101")
                self.assertTrue(self.invoice.flags.pos_verified_room_charge)

    def test_accepts_bridge_payload_returned_as_a_dict(self):
        self.script.execute_method.side_effect = None
        self.script.execute_method.return_value = self.payload
        self.assertEqual(self.settle()["room_number"], "101")
        self.assertTrue(self.invoice.flags.pos_verified_room_charge)
        self.assert_request_restored()

    def test_no_post_requires_and_consumes_manager_override(self):
        self.payload["no_post"] = 1
        consume = Mock(return_value={"approver": "duty@example.com", "claim": {"reason": "Duty manager approved"}})
        with patch.dict(sys.modules, {"ury.ury_pos.override": SimpleNamespace(consume=consume)}):
            with self.assertRaisesRegex(PermissionError, r"Guest stay is marked No Post\. \[KMR-RC-011\] \[POS-AUTH-REQUIRED\]"):
                self.settle()
            consume.assert_not_called()
            result = self.settle(room_charge_approval="token", room_charge_reason="Duty manager approved")
        consume.assert_called_once_with(
            "token", "room_charge_over_limit", "Hotel Till", "POS-1",
            {"reservation": "", "room": "101", "amount": 25000, "currency": "UGX", "reason": "Duty manager approved"},
        )
        self.assertEqual(result["override_approver"], "duty@example.com")
        self.assertEqual(self.invoice.custom_override_reason, "Duty manager approved")
        self.assertTrue(self.invoice.flags.pos_verified_room_charge_override)
        self.assert_request_restored()

    def test_missing_disabled_or_non_api_script_fails_closed(self):
        self.framework.get_attr.side_effect = ImportError
        for row in (None, dict(self.script_row, disabled=1), dict(self.script_row, script_type="DocType Event")):
            with self.subTest(row=row):
                self.script_row = row
                self.framework.db.get_value.reset_mock()
                with self.assertRaisesRegex(ValidationError, r"\[POS-RC-UNAVAILABLE\]"):
                    self.settle()
                self.framework.db.get_value.assert_any_call(
                    "Server Script", {"script_type": "API", "api_method": METHOD, "disabled": 0}, "name",
                )
                self.framework.get_doc.assert_not_called()
                self.script.execute_method.assert_not_called()
                self.assert_request_restored()

    def test_script_refusals_propagate_and_restore_request(self):
        for error in (ValidationError("Room is unavailable"), PermissionError("Room access denied")):
            with self.subTest(error=type(error).__name__):
                def reject():
                    self.execute_script()
                    raise error

                self.script.execute_method.side_effect = reject
                with self.assertRaises(type(error)) as caught:
                    self.settle()
                self.assertIs(caught.exception, error)
                self.assert_request_restored()

    def test_script_does_not_override_explicit_or_unsuccessful_verification(self):
        for verdict in ({"ok": True, "posting_allowed": False}, {"ok": 0}):
            with self.subTest(verdict=verdict):
                self.payload = {"room_number": "101", "reason_code": "ROOM-REFUSED", **verdict}
                with self.assertRaisesRegex(ValidationError, r"\[ROOM-REFUSED\]"):
                    self.settle()
                self.assert_request_restored()

    def test_python_verifier_keeps_priority_and_its_own_response_contract(self):
        verifier = Mock(return_value=self.payload)
        self.framework.get_attr.side_effect = None
        self.framework.get_attr.return_value = verifier
        with self.assertRaisesRegex(ValidationError, r"\[POS-RC-REFUSED\]"):
            self.settle(room_charge_stay="RES-101")
        verifier.assert_called_once_with(reservation="RES-101", pos_profile="Hotel Till", amount=25000, currency="UGX")
        self.framework.get_doc.assert_not_called()
        self.assert_request_restored()


class TestPOSRoleHooks(unittest.TestCase):
    def test_exports_outlet_and_bypass_roles(self):
        self.assertEqual(hooks.pos_outlet_roles, ["URY Cashier", "URY Manager", "URY Captain", "URY Waiter"])
        self.assertEqual(hooks.pos_bypass_roles, ["URY Manager"])
