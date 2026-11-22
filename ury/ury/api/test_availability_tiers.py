"""Site-less coverage of the real availability and guest-ordering boundaries."""

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from ury.ury.api.test_guest_session_reads import _site_less_frappe


MODULE = "ury.ury.api.ury_availability"
BRANCH, COMPANY = "B1", "C1"


class TestAvailabilityTiers(unittest.TestCase):
    def setUp(self):
        self.frappe, self.modules = _site_less_frappe()
        self.frappe.session.user = "Administrator"
        self.frappe.request = None
        self.frappe.set_user.side_effect = lambda user: setattr(self.frappe.session, "user", user)
        self.frappe.local = type("Local", (), {})()
        self.items = {"Fish": 0, "Disabled": 1}
        self.policy = None
        self.frappe.db.get_value.side_effect = self.get_value
        self.frappe.get_all.side_effect = lambda dt, **kw: [
            {"name": code, "disabled": self.items[code]}
            for code in kw["filters"]["name"][1] if code in self.items
        ] if dt == "Item" else self.fail(f"Unexpected batch read: {dt}")
        self.modules["frappe.utils"].getdate = lambda: None
        del self.modules["ury.ury.api.ury_stock_policy"]
        self.modules["ury.ury.api.ury_stock_policy"] = self.load("ury_stock_policy", self.modules)
        resolver = Mock(return_value=None)
        self.modules.update({
            "ury.ury.api.ury_bom_compiler": Mock(),
            "ury.ury.api.ury_production_context": Mock(resolve_production_context=resolver),
            "ury.ury.api.ury_inventory_projection": Mock(),
            "ury.ury.api.ury_projection_service": Mock(),
            "ury.ury.api.ury_sales_plan_commit": Mock(),
            "ury.ury.api.ury_reservation_service": Mock(),
        })
        self.api = self.modules[MODULE] = self.load("ury_availability", self.modules)
        self.resolver = resolver
        self.guest = self.load("self_ordering", self.modules)
        self.menu = {"name": "Lunch", "items": [{"item": "Fish"}, {"item": "Disabled"}]}
        self.guest.resolve_restaurant_menu.return_value = self.menu
        self.frappe.get_doc.return_value = type("Profile", (), {
            "restaurant": "R1", "branch": BRANCH, "enabled": 1, "allow_add_to_running_table": 1,
        })()
        session = type("Session", (), {
            "name": "SESSION", "ordering_profile": "PROFILE", "table": "T1", "invoice": None,
        })()
        self.session = self.enterContext(patch.object(self.guest, "_resolve_session", return_value=session))

    @staticmethod
    def load(module, modules):
        spec = importlib.util.spec_from_file_location(f"_tiers_{module}", Path(__file__).with_name(f"{module}.py"))
        loaded = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(loaded)
        return loaded

    def get_value(self, doctype, name, field, **kwargs):
        if doctype == "Item":
            return self.items.get(name) if field == "disabled" else name
        if doctype == "URY Branch Stock Policy":
            return self.policy
        if doctype == "Branch":
            return COMPANY
        if doctype == "URY Self Ordering Profile":
            return BRANCH
        self.fail(f"Unexpected value read: {doctype}")

    def enable_reservations(self):
        self.policy = {
            "reservation_control_enabled": 1,
            "realtime_production_posting_enabled": 0,
            "closing_reconciliation_enabled": 0,
        }

    def assert_disabled(self, result):
        self.assertEqual(result["reason_code"], "ITEM_DISABLED")
        self.assertIs(result["sellable"], False)
        self.assertEqual(result["available_qty"], 0)

    def test_tier1_item_without_production_config_is_sellable_unconstrained(self):
        result = self.api.get_item_availability("Fish", BRANCH, COMPANY)
        self.assertEqual(result["reason_code"], "AVAILABLE")
        self.assertIs(result["sellable"], True)
        self.assertIsNone(result["available_qty"])
        self.assertEqual((result["stock_policy_mode"], result["source"]), ("NATIVE", "CATALOG"))

    def test_tier1_never_calls_production_resolver(self):
        self.resolver.side_effect = AssertionError("Tier 1 reached the production resolver")
        self.assertTrue(self.api.get_item_availability("Fish", BRANCH, COMPANY)["sellable"])

    def test_tier1_disabled_item_is_unsellable(self):
        self.assert_disabled(self.api.get_item_availability("Disabled", BRANCH, COMPANY))

    def test_tier2_disabled_item_blocked_before_engine(self):
        self.enable_reservations()
        self.resolver.side_effect = AssertionError("A disabled item reached the production resolver")
        result = self.api.get_item_availability("Disabled", BRANCH, COMPANY)
        self.assert_disabled(result)
        self.assertEqual((result["stock_policy_mode"], result["source"]), ("RESERVATION_CONTROLLED", "CATALOG"))

    def test_tier2_engine_unchanged(self):
        self.enable_reservations()
        result = self.api.get_item_availability("Fish", BRANCH, COMPANY)
        self.assertEqual(result["reason_code"], "CONFIGURATION_ERROR")
        self.assertFalse(result["sellable"])
        self.assertEqual(result["available_qty"], 0)
        self.resolver.assert_called_once()

    def test_batch_tier1_uses_two_queries(self):
        self.items = {f"ITEM-{i}": 0 for i in range(258)}
        self.resolver.side_effect = AssertionError("Tier 1 batch reached the production resolver")
        response = self.api.get_menu_availability(BRANCH, COMPANY, list(self.items))
        self.assertEqual(set(response["items"]), set(self.items))
        self.assertEqual(response["stock_policy_mode"], "NATIVE")
        self.assertTrue(all(row["sellable"] and row["available_qty"] is None for row in response["items"].values()))
        self.frappe.get_all.assert_called_once_with(
            "Item", filters={"name": ["in", list(self.items)]}, fields=["name", "disabled"]
        )
        self.frappe.db.get_value.assert_called_once()

    def test_batch_refuses_over_500_codes(self):
        with self.assertRaises(self.frappe.ValidationError):
            self.api.get_menu_availability(BRANCH, COMPANY, [f"ITEM-{i}" for i in range(501)])
        self.frappe.get_all.assert_not_called()

    def test_batch_unknown_code_is_item_disabled(self):
        result = self.api.get_menu_availability(BRANCH, COMPANY, '["No Such Item", "No Such Item"]')
        self.assertEqual(list(result["items"]), ["No Such Item"])
        self.assert_disabled(result["items"]["No Such Item"])

    def test_batch_scope_checked_once(self):
        self.frappe.session.user = "cashier@example.test"
        self.frappe.get_roles.return_value = ["URY Cashier"]
        self.frappe.db.exists.return_value = True
        self.api.get_menu_availability(BRANCH, COMPANY, list(self.items))
        self.frappe.db.exists.assert_called_once_with(
            "URY User", {"parenttype": "Branch", "parent": BRANCH, "user": "cashier@example.test"}
        )

    def test_batch_refuses_a_user_without_branch_scope(self):
        self.frappe.session.user = "other@example.test"
        self.frappe.get_roles.return_value = []
        self.frappe.db.exists.return_value = False
        with self.assertRaises(self.frappe.PermissionError):
            self.api.get_menu_availability(BRANCH, COMPANY, ["Fish"])
        self.frappe.get_all.assert_not_called()

    def test_guest_menu_availability_uses_profile_branch(self):
        self.frappe.session.user = "Guest"
        response = self.guest.get_customer_menu_availability("valid-session")
        # Background polling is not guest activity and must not renew the session.
        self.session.assert_called_once_with("valid-session", refresh_activity=False)
        self.guest.resolve_restaurant_menu.assert_called_once_with(
            branch=BRANCH, room=None, order_type="Dine In", cashier=False
        )
        self.assertTrue(any(c.args[:2] == ("URY Branch Stock Policy", BRANCH)
                            for c in self.frappe.db.get_value.call_args_list))
        self.assertEqual(set(response), {"Fish", "Disabled"})
        for value in response.values():
            self.assertEqual(set(value), {"item_code", "sellable", "available_qty", "reason_code"})
        self.assert_disabled(response["Disabled"])
        self.assertEqual(self.frappe.session.user, "Guest")

    def test_guest_menu_availability_refuses_invalid_session(self):
        self.session.side_effect = self.guest.URYGuestSessionInvalid("Invalid session")
        with self.assertRaises(self.guest.URYGuestSessionInvalid):
            self.guest.get_customer_menu_availability("invalid-session")
        self.guest.resolve_restaurant_menu.assert_not_called()
        self.frappe.set_user.assert_not_called()

    def test_guest_menu_includes_inline_availability_without_changing_items(self):
        result = self.guest.get_customer_menu("valid-session")
        self.assertEqual(result["items"], self.menu["items"])
        self.assertTrue(result["availability"]["Fish"]["sellable"])
        self.assert_disabled(result["availability"]["Disabled"])

    def test_add_customer_items_refuses_disabled_item(self):
        resolve_invoice = self.guest._resolve_or_create_pos_invoice
        resolve_invoice.side_effect = AssertionError("Disabled item accepted past the catalog gate")
        with self.assertRaisesRegex(self.frappe.ValidationError, "unavailable"):
            self.guest.add_customer_items("valid-session", [{"item": "Disabled", "qty": 1}])
        resolve_invoice.assert_not_called()
