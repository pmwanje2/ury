"""RED/GREEN unit tests for G23 Cash Drawer ESC/POS hardware kick and config-driven auto-kick.

Site-less unit tests: run directly with
`PYTHONPATH=. python3 -m unittest ury.ury_pos.test_drawer_kick`.
"""

import importlib
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

APP_ROOT = Path(__file__).resolve().parents[1]


def _build_frappe_stub():
    frappe_mod = MagicMock()
    frappe_mod._ = lambda msg, *args: msg
    frappe_mod.whitelist = lambda *args, **kwargs: (lambda fn: fn)

    class ValidationError(Exception):
        pass

    class PermissionError(Exception):
        pass

    class DoesNotExistError(Exception):
        pass

    frappe_mod.ValidationError = ValidationError
    frappe_mod.PermissionError = PermissionError
    frappe_mod.DoesNotExistError = DoesNotExistError

    def _throw(msg, exc=ValidationError):
        raise exc(msg)

    frappe_mod.throw.side_effect = _throw
    frappe_mod.session = SimpleNamespace(user="cashier@example.com")
    frappe_mod.request = SimpleNamespace(method="POST")
    frappe_mod.flags = SimpleNamespace(in_test=True)
    frappe_mod.get_roles.return_value = ["URY Cashier"]
    frappe_mod.has_permission.return_value = True
    frappe_mod.generate_hash.return_value = "abcd1234"
    frappe_mod.logger.return_value = MagicMock()

    utils_mod = ModuleType("frappe.utils")
    utils_mod.cint = lambda v: int(float(v)) if v not in (None, "", False) else 0
    utils_mod.now_datetime = MagicMock()
    utils_mod.now = MagicMock(return_value="2026-11-24 12:00:00")
    frappe_mod.utils = utils_mod

    printview_mod = ModuleType("frappe.www.printview")
    printview_mod.validate_print_permission = MagicMock()

    custom_field_mod = ModuleType("frappe.custom.doctype.custom_field.custom_field")
    custom_field_mod.create_custom_fields = MagicMock()

    ury_order_mod = ModuleType("ury.ury.doctype.ury_order.ury_order")
    ury_order_mod._free_tables_if_no_open_invoices = MagicMock()
    ury_order_mod.check_split_target_access = MagicMock()

    pos_api_mod = ModuleType("ury.ury_pos.api")
    pos_api_mod.getBranch = MagicMock(return_value="Main Branch")

    monitor_mod = ModuleType("ury.ury.printing.print_job_monitor")
    monitor_mod.register_print_job = MagicMock(return_value=True)

    modules = {
        "frappe": frappe_mod,
        "frappe.utils": utils_mod,
        "frappe.www": ModuleType("frappe.www"),
        "frappe.www.printview": printview_mod,
        "frappe.custom": ModuleType("frappe.custom"),
        "frappe.custom.doctype": ModuleType("frappe.custom.doctype"),
        "frappe.custom.doctype.custom_field": ModuleType("frappe.custom.doctype.custom_field"),
        "frappe.custom.doctype.custom_field.custom_field": custom_field_mod,
        "ury.ury.doctype.ury_order.ury_order": ury_order_mod,
        "ury.ury_pos.api": pos_api_mod,
        "ury.ury.printing.print_job_monitor": monitor_mod,
    }
    return frappe_mod, modules


