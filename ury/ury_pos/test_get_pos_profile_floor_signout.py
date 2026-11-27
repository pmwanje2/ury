"""Site-less getPosProfile contracts: opt-in floor-tablet sign-out settings (theme T4, fork #29 U1, #32 U6)
and the payment-mode account map (fork #72, 2c925938 and ac63706d)."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import frappe
from ury import hooks
from ury.ury_pos import api

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/custom_field.json"


def _fields():
    return {field["name"]: field for field in json.loads(FIXTURE.read_text())}


def _custom_field_names():
    fixture = next(row for row in hooks.fixtures if row.get("doctype", row.get("dt")) == "Custom Field")
    return next(row[2] for row in fixture["filters"] if row[:2] == ["name", "in"])


class Row(frappe._dict):
    def as_dict(self):
        return dict(self)


class TestFloorSignoutProfile(unittest.TestCase):
    def setUp(self):
        self.framework = self.enterContext(patch.object(api, "frappe"))
        self.enterContext(patch.object(api, "getBranch", return_value="Branch A"))
        self.framework.session.user = "waiter@example.com"
        self.framework.db.exists.return_value = "Floor POS"
        self.framework.get_single.return_value = frappe._dict(disable_rounded_total=0)
        self.profile = frappe._dict(
            name="Floor POS", branch="Branch A", warehouse="Stores", company="Company A",
            table_attention_time=10, print_format="Receipt", paid_limit=0,
            custom_enable_discount=0, custom_enable_multiple_cashier=0,
            custom_edit_order_type=0, custom_enable_kot_reprint=0, qz_print=0,
            applicable_for_users=[frappe._dict(user="cashier@example.com")],
            printer_settings=[],
            role_allowed_for_billing=[], role_restricted_for_table_order=[],
            transfer_role_permissions=[],
        )
        self.framework.get_doc.return_value = self.profile

    def test_profile_without_payment_rows_returns_empty_account_map(self):
        # A site-less double (and a profile read without child rows) has no
        # payments list; the till's mode-to-account map must then be empty
        # instead of raising.
        result = api.getPosProfile()
        self.assertEqual(result.get("payment_mode_accounts"), {})

    def test_payment_mode_accounts_use_the_profile_company(self):
        self.profile.payments = [frappe._dict(mode_of_payment="Cash"), frappe._dict(mode_of_payment="Card")]
        accounts = {"Cash": "Cash - TC", "Card": "Card Clearing - TC"}
        self.framework.db.get_value.side_effect = lambda doctype, filters, field: (
            accounts[filters["parent"]]
            if doctype == "Mode of Payment Account" and filters.get("company") == self.profile.company
            and field == "default_account" else None
        )
        self.assertEqual(api.getPosProfile().get("payment_mode_accounts"), accounts)

    def test_returns_configured_floor_settings(self):
        self.profile.custom_floor_idle_signout_seconds = 60
        self.profile.custom_floor_signout_after_send = 1
        result = api.getPosProfile()
        self.assertEqual(result.get("floor_idle_signout_seconds"), 60)
        self.assertEqual(result.get("floor_signout_after_send"), 1)

    def test_unset_floor_settings_default_to_disabled(self):
        result = api.getPosProfile()
        self.assertIn("floor_idle_signout_seconds", result)
        self.assertIn("floor_signout_after_send", result)
        self.assertEqual(result["floor_idle_signout_seconds"], 0)
        self.assertEqual(result["floor_signout_after_send"], 0)

    def test_explicitly_disabled_floor_settings_stay_disabled(self):
        self.profile.custom_floor_idle_signout_seconds = 0
        self.profile.custom_floor_signout_after_send = 0
        result = api.getPosProfile()
        self.assertEqual(result.get("floor_idle_signout_seconds"), 0)
        self.assertEqual(result.get("floor_signout_after_send"), 0)

    def test_full_profile_carries_stay_signed_in_roles_for_the_pos_client(self):
        # v16 /pos reads POS Profile through this whitelisted subset, not a raw doc read.
        self.profile.custom_floor_stay_signed_in_roles = [Row(role="URY Manager")]
        result = api.getPosProfileFull("Floor POS")
        self.assertEqual(result.get("custom_floor_stay_signed_in_roles"), [{"role": "URY Manager"}])

    def test_assigned_billing_user_follows_profile_assignment_and_billing_roles(self):
        self.framework.session.user = "cashier@example.com"
        self.framework.get_roles.return_value = ["Banquet Operator"]
        for user, roles, admitted in (
            ("cashier@example.com", [], True),
            ("cashier@example.com", ["Banquet Operator"], True),
            ("cashier@example.com", ["Other Role"], False),
            ("other@example.com", [], False),
        ):
            with self.subTest(user=user, roles=roles):
                self.profile.applicable_for_users = [Row(user=user)]
                self.profile.role_allowed_for_billing = [Row(role=role) for role in roles]
                result = api.getPosProfileFull("Floor POS")
                self.assertEqual(result.get("assigned_billing_user"), self.framework.session.user if admitted else None)

    def test_full_profile_exposes_hold_fire_switch_to_staff(self):
        for enabled in (0, 1):
            with self.subTest(enabled=enabled):
                self.profile.ury_enable_hold_fire = enabled
                self.assertEqual(api.getPosProfileFull("Floor POS").get("ury_enable_hold_fire"), enabled)

    def test_full_profile_stay_roles_default_to_empty(self):
        result = api.getPosProfileFull("Floor POS")
        self.assertIn("custom_floor_stay_signed_in_roles", result)
        self.assertEqual(result["custom_floor_stay_signed_in_roles"], [])


class TestFloorSignoutFixtures(unittest.TestCase):
    def test_fixture_declares_two_opt_in_profile_fields(self):
        fields = _fields()
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
        names = _custom_field_names()
        self.assertIn("POS Profile-custom_floor_idle_signout_seconds", names)
        self.assertIn("POS Profile-custom_floor_signout_after_send", names)

    def test_fixture_declares_stay_signed_in_roles_multiselect(self):
        field = _fields().get("POS Profile-custom_floor_stay_signed_in_roles", {})
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
        self.assertIn("POS Profile-custom_floor_stay_signed_in_roles", _custom_field_names())

    def test_signout_descriptions_name_stay_roles_not_billing(self):
        fields = _fields()
        for name in (
            "POS Profile-custom_floor_idle_signout_seconds",
            "POS Profile-custom_floor_signout_after_send",
        ):
            with self.subTest(field=name):
                description = fields.get(name, {}).get("description") or ""
                self.assertIn("users without a Stay Signed-in Role", description)
                self.assertNotIn("non-billing", description)
                self.assertNotIn("Billing users", description)


if __name__ == "__main__":
    unittest.main()
