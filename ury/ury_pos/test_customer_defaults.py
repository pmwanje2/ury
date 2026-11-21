import unittest
from unittest.mock import patch

import frappe

from ury.ury_pos.api import create_customer


class TestCreateCustomerDefaults(unittest.TestCase):
    """Site-less regression tests for native Selling Settings defaults."""

    def setUp(self):
        self.frappe = self.enterContext(patch("ury.ury_pos.api.frappe"))
        self.enterContext(patch("ury.ury_pos.api.validate_customer_mobile"))
        self.get_root_of = self.enterContext(patch("frappe.utils.nestedset.get_root_of"))
        self.get_root_of.return_value = "Tous les territoires"
        self.enterContext(patch("ury.ury_pos.api.getBranch", return_value=None))
        self.frappe.has_permission.return_value = True

        def throw_validation_error(message):
            raise frappe.ValidationError(message)

        self.frappe.throw.side_effect = throw_validation_error
        self.defaults = {"territory": "Uganda", "customer_group": "Retail"}
        self.frappe.db.get_single_value.side_effect = (
            lambda doctype, field: self.defaults[field] if doctype == "Selling Settings" else None
        )

    def assert_customer_defaults(self, expected_territory, expected_customer_group, **kwargs):
        result = create_customer("Test Customer", "+256772123456", **kwargs)
        self.assertEqual(result["status"], "success")
        payload = self.frappe.get_doc.call_args.args[0]
        self.assertEqual(payload["doctype"], "Customer")
        self.assertEqual(payload["territory"], expected_territory)
        self.assertEqual(payload["customer_group"], expected_customer_group)
        self.assertEqual(result["territory"], expected_territory)
        self.assertEqual(result["customer_group"], expected_customer_group)
        self.frappe.get_doc.return_value.insert.assert_called_once_with(ignore_permissions=False)
        self.frappe.db.commit.assert_called_once_with()

    def test_omitted_values_use_selling_settings(self):
        self.assert_customer_defaults("Uganda", "Retail")
        self.get_root_of.assert_not_called()

    def test_unset_customer_group_raises_configuration_error_without_creating_customer(self):
        self.defaults = {"territory": None, "customer_group": None}
        with self.assertRaisesRegex(frappe.ValidationError, "Default Customer Group.*Selling Settings"):
            create_customer("Test Customer", "+256772123456")
        self.frappe.get_doc.assert_not_called()
        self.frappe.db.commit.assert_not_called()

    def test_blank_customer_group_raises_configuration_error_without_creating_customer(self):
        self.defaults = {"territory": "", "customer_group": ""}
        with self.assertRaisesRegex(frappe.ValidationError, "Default Customer Group.*Selling Settings"):
            create_customer("Test Customer", "+256772123456", territory="Kenya")
        self.frappe.get_doc.assert_not_called()
        self.frappe.db.commit.assert_not_called()

    def test_unset_territory_uses_native_translated_root(self):
        self.defaults["territory"] = None
        self.assert_customer_defaults("Tous les territoires", "Retail")
        self.get_root_of.assert_called_once_with("Territory")

    def test_blank_territory_uses_native_translated_root(self):
        self.defaults["territory"] = ""
        self.assert_customer_defaults("Tous les territoires", "Retail")
        self.get_root_of.assert_called_once_with("Territory")

    def test_explicit_customer_group_with_unset_settings_uses_native_territory_root(self):
        self.defaults = {"territory": None, "customer_group": None}
        self.assert_customer_defaults("Tous les territoires", "Wholesale", customer_group="Wholesale")
        self.get_root_of.assert_called_once_with("Territory")

    def test_explicit_values_override_selling_settings(self):
        self.assert_customer_defaults(
            "Kenya", "Wholesale", territory="Kenya", customer_group="Wholesale"
        )
        self.assertFalse(any(call.args[0] == "Selling Settings" for call in self.frappe.db.get_single_value.call_args_list))
        self.get_root_of.assert_not_called()

    def test_explicit_values_work_without_selling_settings_defaults(self):
        self.defaults = {"territory": None, "customer_group": None}
        self.assert_customer_defaults(
            "Kenya", "Wholesale", territory="Kenya", customer_group="Wholesale"
        )
        self.assertFalse(any(call.args[0] == "Selling Settings" for call in self.frappe.db.get_single_value.call_args_list))
        self.get_root_of.assert_not_called()

    def test_blank_values_use_selling_settings(self):
        self.assert_customer_defaults("Uganda", "Retail", territory="", customer_group="")
        self.get_root_of.assert_not_called()

    def test_explicit_territory_preserves_default_customer_group(self):
        self.assert_customer_defaults("Kenya", "Retail", territory="Kenya")
        self.get_root_of.assert_not_called()

    def test_explicit_customer_group_preserves_default_territory(self):
        self.assert_customer_defaults("Uganda", "Wholesale", customer_group="Wholesale")
        self.get_root_of.assert_not_called()

    def test_denied_permission_does_not_read_defaults_or_create_customer(self):
        self.frappe.has_permission.return_value = False
        self.frappe.PermissionError = frappe.PermissionError
        self.frappe.throw.side_effect = frappe.PermissionError("Not permitted")
        with self.assertRaises(frappe.PermissionError):
            create_customer("Test Customer", "+256772123456")
        self.assertFalse(any(call.args[0] == "Selling Settings" for call in self.frappe.db.get_single_value.call_args_list))
        self.frappe.get_doc.assert_not_called()
        self.get_root_of.assert_not_called()
