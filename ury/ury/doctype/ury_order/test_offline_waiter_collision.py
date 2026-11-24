"""RED/GREEN site-less unit tests for POS-PR-6 Slice 3 (G46):
Waiter offline capture, shift authorization for floor roles,
peer relay URL validation, and deterministic table collision reconciliation
(Auto Split Check vs Merge Rounds).
"""

from __future__ import annotations

import datetime
import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[4]
CUSTOM_FIELDS_JSON = ROOT / "ury" / "fixtures" / "custom_field.json"
HOOKS_PY = ROOT / "ury" / "hooks.py"
SETUP_CUSTOMIZATIONS_PY = ROOT / "ury" / "setup_customizations.py"
URY_ORDER_PY = ROOT / "ury" / "ury" / "doctype" / "ury_order" / "ury_order.py"


class _FakeValidationError(Exception):
    pass


class _FakePermissionError(Exception):
    pass


class _Dict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __setattr__(self, key, value):
        self[key] = value


def _ensure_frappe_stub():
    if "frappe" in sys.modules:
        return sys.modules["frappe"], False
    frappe_mod = types.ModuleType("frappe")
    frappe_mod.ValidationError = _FakeValidationError
    frappe_mod.PermissionError = _FakePermissionError
    frappe_mod._ = lambda msg: msg
    frappe_mod._dict = lambda d=None, **kw: _Dict({**(d or {}), **kw})
    frappe_mod.whitelist = lambda *a, **kw: (a[0] if a and callable(a[0]) else (lambda fn: fn))
    frappe_mod.flags = _Dict()
    frappe_mod.session = _Dict(user="waiter@example.com")
    frappe_mod.db = MagicMock()
    frappe_mod.get_all = MagicMock(return_value=[])
    frappe_mod.get_cached_doc = MagicMock()
    frappe_mod.get_doc = MagicMock()
    frappe_mod.new_doc = MagicMock()
    frappe_mod.publish_realtime = MagicMock()

    def _throw(msg, exc=None, **kwargs):
        raise (exc or _FakeValidationError)(msg)

    frappe_mod.throw = _throw

    utils_mod = types.ModuleType("frappe.utils")
    utils_mod.cint = lambda v: int(float(v or 0))
    utils_mod.flt = lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0)
    utils_mod.getdate = lambda v=None: datetime.date.fromisoformat(str(v)[:10]) if v else datetime.date(2026, 11, 24)
    utils_mod.add_days = lambda d, n: d + datetime.timedelta(days=int(n))
    utils_mod.today = lambda: "2026-11-24"
    utils_mod.get_datetime = lambda v: datetime.datetime.fromisoformat(str(v)) if isinstance(v, str) else v
    utils_mod.now_datetime = lambda: datetime.datetime(2026, 11, 24, 14, 0, 0)
    utils_mod.get_system_timezone = lambda: "UTC"
    frappe_mod.utils = utils_mod

    sys.modules["frappe"] = frappe_mod
    sys.modules["frappe.utils"] = utils_mod
    return frappe_mod, True


class TestOfflineWaiterCollisionSchema(unittest.TestCase):
    EXPECTED_FIELDS = (
        "POS Profile-custom_offline_peer_relay_url",
        "POS Profile-custom_offline_collision_policy",
        "POS Invoice-custom_offline_conflict",
    )

    def test_collision_and_relay_custom_fields_registered(self):
        rows = json.loads(CUSTOM_FIELDS_JSON.read_text(encoding="utf-8"))
        by_name = {row.get("name"): row for row in rows}
        hooks_text = HOOKS_PY.read_text(encoding="utf-8")
        setup_text = SETUP_CUSTOMIZATIONS_PY.read_text(encoding="utf-8")

        for name in self.EXPECTED_FIELDS:
            fieldname = name.split("-", 1)[1]
            with self.subTest(field=name):
                self.assertIn(name, by_name, f"Missing {name} in custom_field.json")
                self.assertIn(name, hooks_text, f"Missing {name} in hooks.py fixtures")
                self.assertIn(fieldname, setup_text, f"Missing {fieldname} in setup_customizations.py")

        policy = by_name["POS Profile-custom_offline_collision_policy"]
        self.assertEqual(policy.get("fieldtype"), "Select")
        self.assertEqual(policy.get("default"), "Auto Split Check")
        opts = (policy.get("options") or "").splitlines()
        self.assertIn("Auto Split Check", opts)
        self.assertIn("Merge Rounds", opts)


