"""Native branch assignments and User Permissions must resolve without guessing."""

import frappe
from frappe.tests import IntegrationTestCase

from ury.ury_pos.api import getBranch, getBranchRoom, getRoom


class TestBranchResolution(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        suffix = frappe.generate_hash(length=8)
        self.user = frappe.get_doc({
            "doctype": "User", "email": f"branches-{suffix}@example.com",
            "first_name": "Branch Test Cashier", "send_welcome_email": 0,
        }).insert()
        self.user.add_roles("URY Cashier")
        self.branches = []
        self.rooms = []
        for label in ("A", "B"):
            branch = frappe.get_doc({
                "doctype": "Branch", "branch": f"Branch {label} {suffix}",
                "user": [{"user": self.user.name}],
            }).insert()
            room = frappe.get_doc({"doctype": "URY Room", "name": f"Room {label} {suffix}",
                                   "branch": branch.name}).insert()
            branch.user[0].room = room.name
            branch.save()
            self.branches.append(branch)
            self.rooms.append(room)
        frappe.set_user(self.user.name)

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.cache.hdel("user_permissions", self.user.name)
        super().tearDown()

    def set_default(self, branch):
        frappe.set_user("Administrator")
        permission = frappe.get_doc({
            "doctype": "User Permission", "user": self.user.name, "allow": "Branch",
            "for_value": branch.name, "is_default": 1, "apply_to_all_doctypes": 1,
        }).insert()
        frappe.set_user(self.user.name)
        return permission

    def test_two_branches_without_default_refuse_all_branch_apis(self):
        for api in (getBranch, getBranchRoom, getRoom):
            with self.subTest(api=api.__name__), self.assertRaisesRegex(
                frappe.ValidationError, "URY-BRANCH-AMBIGUOUS"
            ):
                api()

    def test_native_default_selects_assigned_branch(self):
        permission = self.set_default(self.branches[0])
        self.assertEqual(getBranch(), self.branches[0].name)
        frappe.set_user("Administrator")
        permission.for_value = self.branches[1].name
        permission.save()
        frappe.set_user(self.user.name)
        self.assertEqual(getBranch(), self.branches[1].name)

    def test_room_apis_use_only_the_default_branch(self):
        self.set_default(self.branches[1])
        expected = [{"name": self.rooms[1].name, "branch": self.branches[1].name}]
        self.assertEqual(getBranchRoom(), expected)
        self.assertEqual(getRoom(), expected)

    def test_unassigned_default_does_not_authorize_a_branch(self):
        frappe.set_user("Administrator")
        other = frappe.get_doc({
            "doctype": "Branch", "branch": f"Other {self.user.name}",
            "user": [{"user": "Administrator"}],
        }).insert()
        self.set_default(other)
        with self.assertRaisesRegex(frappe.ValidationError, "URY-BRANCH-AMBIGUOUS"):
            getBranch()

    def test_non_default_permission_does_not_resolve_ambiguity(self):
        permission = self.set_default(self.branches[1])
        frappe.set_user("Administrator")
        permission.is_default = 0
        permission.save()
        frappe.set_user(self.user.name)
        with self.assertRaisesRegex(frappe.ValidationError, "URY-BRANCH-AMBIGUOUS"):
            getBranch()

    def test_multiple_rooms_in_one_branch_are_not_branch_ambiguity(self):
        frappe.set_user("Administrator")
        self.branches[1].user[0].user = "Administrator"
        self.branches[1].save()
        self.branches[0].append("user", {"user": self.user.name})
        self.branches[0].save()
        frappe.set_user(self.user.name)
        self.assertEqual(getBranch(), self.branches[0].name)
        self.assertEqual({row["branch"] for row in getRoom()}, {self.branches[0].name})
        self.assertEqual(len(getRoom()), 2)

    def test_single_branch_without_room_keeps_existing_room_refusal(self):
        frappe.set_user("Administrator")
        self.branches[1].user[0].user = "Administrator"
        self.branches[1].save()
        self.branches[0].user[0].room = None
        self.branches[0].save()
        frappe.set_user(self.user.name)
        self.assertEqual(getBranch(), self.branches[0].name)
        self.assertEqual(getRoom(), [{"name": None, "branch": self.branches[0].name}])
        with self.assertRaisesRegex(frappe.ValidationError, "No room assigned"):
            getBranchRoom()
