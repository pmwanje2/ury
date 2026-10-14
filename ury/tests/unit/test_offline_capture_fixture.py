"""The native fixture import must admit an opt-in, default-off profile flag."""
import json
import importlib
import unittest
from pathlib import Path


class OfflineCaptureFixtureTest(unittest.TestCase):
    def test_profile_flag_is_exported_and_admitted_by_fixture_filter(self):
        root = Path(__file__).resolve().parents[2]
        fields = json.loads((root / "fixtures/custom_field.json").read_text())
        flag = next((field for field in fields if field["name"] == "POS Profile-ury_offline_capture"), None)
        self.assertIsNotNone(flag, "POS Profile offline capture opt-in is not shipped")
        self.assertEqual((flag["dt"], flag["fieldname"], flag["fieldtype"], flag["default"]),
                         ("POS Profile", "ury_offline_capture", "Check", "0"))
        fixtures = importlib.import_module("ury.hooks").fixtures
        filters = next(fixture["filters"] for fixture in fixtures if fixture["doctype"] == "Custom Field")
        self.assertTrue(any("POS Profile-ury_offline_capture" in rule[2] for rule in filters))


if __name__ == "__main__":
    unittest.main()
