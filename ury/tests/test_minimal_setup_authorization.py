"""Native DocPerm regressions; run only on a disposable Frappe test site."""

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from ury.ury.api.minimal import business_setup as business
from ury.ury.api.minimal import setup_organization as organization


class TestMinimalSetupAuthorization(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        if frappe.local.site != "test_tankhill_pages.local":
            raise RuntimeError("Use the isolated minimal-setup test site only")
        frappe.set_user("Administrator")
        self.original_user = "Administrator"
        self.marker = "ury-auth-" + uuid4().hex[:10]
        self.password = self.enterContext(patch("frappe.utils.password.update_password"))
        # User hooks can otherwise enqueue mail/indexing despite transaction rollback.
        self.enqueue = self.enterContext(patch("frappe.enqueue"))
        self.mail = self.enterContext(patch("frappe.sendmail"))
        self.realtime = self.enterContext(patch("frappe.publish_realtime"))
        self.commit = self.enterContext(patch.object(frappe.db, "commit"))
        self.roles = []
        self.users = []
        self.addCleanup(self.cleanup_fixture)
        frappe.db.savepoint("minimal_setup_fixture")
        self.creator_role = self.make_role("Creator")
        self.editor_role = self.make_role("Editor")
        self.grant("User", self.creator_role, create=1, read=1, write=1)
        self.grant("Branch", self.editor_role, create=1, read=1, write=1)
        self.grant("URY Restaurant", self.editor_role, create=1, read=1, write=1)
        self.grant("Company", self.editor_role, read=1)
        self.creator = self.make_user("creator", self.creator_role)
        self.cashier = self.make_user("cashier", "URY Cashier")
        self.owner = self.make_user("owner", "URY Admin")
        self.manager = self.make_user("manager", "System Manager")
        self.editor = self.make_user("editor", self.editor_role)
        self.company = frappe.get_all("Company", pluck="name", limit=1)[0]
        self.branch = frappe.get_doc({
            "doctype": "Branch", "branch": self.marker,
            "user": [{"user": self.cashier}],
        }).insert().name
        self.other_branch = frappe.get_doc({
            "doctype": "Branch", "branch": self.marker + "-other",
            "user": [{"user": self.editor}],
        }).insert().name
        self.room = frappe.get_doc({
            "doctype": "URY Room", "name": self.marker + "-room",
            "branch": self.branch, "room_type": "AC",
        }).insert().name
        self.restaurant = frappe.get_doc({
            "doctype": "URY Restaurant", "name": self.marker + "-restaurant",
            "company": self.company, "branch": self.branch,
            "default_room": self.room, "invoice_series_prefix": "TEST-",
        }).insert().name

    def make_role(self, suffix):
        role = frappe.get_doc({"doctype": "Role", "role_name": self.marker + suffix, "desk_access": 1}).insert()
        self.roles.append(role.name)
        return role.name

    def grant(self, doctype, role, **permissions):
        frappe.get_doc({
            "doctype": "Custom DocPerm", "parent": doctype,
            "role": role, "permlevel": 0, **permissions,
        }).insert()
        frappe.clear_cache(doctype=doctype)

    def make_user(self, suffix, role):
        email = f"{self.marker}-{suffix}@example.invalid"
        frappe.get_doc({
            "doctype": "User", "email": email, "first_name": "Isolated Fixture",
            "enabled": 1, "send_welcome_email": 0, "user_type": "System User",
            "roles": [{"role": role}],
        }).insert()
        self.users.append(email)
        return email

    def cleanup_fixture(self):
        frappe.set_user("Administrator")
        frappe.db.rollback(save_point="minimal_setup_fixture")
        for user in self.users:
            frappe.clear_cache(user=user)
            self.assertFalse(frappe.db.exists("User", user))
            self.assertFalse(frappe.db.exists("Has Role", {"parent": user}))
        for role in self.roles:
            self.assertFalse(frappe.db.exists("Role", role))
        for doctype in ("User", "Branch", "URY Restaurant", "Company"):
            frappe.clear_cache(doctype=doctype)

    def restrict_branch(self, user):
        frappe.set_user("Administrator")
        frappe.get_doc({
            "doctype": "User Permission", "user": user, "allow": "Branch",
            "for_value": self.branch, "apply_to_all_doctypes": 1, "is_default": 1,
        }).insert()
        frappe.clear_cache(user=user)
        frappe.set_user(user)

    def test_permlevel_zero_creator_cannot_assign_roles_or_use_duplicate_shortcut(self):
        frappe.set_user(self.creator)
        probe = frappe.new_doc("User")
        self.assertTrue(probe.has_permission("create"))
        self.assertFalse(probe.has_permlevel_access_to("roles", permission_type="write"))
        new_email = f"{self.marker}-target@example.invalid"
        for email in (self.cashier, new_email):
            with self.subTest(existing=email == self.cashier):
                with self.assertRaises(frappe.PermissionError):
                    business.create_setup_user(email, "Fixture", role="URY Cashier")
        self.assertFalse(frappe.db.exists("User", new_email))
        self.password.assert_not_called()

    def test_cashier_cannot_create_default_or_privileged_user(self):
        frappe.set_user(self.cashier)
        self.assertFalse(frappe.new_doc("User").has_permission("create"))
        for role in ("URY Cashier", "System Manager"):
            for email in (self.cashier, f"{self.marker}-denied@example.invalid"):
                with self.subTest(role=role, existing=email == self.cashier):
                    with self.assertRaises(frappe.PermissionError):
                        business.create_setup_user(email, "Fixture", role=role)
        self.password.assert_not_called()

    def test_native_administrator_and_manager_creation_and_duplicate(self):
        for actor in ("Administrator", self.manager):
            frappe.set_user(actor)
            email = f"{self.marker}-target-{len(self.users)}@example.invalid"
            self.users.append(email)
            self.assertEqual(business.create_setup_user(email, "Fixture"), {"status": "created", "email": email})
            self.assertIn("URY Cashier", frappe.get_roles(email))
            self.assertEqual(business.create_setup_user(email, "Other", role="System Manager"), {"status": "exists", "email": email})
            self.assertNotIn("System Manager", frappe.get_roles(email))
            self.assertEqual(frappe.db.get_value("User", email, "first_name"), "Fixture")
        self.password.assert_not_called()

    def test_native_validation_is_preserved(self):
        frappe.set_user(self.manager)
        with self.assertRaises(frappe.ValidationError):
            business.create_setup_user("invalid-email", "Fixture")

    def test_native_technical_setup_denials_have_no_downstream_work(self):
        with patch.object(organization, "setup_complete") as inner, \
             patch.object(frappe.db, "get_single_value", return_value=0) as settings:
            for actor in ("Guest", self.cashier, self.owner):
                frappe.set_user(actor)
                for entry in (business.submit_configure_data, organization.submit_setup,
                              organization.complete_wizard_setup):
                    with self.subTest(actor=actor, entry=entry.__name__):
                        try:
                            entry({})
                        except Exception as error:
                            self.assertIsInstance(error, frappe.PermissionError)
                        else:
                            self.fail("Native non-admin actor entered technical setup")
            inner.assert_not_called()
            settings.assert_not_called()
        self.commit.assert_not_called()

    def test_branch_create_and_write_denials(self):
        frappe.set_user(self.creator)
        for data in ({"name": self.branch, "branch": "Changed"},
                     {"branch": self.marker + "-new", "user": [{"user": self.creator}]}):
            with self.subTest(data=data):
                with self.assertRaises(frappe.PermissionError):
                    business.update_business_setup(branch=data)
        self.assertEqual(frappe.db.get_value("Branch", self.branch, "branch"), self.marker)

    def test_restaurant_create_and_write_denials(self):
        frappe.set_user(self.creator)
        for data in ({"name": self.restaurant, "invoice_series_prefix": "Changed"},
                     {"name": self.marker + "-new", "company": self.company, "branch": self.branch,
                      "default_room": self.room, "invoice_series_prefix": "NEW-"}):
            with self.subTest(existing=data["name"] == self.restaurant):
                with self.assertRaises(frappe.PermissionError):
                    business.update_business_setup(restaurant=data)
        self.assertEqual(frappe.db.get_value("URY Restaurant", self.restaurant, "invoice_series_prefix"), "TEST-")

    def test_native_permitted_branch_and_restaurant_writes(self):
        frappe.set_user(self.editor)
        self.assertEqual(business.update_business_setup(branch={"name": self.branch, "custom_make_unpaid": 1}), {"status": "success"})
        self.assertEqual(business.update_business_setup(restaurant={"name": self.restaurant, "invoice_series_prefix": "EDIT-"}), {"status": "success"})
        self.assertEqual(frappe.get_doc("Branch", self.branch).custom_make_unpaid, 1)
        self.assertEqual(frappe.get_doc("URY Restaurant", self.restaurant).invoice_series_prefix, "EDIT-")

    def test_native_user_permission_denies_other_branch(self):
        self.restrict_branch(self.editor)
        self.assertTrue(frappe.get_doc("Branch", self.branch).has_permission("write"))
        self.assertFalse(frappe.get_doc("Branch", self.other_branch).has_permission("write"))
        with self.assertRaises(frappe.PermissionError):
            business.update_business_setup(branch={"name": self.other_branch, "branch": "Changed"})
        self.assertEqual(frappe.db.get_value("Branch", self.other_branch, "branch"), self.marker + "-other")

    def test_native_branch_write_does_not_grant_company_tax_write(self):
        previous = frappe.db.get_value("Company", self.company, "tax_id")
        frappe.set_user(self.editor)
        self.assertFalse(frappe.get_doc("Company", self.company).has_permission("write"))
        with self.assertRaises(frappe.PermissionError):
            business.update_business_setup(branch={"name": self.branch, "tax_id": "TEST-TAX"})
        self.assertEqual(frappe.db.get_value("Company", self.company, "tax_id"), previous)

    def test_native_company_write_saves_tax(self):
        frappe.set_user("Administrator")
        business.update_business_setup(branch={"name": self.branch, "tax_id": "TEST-TAX"})
        self.assertEqual(frappe.db.get_value("Company", self.company, "tax_id"), "TEST-TAX")

    def test_native_high_permlevel_write_is_not_changed(self):
        meta = frappe.get_meta("URY Restaurant")
        with patch.object(meta.get_field("invoice_series_prefix"), "permlevel", 1):
            frappe.set_user(self.editor)
            business.update_business_setup(restaurant={"name": self.restaurant, "invoice_series_prefix": "FORBIDDEN-"})
        self.assertEqual(frappe.db.get_value("URY Restaurant", self.restaurant, "invoice_series_prefix"), "TEST-")

    def test_native_assigned_cashier_dropdown_and_default_branch(self):
        self.restrict_branch(self.cashier)
        rows = business.get_branches()
        self.assertEqual([row["id"] for row in rows], [self.branch])
        self.assertEqual(rows[0]["name"], self.marker)
        self.assertEqual(set(rows[0]), {"id", "name", "tax_id"})
        self.assertEqual(frappe.defaults.get_user_default("Branch"), self.branch)

    def test_native_dropdown_null_tax_when_company_is_unreadable(self):
        # This creator role has no Company read; Branch access is granted natively.
        frappe.set_user("Administrator")
        self.grant("Branch", self.creator_role, read=1)
        self.restrict_branch(self.creator)
        self.assertFalse(frappe.has_permission("Company", "read"))
        rows = business.get_branches()
        self.assertEqual(rows, [{"id": self.branch, "name": self.marker, "tax_id": None}])

    def test_native_read_denials_are_not_unrestricted_fallbacks(self):
        frappe.set_user(self.creator)
        with self.assertRaises(frappe.PermissionError):
            business.get_business_setup()
        with self.assertRaises(frappe.PermissionError):
            business.get_branches()

    def test_native_hidden_company_tax_does_not_leak_into_dropdown(self):
        frappe.set_user("Administrator")
        frappe.db.set_value("Company", self.company, "tax_id", "PRIVATE-TAX")
        field = frappe.get_meta("Company").get_field("tax_id")
        with patch.object(field, "permlevel", 1):
            self.restrict_branch(self.editor)
            self.assertFalse(frappe.get_doc("Company", self.company).has_permlevel_access_to("tax_id"))
            self.assertEqual(business.get_branches(), [{"id": self.branch, "name": self.marker, "tax_id": None}])

    def test_native_full_branch_serialization_filters_child_fields(self):
        meta = frappe.get_meta("Branch")
        child_field = frappe.get_meta(meta.get_field("user").options).get_field("user")
        with patch.object(child_field, "permlevel", 1):
            frappe.set_user(self.editor)
            result = business.get_business_setup()["data"]
            branch = next(row for row in result["branches"] if row["name"] == self.branch)
            self.assertIn("user", branch)
            self.assertTrue(branch["user"])
            self.assertTrue(all(not row.get("user") for row in branch["user"]))

    def test_native_full_document_serialization_masks_fields(self):
        field = frappe.get_meta("Branch").get_field("branch")
        with patch.object(field, "mask", 1, create=True):
            frappe.set_user(self.editor)
            result = business.get_business_setup()["data"]
            branch = next(row for row in result["branches"] if row["name"] == self.branch)
            self.assertNotEqual(branch["branch"], self.marker)

    def test_native_permitted_branch_and_restaurant_creation(self):
        frappe.set_user(self.editor)
        new_branch = self.marker + "-created"
        business.update_business_setup(branch={"branch": new_branch, "user": [{"user": self.editor}]})
        self.assertTrue(frappe.db.exists("Branch", new_branch))
        new_restaurant = self.marker + "-created-restaurant"
        business.update_business_setup(restaurant={
            "name": new_restaurant, "company": self.company, "branch": self.branch,
            "default_room": self.room, "invoice_series_prefix": "NEW-",
        })
        self.assertTrue(frappe.db.exists("URY Restaurant", new_restaurant))

    def test_native_staff_wizard_status_is_boolean_only(self):
        frappe.set_user(self.cashier)
        result = organization.get_wizard_status()
        self.assertEqual(result, {"step1_complete": True, "step2_complete": True})
        self.assertTrue(all(type(value) is bool for value in result.values()))
        frappe.set_user("Guest")
        with self.assertRaises(frappe.PermissionError):
            organization.get_wizard_status()
