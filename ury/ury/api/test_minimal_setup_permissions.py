"""Focused permission and side-effect contracts for the minimal setup APIs."""

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ury.ury.api.minimal import business_setup as business
from ury.ury.api.minimal import setup_organization as organization


def throw(message, exception=frappe.ValidationError):
    raise exception(message)


class PermissionDocument:
    """Model native checks, including the old ignore_permissions bypass."""

    def __init__(self, doctype, name, *, denied=(), roles_write=True, **values):
        self.doctype = doctype
        self.name = name
        self.denied = set(denied)
        self.roles_write = roles_write
        self.values = {"doctype": doctype, "name": name, **values}
        self.user = []
        self.checked = []
        self.mutations = []
        self.saved = []
        self.filtered = False

    def check_permission(self, permission):
        self.checked.append(permission)
        if permission in self.denied:
            raise frappe.PermissionError

    def has_permlevel_access_to(self, fieldname, permission_type="read"):
        assert (fieldname, permission_type) == ("roles", "write")
        return self.roles_write

    def update(self, values):
        self.mutations.append(values)
        self.values.update(values)

    def insert(self, ignore_permissions=False):
        if not ignore_permissions:
            self.check_permission("create")
        self.saved.append(ignore_permissions)
        return self

    def save(self, ignore_permissions=False):
        if not ignore_permissions:
            self.check_permission("write")
        self.saved.append(ignore_permissions)
        return self

    def append(self, field, value):
        self.mutations.append(value)

    def apply_fieldlevel_read_permissions(self):
        self.filtered = True

    def as_dict(self):
        result = dict(self.values)
        if self.filtered:
            result.pop("private_field", None)
            result["children"] = [{"public": "visible"}]
        return result

    def get(self, key, default=None):
        return self.values.get(key, default)