class TestOfflineCollisionAndRelayHelpers(unittest.TestCase):
    def setUp(self):
        self.frappe, self._stubbed = _ensure_frappe_stub()
        self.frappe.db.reset_mock()
        self.frappe.publish_realtime.reset_mock()

    def tearDown(self):
        if self._stubbed:
            for mod_name in ("frappe", "frappe.utils", "ury.ury_pos.fx"):
                sys.modules.pop(mod_name, None)

    def _fx(self):
        return importlib.import_module("ury.ury_pos.fx")

    def test_peer_relay_url_validation(self):
        fx = self._fx()
        fx.validate_offline_peer_relay_url("")
        fx.validate_offline_peer_relay_url("https://lan-relay.local:8443/pos-sync")
        fx.validate_offline_peer_relay_url("http://192.168.1.20:8080/relay")
        for bad in ("javascript:alert(1)", "ftp://192.168.1.20/relay", "not-a-url"):
            with self.subTest(bad=bad), self.assertRaises(self.frappe.ValidationError):
                fx.validate_offline_peer_relay_url(bad)

    def test_auto_split_check_collision_plan_labels_split_and_sets_conflict_flag(self):
        fx = self._fx()
        existing = _Dict(
            name="POS-INV-0042",
            restaurant_table="T-05",
            custom_split_group="GRP-001",
            ury_client_order_id="aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee",
            docstatus=0,
        )
        plan = fx.build_offline_collision_plan(
            existing_invoice=existing,
            table="T-05",
            client_order_id="f9c2dead-beef-4444-8888-111122223333",
            round_id="round-1",
            policy="Auto Split Check",
        )
        self.assertEqual(plan["action"], "auto_split_check")
        self.assertEqual(plan["custom_split_from"], "POS-INV-0042")
        self.assertEqual(plan["custom_split_group"], "GRP-001")
        self.assertEqual(plan["custom_offline_conflict"], 1)
        self.assertIn("T-05", plan["custom_guest_label"])
        self.assertIn("f9c2", plan["custom_guest_label"])
        self.assertLessEqual(len(plan["custom_guest_label"]), 40)

    def test_merge_rounds_collision_plan_generates_scoped_round_key(self):
        fx = self._fx()
        existing = _Dict(
            name="POS-INV-0042",
            restaurant_table="T-05",
            ury_client_order_id="aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee",
            docstatus=0,
        )
        plan = fx.build_offline_collision_plan(
            existing_invoice=existing,
            table="T-05",
            client_order_id="f9c2dead-beef-4444-8888-111122223333",
            round_id="round-1",
            policy="Merge Rounds",
        )
        self.assertEqual(plan["action"], "merge_rounds")
        self.assertEqual(plan["collision_round_id"], "f9c2dead-beef-4444-8888-111122223333:round-1")
        self.assertEqual(plan["custom_offline_conflict"], 1)

    def test_ury_order_supports_waiter_shift_lookup_and_collision_policy(self):
        text = URY_ORDER_PY.read_text(encoding="utf-8")
        self.assertIn("custom_offline_collision_policy", text)
        self.assertIn("custom_offline_conflict", text)
        self.assertIn("build_offline_collision_plan", text)
        # Waiter offline shift check parameter on _offline_capture_time
        self.assertRegex(text, r"def _offline_capture_time\([^)]*billing_user")


if __name__ == "__main__":
    unittest.main()
