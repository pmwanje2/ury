"""The native fixture import must admit the offline capture fields (default off)."""
import importlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# name -> (dt, fieldname, fieldtype, default, unique, no_copy)
OFFLINE_FIELDS = {
    "POS Profile-ury_offline_capture": ("POS Profile", "ury_offline_capture", "Check", "0", 0, 0),
    "POS Invoice-ury_client_order_id": ("POS Invoice", "ury_client_order_id", "Data", None, 1, 1),
    "POS Invoice-ury_last_round_id": ("POS Invoice", "ury_last_round_id", "Data", None, 0, 1),
    "POS Invoice-ury_offline_captured_at": ("POS Invoice", "ury_offline_captured_at", "Datetime", None, 0, 1),
    # Every applied round id with its payload digest, so any earlier round's retry replays.
    "POS Invoice-ury_applied_rounds": ("POS Invoice", "ury_applied_rounds", "Long Text", None, 0, 1),
    "URY KOT-ury_offline_printed": ("URY KOT", "ury_offline_printed", "Check", "0", 0, 1),
}


def _custom_field_filter_names():
    fixtures = importlib.import_module("ury.hooks").fixtures
    filters = next(fixture["filters"] for fixture in fixtures if fixture["doctype"] == "Custom Field")
    return {name for rule in filters for name in rule[2]}


class OfflineCaptureFixtureTest(unittest.TestCase):
    def test_profile_flag_is_exported_and_admitted_by_fixture_filter(self):
        fields = json.loads((ROOT / "fixtures/custom_field.json").read_text())
        flag = next((field for field in fields if field["name"] == "POS Profile-ury_offline_capture"), None)
        self.assertIsNotNone(flag, "POS Profile offline capture opt-in is not shipped")
        self.assertEqual((flag["dt"], flag["fieldname"], flag["fieldtype"], flag["default"]),
                         ("POS Profile", "ury_offline_capture", "Check", "0"))
        self.assertIn("POS Profile-ury_offline_capture", _custom_field_filter_names())

    def test_every_offline_field_has_one_fixture_row_and_a_filter_entry(self):
        fields = json.loads((ROOT / "fixtures/custom_field.json").read_text())
        admitted = _custom_field_filter_names()
        for name, expected in OFFLINE_FIELDS.items():
            with self.subTest(field=name):
                rows = [field for field in fields if field["name"] == name]
                self.assertEqual(len(rows), 1, f"{name} must ship exactly one fixture row")
                row = rows[0]
                self.assertEqual(
                    (row["dt"], row["fieldname"], row["fieldtype"], row["default"], row["unique"], row["no_copy"]),
                    expected,
                )
                self.assertIn(name, admitted, f"{name} is not admitted by the hooks.py fixture filter")


if __name__ == "__main__":
    unittest.main()
