"""Site-less contracts for opt-in floor-tablet sign-out settings."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import frappe
from ury import hooks
from ury.ury_pos import api


class TestFloorSignoutProfile(unittest.TestCase):
    def setUp(self):
        self.framework = self.enterContext(patch.object(api, "frappe"))
        self.enterContext(patch.object(api, "getBranch", return_value="Salama"))
        self.framework.session.user = "waiter@example.com"
        self.framework.db.exists.return_value = "Salama POS"
        self.framework.get_single.return_value = frappe._dict(disable_rounded_total=0)
        self.profile = frappe._dict(
            name="Salama POS", branch="Salama", warehouse="Stores", company="Salama",
            table_attention_time=10, print_format="Receipt", paid_limit=0,
            custom_enable_discount=0, custom_enable_multiple_cashier=0,
            custom_edit_order_type=0, custom_enable_kot_reprint=0, qz_print=0,
            applicable_for_users=[frappe._dict(user="cashier@example.com")],
            printer_settings=[],
        )
        self.framework.get_doc.return_value = self.profile

    def test_returns_configured_floor_settings(self):
        self.profile.custom_floor_idle_signout_seconds = 60
        self.profile.custom_floor_signout_after_send = 1
        result = api.getPosProfile()
        self.assertEqual(result.get("floor_idle_signout_seconds"), 60)
        self.assertEqual(result.get("floor_signout_after_send"), 1)

    def test_unset_floor_settings_default_to_disabled(self):
        result = api.getPosProfile()
        self.assertEqual(result.get("floor_idle_signout_seconds"), 0)
        self.assertEqual(result.get("floor_signout_after_send"), 0)

    def test_explicitly_disabled_floor_settings_stay_disabled(self):
        self.profile.custom_floor_idle_signout_seconds = 0
        self.profile.custom_floor_signout_after_send = 0
        result = api.getPosProfile()
        self.assertEqual(result.get("floor_idle_signout_seconds"), 0)
        self.assertEqual(result.get("floor_signout_after_send"), 0)

    def test_profile_without_payment_rows_returns_empty_account_map(self):
        # A site-less double (and a profile read without child rows) has no
        # payments list; the till's mode-to-account map must then be empty
        # instead of raising.
        result = api.getPosProfile()
        self.assertEqual(result.get("payment_mode_accounts"), {})

    def test_fixture_declares_two_opt_in_profile_fields(self):
        path = Path(__file__).resolve().parents[1] / "fixtures/custom_field.json"
        fields = {field["name"]: field for field in json.loads(path.read_text())}
        for name, fieldtype in (
            ("custom_floor_idle_signout_seconds", "Int"),
            ("custom_floor_signout_after_send", "Check"),
        ):
            with self.subTest(field=name):
                field = fields.get(f"POS Profile-{name}", {})
                self.assertEqual(field.get("dt"), "POS Profile")
                self.assertEqual(field.get("fieldname"), name)
                self.assertEqual(field.get("fieldtype"), fieldtype)
                self.assertEqual(field.get("default"), "0")

    def test_fixture_export_includes_both_fields(self):
        fixture = next(row for row in hooks.fixtures if row["doctype"] == "Custom Field")
        names = next(row[2] for row in fixture["filters"] if row[:2] == ["name", "in"])
        self.assertIn("POS Profile-custom_floor_idle_signout_seconds", names)
        self.assertIn("POS Profile-custom_floor_signout_after_send", names)

    def test_fixture_declares_stay_signed_in_roles_multiselect(self):
        path = Path(__file__).resolve().parents[1] / "fixtures/custom_field.json"
        fields = {field["name"]: field for field in json.loads(path.read_text())}
        field = fields.get("POS Profile-custom_floor_stay_signed_in_roles", {})
        self.assertEqual(field.get("dt"), "POS Profile")
        self.assertEqual(field.get("fieldname"), "custom_floor_stay_signed_in_roles")
        self.assertEqual(field.get("fieldtype"), "Table MultiSelect")
        self.assertEqual(field.get("options"), "Role Permitted")
        self.assertEqual(field.get("permlevel"), 0)
        self.assertEqual(field.get("label"), "Stay Signed-in Roles")
        self.assertEqual(field.get("insert_after"), "custom_floor_signout_after_send")
        self.assertEqual(
            field.get("description"),
            "Staff with one of these roles stay signed in. Everyone else on the floor screen is signed out after the idle time or after each send.",
        )

    def test_hooks_ship_stay_signed_in_roles(self):
        fixture = next(row for row in hooks.fixtures if row["doctype"] == "Custom Field")
        names = next(row[2] for row in fixture["filters"] if row[:2] == ["name", "in"])
        self.assertIn("POS Profile-custom_floor_stay_signed_in_roles", names)

    def test_signout_descriptions_name_stay_roles_not_billing(self):
        path = Path(__file__).resolve().parents[1] / "fixtures/custom_field.json"
        fields = {field["name"]: field for field in json.loads(path.read_text())}
        for name in (
            "POS Profile-custom_floor_idle_signout_seconds",
            "POS Profile-custom_floor_signout_after_send",
        ):
            description = fields[name].get("description", "")
            with self.subTest(field=name):
                self.assertIn("users without a Stay Signed-in Role", description)
                self.assertNotIn("non-billing", description)
                self.assertNotIn("Billing users", description)
