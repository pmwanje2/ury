from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from ury.ury.doctype.ury_order.ury_order import captain_transfer


class TestCaptainTransferCandidates(TestCase):
    def setUp(self):
        from ury.ury.doctype.ury_order import ury_order

        self.order = ury_order
        self.invoice = MagicMock(
            pos_profile="Test POS Profile", branch="Test Branch", restaurant_table="T1",
            waiter="current@example.com",
        )
        self.invoice.name = "POS-INV-1"
        self.profile = SimpleNamespace(custom_enable_multiple_cashier=0,
            transfer_role_permissions=[SimpleNamespace(role="URY Manager")],
            role_restricted_for_table_order=[SimpleNamespace(role="Restaurant Kitchen")],
        )
        self.users = {
            "current@example.com": ("Current Captain", 1, ["URY Captain"]),
            "zoe@example.com": ("Zoe Captain", 1, ["URY Captain"]),
            "amina@example.com": ("Amina Captain", 1, ["URY Captain"]),
            "kitchen@example.com": ("Kitchen", 1, ["Restaurant Kitchen"]),
            "disabled@example.com": ("Disabled", 0, ["URY Captain"]),
            "Administrator": ("Administrator", 1, ["System Manager"]),
            "Guest": ("Guest", 1, ["Guest"]),
            "outside@example.com": ("Other Branch", 1, ["URY Captain"]),
        }
        self.roster = [
            {"user": user, "room": "Terrace" if user == "zoe@example.com" else "Main Hall"}
            for user in self.users if user != "outside@example.com"
        ]
        # Multiple room assignments must not duplicate a captain in the list.
        self.roster.append({"user": "amina@example.com", "room": "Terrace"})
        self.caller_roles = ["URY Manager"]
        self.permission = self.enterContext(patch.object(ury_order.frappe, "has_permission", return_value=True))
        self.enterContext(patch.object(ury_order.frappe, "get_doc", side_effect=self._get_doc))
        self.enterContext(patch.object(ury_order.frappe, "get_roles", side_effect=lambda user=None:
            self.caller_roles if user is None else self.users[user][2]))
        self.roster_read = self.enterContext(patch.object(ury_order.frappe, "get_all", side_effect=self._get_all))
        self.enterContext(patch.object(ury_order.frappe.db, "get_value", side_effect=self._get_value))
        self.enterContext(patch.object(ury_order.frappe, "get_value", side_effect=self._get_value))
        self.enterContext(patch.object(ury_order.frappe.db, "sql", side_effect=self._sql))

    def _get_doc(self, doctype, name):
        if doctype == "POS Invoice":
            self.assertEqual(name, self.invoice.name)
            return self.invoice
        if doctype == "POS Profile":
            self.assertEqual(name, self.invoice.pos_profile)
            return self.profile
        if doctype == "User":
            full_name, enabled, _roles = self.users[name]
            return SimpleNamespace(name=name, full_name=full_name, enabled=enabled)
        raise AssertionError(f"Unexpected doctype: {doctype}")

    def _get_all(self, doctype, **kwargs):
        self.assertEqual(doctype, "URY User")
        self.assertEqual(kwargs["filters"], {"parent": "Test Branch", "parenttype": "Branch"})
        self.assertNotIn("limit", kwargs)
        self.assertNotIn("limit_page_length", kwargs)
        return self.roster

    def _get_value(self, doctype, name, field):
        if doctype == "URY Table":
            self.assertEqual((name, field), ("T1", "restaurant_room"))
            return "Main Hall"
        self.assertEqual(doctype, "User")
        return {"enabled": self.users[name][1], "full_name": self.users[name][0]}[field]

    def _sql(self, query, params, **kwargs):
        self.assertIn("tabURY User", query)
        branch, user = params
        return [{"room": row["room"]} for row in self.roster
                if branch == "Test Branch" and row["user"] == user]

    def _candidates(self):
        endpoint = getattr(self.order, "get_captain_transfer_candidates", None)
        self.assertTrue(callable(endpoint), "The invoice-scoped captain candidate read is missing")
        return endpoint(self.invoice.name)

    def test_lists_only_branch_users_without_restricted_roles(self):
        self.assertEqual(self._candidates(), [
            {"name": "amina@example.com", "full_name": "Amina Captain"},
            {"name": "zoe@example.com", "full_name": "Zoe Captain"},
        ])
        self.invoice.save.assert_not_called()

    def test_excludes_disabled_users_and_administrator(self):
        names = {row["name"] for row in self._candidates()}
        self.assertTrue(names.isdisjoint({"disabled@example.com", "Administrator", "Guest"}))

    def test_excludes_current_captain(self):
        self.assertNotIn(self.invoice.waiter, {row["name"] for row in self._candidates()})

    def test_refuses_current_captain_even_if_client_claims_another_owner(self):
        current = self.invoice.waiter
        for multiple_cashier in (0, 1):
            with self.subTest(multiple_cashier=multiple_cashier):
                self.profile.custom_enable_multiple_cashier = multiple_cashier
                with self.assertRaises(frappe.PermissionError):
                    captain_transfer("amina@example.com", current, self.invoice.name)
                self.assertEqual(self.invoice.waiter, current)
                self.invoice.save.assert_not_called()

    def test_room_scoped_when_multiple_cashier(self):
        self.profile.custom_enable_multiple_cashier = 1
        self.assertEqual(self._candidates(), [{"name": "amina@example.com", "full_name": "Amina Captain"}])

    def test_denied_without_transfer_role(self):
        self.caller_roles = ["URY Captain"]
        with self.assertRaises(frappe.PermissionError):
            self._candidates()
        self.roster_read.assert_not_called()

    def test_denied_without_invoice_write(self):
        self.permission.return_value = False
        with self.assertRaises(frappe.PermissionError):
            self._candidates()
        self.permission.assert_called_once_with("POS Invoice", "write", doc=self.invoice)
        self.roster_read.assert_not_called()

    def test_denied_without_pos_profile(self):
        self.invoice.pos_profile = None
        with self.assertRaises(frappe.PermissionError):
            self._candidates()
        self.roster_read.assert_not_called()

    def test_every_candidate_is_accepted_and_every_non_candidate_refused(self):
        current = self.invoice.waiter
        for multiple_cashier in (0, 1):
            self.invoice.waiter = current
            self.profile.custom_enable_multiple_cashier = multiple_cashier
            listed = {row["name"] for row in self._candidates()}
            for user in self.users:
                with self.subTest(multiple_cashier=multiple_cashier, user=user):
                    self.invoice.waiter = current
                    self.invoice.save.reset_mock()
                    if user in listed:
                        captain_transfer(current, user, self.invoice.name)
                        self.assertEqual(self.invoice.waiter, user)
                        self.invoice.save.assert_called_once_with()
                    else:
                        with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
                            captain_transfer(current, user, self.invoice.name)
                        self.assertEqual(self.invoice.waiter, current)
                        self.invoice.save.assert_not_called()
