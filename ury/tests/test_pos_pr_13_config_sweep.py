"""Regression tests for outlet configuration, staff punches, and guest orders."""

import json
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from ury.ury.api import self_ordering, ury_kot_display, ury_print
from ury.ury.doctype.ury_order import ury_order
from ury.ury.hooks import ury_pos_invoice
from ury.ury_pos import floor_pin, timeclock, tips_declaration

ROOT = Path(__file__).resolve().parents[1]


def reject(message, exc=frappe.ValidationError):
    raise exc(message)


class TestPOSConfigSweep(unittest.TestCase):
    def test_dine_in_invoice_series_overrides_the_restaurant_series(self):
        doc = frappe._dict(pos_profile="Patio POS", restaurant_table="T1",
                           order_type="Dine In", naming_series="BASE-.#####")
        db = Mock()

        def get_value(doctype, name, field, **kwargs):
            if doctype == "POS Profile":
                return frappe._dict(restaurant="Patio")
            return {"dine_in_invoice_series": "PATIO-.#####",
                    "invoice_series_prefix": "BASE-.#####"}[field]

        db.get_value.side_effect = get_value
        with patch.object(ury_pos_invoice, "frappe", SimpleNamespace(db=db)):
            ury_pos_invoice.pos_invoice_naming(doc, None)
            self.assertEqual(doc.naming_series, "PATIO-.#####")
            doc.restaurant_table = None
            doc.order_type = "Take Away"
            ury_pos_invoice.pos_invoice_naming(doc, None)
            self.assertEqual(doc.naming_series, "BASE-.#####")

    def test_qz_configured_roles_replace_legacy_roles_before_key_access(self):
        signer = Mock()
        signer.sign.return_value = b"signed"
        settings = frappe._dict(qz_signing_roles="Print Operator\n")
        runtime = SimpleNamespace(session=frappe._dict(user="printer@example.test"),
                                  get_roles=Mock(return_value=["URY Cashier"]),
                                  get_single=Mock(return_value=settings),
                                  throw=reject, PermissionError=frappe.PermissionError)
        with (patch.object(ury_print, "frappe", runtime),
              patch.object(ury_print, "_get_qz_private_key", return_value=b"pem") as key,
              patch.object(ury_print.serialization, "load_pem_private_key", return_value=signer)):
            with self.assertRaises(frappe.PermissionError):
                ury_print.signature_promise("job")
            key.assert_not_called()
            runtime.get_roles.return_value = ["Print Operator"]
            self.assertEqual(ury_print.signature_promise("job"), "c2lnbmVk")
            settings.qz_signing_roles = ""
            with self.assertRaises(frappe.PermissionError):
                ury_print.signature_promise("job")

    def test_covers_handle_legacy_integer_strings_and_order_type_limits(self):
        profile = frappe._dict(custom_max_covers=80, custom_order_type_cover_limits=[
            frappe._dict(order_type="Dine In", max_covers=65),
            frappe._dict(order_type="Take Away", max_covers=0),
        ])
        with patch.object(ury_order, "frappe", SimpleNamespace(throw=reject)):
            pax = ury_order._validate_dine_in_pax(" 61 ", "Dine In", profile)
            self.assertIs(type(pax), int)
            self.assertEqual(pax, 61)
            self.assertEqual(ury_order._validate_dine_in_pax("101", "Take Away", profile), 101)
            for value in ("66", "2.5", "abc", 0, True):
                with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
                    ury_order._validate_dine_in_pax(value, "Dine In", profile)

    def test_floor_pin_uses_profile_length_lockout_and_session(self):
        profile = frappe._dict(custom_floor_pin_min_length=5, custom_floor_pin_max_length=6,
                               custom_floor_pin_max_attempts=2, custom_floor_pin_lockout_minutes=12,
                               custom_floor_pin_session_minutes=9)
        tracker = Mock()
        tracker.return_value.is_user_allowed.return_value = True
        manager = Mock()
        db = Mock()
        db.get_value.return_value = "staff@example.test"
        fake = SimpleNamespace(db=db, local=SimpleNamespace(login_manager=manager),
                               sessions=Mock(), AuthenticationError=frappe.AuthenticationError,
                               throw=reject, _=str)
        with (patch.object(floor_pin, "frappe", fake),
              patch.object(floor_pin, "_device", return_value=(profile, "device", "tablet")),
              patch.object(floor_pin, "_till_open", return_value=True),
              patch.object(floor_pin, "_eligible", return_value=True),
              patch.object(floor_pin, "_digest", return_value="pin-hash"),
              patch.object(floor_pin, "add_authentication_log"),
              patch.object(floor_pin, "get_fullname", return_value="Staff"),
              patch.object(floor_pin, "LoginAttemptTracker", tracker)):
            before = datetime.now(timezone.utc)
            floor_pin._sign_in.__wrapped__("012345")
            self.assertEqual(floor_pin.get_floor_pad()["max_length"], 6)
        tracker.assert_any_call("ury-floor-short:device", max_consecutive_login_attempts=1,
                                lock_interval=720)
        tracker.assert_any_call("ury-floor-day:device", max_consecutive_login_attempts=19,
                                lock_interval=86400)
        end = datetime.fromisoformat(manager.login_as.call_args.kwargs["session_end"])
        self.assertLess(abs(end - before - timedelta(minutes=9)), timedelta(seconds=5))

    def test_room_type_is_a_link_to_an_editable_master(self):
        root = ROOT / "ury" / "doctype"
        room = json.loads((root / "ury_room" / "ury_room.json").read_text())
        field = next(row for row in room["fields"] if row["fieldname"] == "room_type")
        self.assertEqual((field["fieldtype"], field["options"]), ("Link", "Dining Area Type"))
        master = json.loads((root / "dining_area_type" / "dining_area_type.json").read_text())
        self.assertTrue(any(row.get("create") for row in master["permissions"]))

    def test_kds_station_recall_window_wins_over_the_invoice_profile(self):
        db = Mock()
        db.get_value.side_effect = lambda dt, name, field: {
            ("URY Production Unit", "Kitchen", "pos_profile"): "Kitchen Profile",
            ("POS Profile", "Kitchen Profile", "custom_kds_recall_window_minutes"): 30,
            ("POS Profile", "Register", "custom_kds_recall_window_minutes"): 8,
        }.get((dt, name, field)) if isinstance(name, str) else None
        with patch.object(ury_kot_display, "frappe", SimpleNamespace(db=db)):
            self.assertEqual(ury_kot_display._kot_recall_window_minutes(
                "Outlet", "Register", production="Kitchen"), 30)

    def test_qr_placement_resolves_verified_branch_without_writing_branch_users(self):
        reached = RuntimeError("reached invoice lookup")
        profile = SimpleNamespace(branch="Outlet", enabled=1, allow_add_to_running_table=1)
        session = SimpleNamespace(name="QR Session", ordering_profile="QR Profile", table="T1", invoice=None)
        branch = Mock()
        db = Mock()
        db.exists.return_value = False
        runtime = SimpleNamespace(db=db, get_doc=Mock(side_effect=lambda dt, _:
                                                       branch if dt == "Branch" else profile))
        with (patch.object(self_ordering, "frappe", runtime),
              patch.object(self_ordering, "_resolve_session", return_value=session),
              patch.object(self_ordering, "_elevated", return_value=nullcontext()),
              patch.object(self_ordering, "_limit_guest_session"),
              patch.object(self_ordering, "resolve_restaurant_menu", return_value={"items": [{"item": "Tea"}]}),
              patch.object(self_ordering, "_catalog_gate", return_value=True),
              patch.object(self_ordering, "_resolve_or_create_pos_invoice", side_effect=reached) as lookup):
            with self.assertRaisesRegex(RuntimeError, "reached invoice lookup"):
                self_ordering.add_customer_items("token", [{"item": "Tea", "qty": 1}])
        branch.append.assert_not_called()
        branch.save.assert_not_called()
        self.assertEqual(lookup.call_args.kwargs["override_branch"], "Outlet")

    def test_clock_in_requires_and_persists_outlet_job_code(self):
        profile = frappe._dict(name="Main POS", company="Company", custom_pos_job_codes=[
            frappe._dict(job_code="Server"), frappe._dict(job_code="Bartender"),
        ])
        employee = {"name": "EMP-1"}
        checkin = Mock()
        checkin.name = "PUNCH-1"
        checkin.get.side_effect = lambda key: {"custom_pos_job_code": "Bartender"}.get(key)
        runtime = SimpleNamespace(
            session=frappe._dict(user="employee@example.test"), throw=reject,
            ValidationError=frappe.ValidationError,
            get_doc=Mock(side_effect=lambda obj, *args: profile if isinstance(obj, str) else checkin),
        )
        with (patch.object(timeclock, "frappe", runtime),
              patch.object(timeclock.outlet_context, "assert_outlet_access"),
              patch.object(timeclock, "is_hrms_timeclock_available", return_value=True),
              patch.object(timeclock, "_resolve_active_employee", return_value=employee),
              patch.object(timeclock, "_latest_checkin", return_value=None),
              patch.object(timeclock, "_build_status_payload", return_value={}),
              patch.object(timeclock, "now_datetime", return_value=datetime(2026, 11, 25, 9))):
            with self.assertRaises(frappe.ValidationError):
                timeclock.punch_timeclock("IN", "Main POS")
            with self.assertRaises(frappe.ValidationError):
                timeclock.punch_timeclock("IN", "Main POS", job_code="Manager")
            timeclock.punch_timeclock("IN", "Main POS", job_code="Bartender")
        self.assertEqual(runtime.get_doc.call_args.args[0]["custom_pos_job_code"], "Bartender")

    def test_shift_summary_keeps_one_employee_two_jobs_separate(self):
        start = datetime(2026, 11, 25, 9)
        midway = start + timedelta(hours=1)
        end = midway + timedelta(hours=2)
        punches = [
            {"employee": "EMP", "time": start, "log_type": "IN", "custom_pos_job_code": "Server"},
            {"employee": "EMP", "time": midway, "log_type": "OUT"},
            {"employee": "EMP", "time": midway, "log_type": "IN", "custom_pos_job_code": "Bartender"},
            {"employee": "EMP", "time": end, "log_type": "OUT"},
        ]
        receipts = [
            ("EMP", start + timedelta(minutes=30), 1.125),
            ("EMP", midway + timedelta(minutes=30), 2.125),
        ]
        result = tips_declaration.summarize_labor_and_charged_tips(punches, receipts, start, end, 3)
        self.assertEqual({row["job_code"]: (row["labor_minutes"], row["charged_tips"])
                          for row in result}, {"Server": (60, 1.125), "Bartender": (120, 2.125)})

    def test_tip_precision_for_configured_outlet_currency(self):
        self.assertEqual([tips_declaration.currency_decimal_places(code)
                          for code in ("UGX", "USD", "KWD")], [0, 2, 3])