class MinimalSetupPermissions(unittest.TestCase):
    def setUp(self):
        self.framework = MagicMock()
        self.framework.PermissionError = frappe.PermissionError
        self.framework.throw.side_effect = throw
        self.framework.session.user = "cashier@example.invalid"
        self.framework.only_for.side_effect = self.only_for
        self.framework.defaults.get_user_default.return_value = "Company"
        self.framework.db.get_value.return_value = "Company"
        self.framework.db.get_single_value.return_value = 0
        self.framework.get_all.return_value = [frappe._dict(name="Company")]
        self.framework.db.exists.return_value = True
        self.enterContext(patch.object(business, "frappe", self.framework))
        self.enterContext(patch.object(organization, "frappe", self.framework))
        self.password = self.enterContext(patch("frappe.utils.password.update_password"))
        self.inner = self.enterContext(patch.object(organization, "setup_complete", return_value={"ok": True}))
        self.documents = {}
        self.framework.get_doc.side_effect = self.get_doc
        self.framework.new_doc.side_effect = lambda doctype: self.get_doc(doctype, "New")
        self.framework.get_list.side_effect = self.get_list

    def only_for(self, role):
        if role != "System Manager" or self.framework.session.user not in (
            "Administrator", "manager@example.invalid",
        ):
            raise frappe.PermissionError

    def get_doc(self, doctype, name=None):
        if isinstance(doctype, dict):
            values = dict(doctype)
            doctype = values.pop("doctype")
            name = values.pop("name", values.get("email", "New"))
            key = (doctype, name)
            if key not in self.documents:
                self.documents[key] = PermissionDocument(doctype, name, **values)
        return self.documents.setdefault((doctype, name), PermissionDocument(doctype, name))

    def get_list(self, doctype, **kwargs):
        if doctype == "Company":
            return [frappe._dict(name="Company")]
        return [frappe._dict(name="Branch", branch="Branch")]

    def user_document(self, **kwargs):
        doc = PermissionDocument("User", "new@example.invalid", **kwargs)
        self.documents[("User", doc.name)] = doc
        return doc

    def assert_no_setup_effects(self):
        self.inner.assert_not_called()
        self.framework.publish_realtime.assert_not_called()
        self.framework.cache.set_value.assert_not_called()
        self.framework.db.commit.assert_not_called()
        self.framework.db.set_single_value.assert_not_called()
        self.framework.db.get_single_value.assert_not_called()
        self.framework.parse_json.assert_not_called()
        self.framework.get_doc.assert_not_called()
        self.framework.db.exists.assert_not_called()

    def test_technical_entries_deny_before_any_work(self):
        for actor in ("Guest", "cashier@example.invalid", "owner@example.invalid"):
            for entry in (business.submit_configure_data, organization.submit_setup,
                          organization.complete_wizard_setup):
                with self.subTest(actor=actor, entry=entry.__name__):
                    self.framework.session.user = actor
                    try:
                        entry({})
                    except Exception as error:
                        self.assertIsInstance(error, frappe.PermissionError)
                    else:
                        self.fail("Technical setup accepted a non-administrator")
                    self.assert_no_setup_effects()

    def test_authorized_setup_preserves_demo_selection_and_realtime(self):
        for actor in ("Administrator", "manager@example.invalid"):
            self.framework.session.user = actor
            for entry in (organization.submit_setup, organization.complete_wizard_setup):
                for demo in (0, 1):
                    with self.subTest(actor=actor, entry=entry.__name__, demo=demo):
                        original = self.framework.publish_realtime
                        self.assertEqual(entry({"setup_ury_demo": demo}), {"ok": True})
                        self.assertEqual(self.inner.call_args.kwargs["args"]["setup_ury_demo"], demo)
                        self.assertIs(self.framework.publish_realtime, original)

    def test_authorized_completed_setup_still_refused(self):
        self.framework.session.user = "Administrator"
        self.framework.db.get_single_value.return_value = 1
        with self.assertRaises(frappe.ValidationError):
            organization.submit_setup({})
        self.inner.assert_not_called()

    def test_user_create_denied_before_existence_or_password(self):
        for role in ("URY Cashier", "System Manager"):
            for exists in (False, True):
                with self.subTest(role=role, exists=exists):
                    self.framework.db.exists.return_value = exists
                    doc = self.user_document(denied=("create",))
                    with self.assertRaises(frappe.PermissionError):
                        business.create_setup_user(doc.name, "Fixture", password="unused", role=role)
                    self.framework.db.exists.assert_not_called()
                    self.password.assert_not_called()
                    self.assertEqual(doc.saved, [])

    def test_user_roles_field_denied_even_with_native_create(self):
        for exists in (False, True):
            with self.subTest(exists=exists):
                self.framework.db.exists.return_value = exists
                doc = self.user_document(roles_write=False)
                with self.assertRaises(frappe.PermissionError):
                    business.create_setup_user(doc.name, "Fixture", password="unused")
                self.framework.db.exists.assert_not_called()
                self.password.assert_not_called()
                self.assertEqual(doc.saved, [])

    def test_authorized_new_user_uses_normal_insert_then_password(self):
        self.framework.db.exists.return_value = False
        doc = self.user_document()
        result = business.create_setup_user(doc.name, "Fixture", password="unused")
        self.assertEqual(result, {"status": "created", "email": doc.name})
        self.assertIn("create", doc.checked)
        self.assertEqual(doc.saved, [False])
        self.password.assert_called_once_with(user=doc.name, pwd="unused")

    def test_authorized_duplicate_does_not_touch_roles_or_password(self):
        doc = self.user_document()
        result = business.create_setup_user(doc.name, "Fixture", password="unused")
        self.assertEqual(result, {"status": "exists", "email": doc.name})
        self.assertIn("create", doc.checked)
        self.assertEqual(doc.saved, [])
        self.password.assert_not_called()

    def test_crud_denials_precede_mutation(self):
        for doctype, arg, payload in (
            ("Branch", "branch", {"name": "Branch", "branch": "Changed"}),
            ("URY Restaurant", "restaurant", {"name": "Company", "invoice_series_prefix": "CHANGED"}),
        ):
            for exists, permission, name in ((True, "write", payload["name"]), (False, "create", "New")):
                with self.subTest(doctype=doctype, permission=permission):
                    self.framework.db.exists.return_value = exists
                    doc = PermissionDocument(doctype, name, denied=(permission,))
                    self.documents[(doctype, name)] = doc
                    with self.assertRaises(frappe.PermissionError):
                        business.update_business_setup(**{arg: payload})
                    self.assertEqual(doc.saved, [])
                    self.assertEqual(doc.mutations, [])

    def test_permitted_crud_uses_normal_orm(self):
        for doctype, arg, name in (("Branch", "branch", "Branch"),
                                    ("URY Restaurant", "restaurant", "Company")):
            for exists, permission in ((True, "write"), (False, "create")):
                with self.subTest(doctype=doctype, permission=permission):
                    self.framework.db.exists.return_value = exists
                    key = (doctype, name if exists else "New")
                    doc = PermissionDocument(*key)
                    self.documents[key] = doc
                    self.assertEqual(business.update_business_setup(**{arg: {"name": name}}), {"status": "success"})
                    self.assertIn(permission, doc.checked)
                    self.assertEqual(doc.saved, [False])
        self.framework.db.commit.assert_not_called()

    def test_branch_write_does_not_authorize_company_tax_write(self):
        company = PermissionDocument("Company", "Company", denied=("write",))
        self.documents[("Company", "Company")] = company
        with self.assertRaises(frappe.PermissionError):
            business.update_business_setup(branch={"name": "Branch", "tax_id": "changed"})
        self.framework.db.set_value.assert_not_called()
        self.assertEqual(company.saved, [])
        self.assertEqual(company.mutations, [])

    def test_permitted_company_tax_write_uses_normal_save(self):
        business.update_business_setup(branch={"name": "Branch", "tax_id": "changed"})
        self.assertIn(("Company", "Company"), self.documents)
        company = self.documents[("Company", "Company")]
        self.assertIn("write", company.checked)
        self.assertEqual(company.tax_id, "changed")
        self.assertEqual(company.saved, [False])
        self.framework.db.set_value.assert_not_called()

    def test_business_read_checks_and_filters_full_documents(self):
        self.documents[("Branch", "Branch")] = PermissionDocument("Branch", "Branch", private_field="hidden")
        self.documents[("URY Restaurant", "Company")] = PermissionDocument("URY Restaurant", "Company", private_field="hidden")
        result = business.get_business_setup()["data"]
        self.assertEqual(result["company"], "Company")
        for value in (*result["branches"], result["restaurant"]):
            self.assertNotIn("private_field", value)
        for key in (("Branch", "Branch"), ("URY Restaurant", "Company")):
            self.assertIn("read", self.documents[key].checked)
            self.assertTrue(self.documents[key].filtered)
        self.framework.get_all.assert_not_called()

    def test_denied_restaurant_is_not_serialized(self):
        self.documents[("URY Restaurant", "Company")] = PermissionDocument("URY Restaurant", "Company", denied=("read",))
        with self.assertRaises(frappe.PermissionError):
            business.get_business_setup()

    def test_denied_company_has_no_unrestricted_fallback(self):
        self.framework.get_list.side_effect = frappe.PermissionError
        with self.assertRaises(frappe.PermissionError):
            business.get_business_setup()
        self.framework.get_all.assert_not_called()
        self.framework.db.get_value.assert_not_called()

    def test_branch_dropdown_survives_unreadable_company(self):
        def rows(doctype, **kwargs):
            if doctype == "Company":
                raise frappe.PermissionError
            return [frappe._dict(name="Assigned", branch="Assigned Branch")]
        self.framework.get_list.side_effect = rows
        self.assertEqual(business.get_branches(), [{"id": "Assigned", "name": "Assigned Branch", "tax_id": None}])
        self.framework.db.get_value.assert_not_called()
        self.framework.only_for.assert_not_called()

    def test_denied_branch_read_propagates(self):
        def rows(doctype, **kwargs):
            if doctype == "Branch":
                raise frappe.PermissionError
            return [frappe._dict(name="Company")]
        self.framework.get_list.side_effect = rows
        with self.assertRaises(frappe.PermissionError):
            business.get_branches()

    def test_hidden_tax_is_not_looked_up_raw(self):
        company = PermissionDocument("Company", "Company")
        self.documents[("Company", "Company")] = company
        self.assertEqual(business.get_branches(), [{"id": "Branch", "name": "Branch", "tax_id": None}])
        self.assertIn("read", company.checked)
        self.assertTrue(company.filtered)
        self.framework.db.get_value.assert_not_called()

    def test_guest_readers_are_refused(self):
        self.framework.session.user = "Guest"
        for entry in (business.get_branches, business.get_business_setup, organization.get_wizard_status,
                      organization.get_setup_defaults, organization.get_setup_progress_steps,
                      organization.get_setup_progress_status):
            with self.subTest(entry=entry.__name__):
                with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
                    entry()

    def test_staff_wizard_status_exactly_two_booleans(self):
        for company in (False, True):
            for branch in (False, True):
                self.framework.db.exists.side_effect = lambda doctype, filters: company if doctype == "Company" else branch
                self.assertEqual(organization.get_wizard_status(), {
                    "step1_complete": company, "step2_complete": branch,
                })
        self.framework.only_for.assert_not_called()

    def configure_fixture(self):
        self.framework.session.user = "manager@example.invalid"
        self.framework.get_all.return_value = []
        self.framework.get_meta.return_value.has_field.return_value = False
        self.framework.get_meta.return_value.get_table_fields.return_value = []

    def test_configure_permission_error_in_branch_attachment_aborts_commit(self):
        self.configure_fixture()
        doc = PermissionDocument("Branch", "Company", denied=("write",))
        self.documents[("Branch", "Company")] = doc
        with self.assertRaises(frappe.PermissionError):
            business.submit_configure_data({"users": [{"email": "fixture@example.invalid"}]})
        self.framework.db.commit.assert_not_called()
        self.framework.db.set_single_value.assert_not_called()

    def test_configure_permission_error_in_pos_creation_aborts_commit(self):
        self.configure_fixture()
        self.framework.db.exists.side_effect = lambda doctype, *args: doctype != "Warehouse"
        doc = PermissionDocument("Warehouse", "New", denied=("create",))
        self.documents[("Warehouse", "New")] = doc
        with self.assertRaises(frappe.PermissionError):
            business.submit_configure_data({})
        self.framework.db.commit.assert_not_called()
        self.framework.db.set_single_value.assert_not_called()

    def test_configure_nested_user_denial_aborts_commit(self):
        self.configure_fixture()
        self.documents[("User", "fixture@example.invalid")] = PermissionDocument("User", "fixture@example.invalid", roles_write=False)
        with self.assertRaises(frappe.PermissionError):
            business.submit_configure_data({"users": [{"email": "fixture@example.invalid"}]})
        self.password.assert_not_called()
        self.framework.db.commit.assert_not_called()

    def test_authorized_configure_completion_uses_native_settings_save(self):
        self.configure_fixture()
        result = business.submit_configure_data({})
        self.assertEqual(result["status"], "success")
        self.assertIn(("System Settings", None), self.documents)
        settings = self.documents[("System Settings", None)]
        self.assertEqual(settings.setup_complete, 1)
        self.assertEqual(settings.saved, [False])
        self.framework.db.set_single_value.assert_not_called()
        self.framework.db.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