class TestEscPosDrawerKickService(unittest.TestCase):
    def setUp(self):
        self.frappe, self.modules = _build_frappe_stub()
        self._patcher = patch.dict(sys.modules, self.modules)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        for mod_name in (
            "ury.ury.printing.service",
            "ury.ury.api.ury_print",
            "ury.ury_pos.kds_device_fields",
        ):
            sys.modules.pop(mod_name, None)

    def test_build_drawer_kick_bytes_supports_pin_2_pin_5_and_custom_hex(self):
        service = importlib.import_module("ury.ury.printing.service")
        build = getattr(service, "build_drawer_kick_bytes", None)
        self.assertTrue(callable(build), "service.build_drawer_kick_bytes is missing")

        pin2_bytes, pin2_hex = build(drawer_pin=2)
        self.assertEqual(pin2_bytes, b"\x1b\x70\x00\x19\xfa")
        self.assertEqual(pin2_hex, "1b700019fa")

        pin5_bytes, pin5_hex = build(drawer_pin=5)
        self.assertEqual(pin5_bytes, b"\x1b\x70\x01\x19\xfa")
        self.assertEqual(pin5_hex, "1b700119fa")

        custom_bytes, custom_hex = build(hex_command="1B 70 01 19 FA")
        self.assertEqual(custom_bytes, b"\x1b\x70\x01\x19\xfa")
        self.assertEqual(custom_hex, "1b700119fa")

        for bad_pin in (0, 1, 3, 9, -2):
            with self.assertRaises(ValueError):
                build(drawer_pin=bad_pin)

        for bad_hex in ("zzzz", "1b7", "00" * 40):
            with self.assertRaises(ValueError):
                build(hex_command=bad_hex)

    def test_send_raw_escpos_sends_bytes_over_tcp_and_handles_absent_or_offline_printer(self):
        service = importlib.import_module("ury.ury.printing.service")
        send_raw = getattr(service, "send_raw_escpos", None)
        self.assertTrue(callable(send_raw), "service.send_raw_escpos is missing")

        # No printer attached -> structured Not Configured, no exception.
        unconfigured = send_raw(b"\x1b\x70\x00\x19\xfa")
        self.assertEqual(unconfigured["status"], "Not Configured")
        self.assertEqual(unconfigured["transport"], "none")

        # Direct TCP/9100 delivery sends exact bytes.
        fake_sock = MagicMock()
        fake_sock.__enter__.return_value = fake_sock
        with patch("socket.create_connection", return_value=fake_sock) as conn_mock:
            sent = send_raw(b"\x1b\x70\x01\x19\xfa", host="192.0.2.50", port=9100)
        conn_mock.assert_called_once_with(("192.0.2.50", 9100), timeout=3.0)
        fake_sock.sendall.assert_called_once_with(b"\x1b\x70\x01\x19\xfa")
        self.assertEqual(sent["status"], "Sent")
        self.assertEqual(sent["transport"], "tcp")
        self.assertEqual(sent["bytes_sent"], 5)

        # Offline printer -> structured Failed, never crashes.
        with patch("socket.create_connection", side_effect=OSError("Connection refused")):
            failed = send_raw(b"\x1b\x70\x00\x19\xfa", host="192.0.2.50", port=9100)
        self.assertEqual(failed["status"], "Failed")
        self.assertEqual(failed["transport"], "tcp")
        self.assertIn("Connection refused", failed["message"])

    def test_kick_cash_drawer_endpoint_enforces_auth_and_generates_pin_pulse(self):
        ury_print = importlib.import_module("ury.ury.api.ury_print")
        kick = getattr(ury_print, "kick_cash_drawer", None)
        self.assertTrue(callable(kick), "ury_print.kick_cash_drawer is missing")

        profile_doc = SimpleNamespace(
            name="Main Till",
            branch="Main Branch",
            applicable_for_users=[SimpleNamespace(user="cashier@example.com")],
            custom_drawer_pin="5",
            custom_drawer_kick_hex="",
            custom_auto_kick_on_cash_tender=1,
            get=lambda k, default=None: {
                "branch": "Main Branch",
                "custom_drawer_pin": "5",
                "custom_drawer_kick_hex": "",
                "custom_auto_kick_on_cash_tender": 1,
                "applicable_for_users": [SimpleNamespace(user="cashier@example.com")],
            }.get(k, default),
        )
        self.frappe.get_doc.side_effect = lambda dt, name=None: profile_doc if dt == "POS Profile" else MagicMock()
        self.frappe.get_all.return_value = []  # no network printer configured

        # Guest must be rejected.
        self.frappe.session.user = "Guest"
        with self.assertRaises(self.frappe.PermissionError):
            kick("Main Till")

        # Cross-branch user must be rejected.
        self.frappe.session.user = "other@example.com"
        self.modules["ury.ury_pos.api"].getBranch.return_value = "Other Branch"
        with self.assertRaises(self.frappe.PermissionError):
            kick("Main Till")

        # Authorized branch cashier gets graceful Not Configured + exact Pin 5 pulse bytes.
        self.frappe.session.user = "cashier@example.com"
        self.modules["ury.ury_pos.api"].getBranch.return_value = "Main Branch"
        result = kick("Main Till", reason="No Sale")
        self.assertEqual(result["status"], "Not Configured")
        self.assertEqual(result["drawer_pin"], 5)
        self.assertEqual(result["hex"], "1b700119fa")
        self.assertEqual(result["bytes_length"], 5)

    def test_mode_of_payment_opens_drawer_is_strictly_configuration_driven(self):
        ury_print = importlib.import_module("ury.ury.api.ury_print")
        opens_drawer = getattr(ury_print, "mode_of_payment_opens_drawer", None)
        self.assertTrue(callable(opens_drawer), "ury_print.mode_of_payment_opens_drawer is missing")

        config_map = {
            "Cash": 0,  # Explicitly disabled: must NOT kick just because it's named "Cash"!
            "Petty Register Notes": 1,  # Non-standard tender name enabled by config: MUST kick!
            "Room Charge": 0,
            "Mobile Money": 0,
        }
        self.frappe.db.get_value.side_effect = lambda dt, name, field: (
            config_map.get(name, 0) if (dt, field) == ("Mode of Payment", "custom_open_cash_drawer") else None
        )

        self.assertFalse(opens_drawer("Cash"))
        self.assertTrue(opens_drawer("Petty Register Notes"))
        self.assertFalse(opens_drawer("Room Charge"))
        self.assertFalse(opens_drawer("Mobile Money"))

    def test_kds_device_custom_fields_and_patch_are_declared(self):
        fields_mod = importlib.import_module("ury.ury_pos.kds_device_fields")
        fields = fields_mod.get_kds_device_custom_fields()

        mop_fields = {f["fieldname"]: f for f in fields.get("Mode of Payment", [])}
        self.assertIn("custom_open_cash_drawer", mop_fields)
        self.assertEqual(mop_fields["custom_open_cash_drawer"]["fieldtype"], "Check")
        self.assertEqual(str(mop_fields["custom_open_cash_drawer"]["default"]), "0")

        profile_fields = {f["fieldname"]: f for f in fields.get("POS Profile", [])}
        self.assertIn("custom_kds_recall_window_minutes", profile_fields)
        self.assertEqual(str(profile_fields["custom_kds_recall_window_minutes"]["default"]), "15")
        self.assertIn("custom_drawer_pin", profile_fields)
        self.assertIn("custom_drawer_kick_hex", profile_fields)
        self.assertIn("custom_auto_kick_on_cash_tender", profile_fields)

        inv_fields = {f["fieldname"]: f for f in fields.get("POS Invoice", [])}
        self.assertIn("custom_is_rush", inv_fields)
        self.assertIn("custom_is_vip", inv_fields)

        kot_fields = {f["fieldname"]: f for f in fields.get("URY KOT", [])}
        self.assertIn("custom_is_rush", kot_fields)
        self.assertIn("custom_is_vip", kot_fields)

        printer_fields = {f["fieldname"]: f for f in fields.get("Network Printer Settings", [])}
        self.assertIn("custom_backup_printer", printer_fields)

        patches_txt = (APP_ROOT / "patches.txt").read_text(encoding="utf-8")
        self.assertIn("ury.patches.v2_0.setup_kds_expo_routing_drawer", patches_txt)
        self.assertTrue((APP_ROOT / "patches/v2_0/setup_kds_expo_routing_drawer.py").exists())


if __name__ == "__main__":
    unittest.main()
