"""Transaction and permission contracts on the disposable Frappe site."""
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from ury.ury.api import ury_print as receipts
from ury.ury.api.ury_kot_display import get_production_units


def row(doctype, **fields):
    doc = frappe.get_doc(dict(doctype=doctype, **fields))
    doc.db_insert()
    return doc


class SiteContracts(TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.addCleanup(frappe.db.rollback)
        self.addCleanup(frappe.set_user, "Administrator")
        self.prefix = "R2-" + uuid4().hex[:10]

    def prepare(self):
        self.table = row("URY Table", name=self.prefix, occupied=1)
        self.invoice = row("POS Invoice", name=self.prefix, restaurant_table=self.table.name,
                           invoice_printed=0, docstatus=1, grand_total=100)
        self.enterContext(patch("frappe.www.printview.get_html_and_style",
                                return_value={"html": "<p>Guest check</p>", "style": ""}))
        return receipts.prepare_receipt_delivery(self.invoice.name, str(uuid4()))

    def test_confirmation_keeps_release_and_manufacture_in_request_transaction(self):
        prepared = self.prepare()
        frappe.db.savepoint("before_delivery")
        with patch.object(frappe.db, "commit") as inner_commit, patch(
            "ury.ury.api.ury_fulfilment_posting_service.trigger_manufacture_on_bill_print",
            side_effect=frappe.ValidationError("manufacture interrupted"),
        ):
            with self.assertRaisesRegex(frappe.ValidationError, "manufacture interrupted"):
                receipts.complete_receipt_delivery(self.invoice.name, prepared["delivery_id"])
            inner_commit.assert_not_called()
        frappe.db.rollback(save_point="before_delivery")
        self.assertEqual(self.table.reload().occupied, 1)
        self.assertEqual(self.invoice.reload().invoice_printed, 0)
        self.assertEqual(frappe.db.get_value("URY Receipt Delivery", prepared["delivery_id"], "delivered"), 0)

    def test_changed_check_cannot_confirm_even_when_modified_timestamp_is_preserved(self):
        prepared = self.prepare()
        frappe.db.set_value("POS Invoice", self.invoice.name, "grand_total", 125, update_modified=False)
        with self.assertRaisesRegex(frappe.ValidationError, "(?i)changed|stale"):
            receipts.complete_receipt_delivery(self.invoice.name, prepared["delivery_id"])
        self.assertEqual(self.table.reload().occupied, 1)
        self.assertEqual(self.invoice.reload().invoice_printed, 0)

    def test_authorized_copy_number_revision_and_label_belong_to_rendered_snapshot(self):
        with patch("ury.ury_pos.floor_actions.authorize_and_count_bill_print",
                   return_value={"print_number": 2, "is_reprint": True, "reprint_label": "COPY #2 / REPRINT"}):
            result = self.prepare()
        self.assertEqual(result.get("print_number"), 2)
        self.assertEqual(result["copy_label"], "COPY #2 / REPRINT")
        self.assertTrue(result["invoice_revision"])
        self.assertIn(result["copy_label"], result["html"])
        self.assertIn("Guest check", result["html"])
        response = receipts.receipt_delivery_view(result["delivery_id"])
        self.assertIn(result["html"], response.get_data(as_text=True))

    def station_fixture(self):
        self.company = row("Company", name=self.prefix, abbr=self.prefix[-5:]).name
        self.branch = row("Branch", name=self.prefix, company=self.company).name
        self.station = row("URY Production Unit", name=self.prefix, production=self.prefix, branch=self.branch)
        self.user = self.prefix.lower() + "@example.test"
        row("User", name=self.user, email=self.user, enabled=1, user_type="System User")
        row("Has Role", name=uuid4().hex, parenttype="User", parentfield="roles", parent=self.user, role="System Manager")
        row("Has Role", name=uuid4().hex, parenttype="User", parentfield="roles", parent=self.user, role="URY Admin")
        row("User Permission", name=uuid4().hex, user=self.user, allow="Company", for_value=self.company,
            apply_to_all_doctypes=1)
        frappe.clear_cache(user=self.user)

    def test_strict_listing_resolves_company_from_branch_and_preserves_role_permissions(self):
        self.station_fixture()
        other = row("Branch", name=self.prefix + "-B", company=self.company + "-B")
        row("URY Production Unit", name=other.name, branch=other.name)
        frappe.db.set_single_value("System Settings", "apply_strict_user_permissions", 1)
        from frappe.core.doctype.system_settings.system_settings import clear_system_settings_cache
        clear_system_settings_cache()
        frappe.local.system_settings = None
        self.addCleanup(clear_system_settings_cache)
        frappe.set_user(self.user)
        self.assertTrue(frappe.get_system_settings("apply_strict_user_permissions"))
        rows = get_production_units(company=self.company)
        self.assertIn(self.station.name, [r["name"] for r in rows])
        self.assertNotIn(other.name, [r["name"] for r in get_production_units()])

    def test_ownership_change_reconciles_existing_document_share(self):
        self.station_fixture()
        share = row("DocShare", name=uuid4().hex, share_doctype=self.station.doctype, share_name=self.station.name,
                    user=self.user, read=1, write=1)
        other = row("Branch", name=self.prefix + "-B", company=self.company + "-B")
        self.station.branch = other.name
        self.station.save_device_settings()
        self.assertFalse(frappe.db.exists("DocShare", share.name))
        frappe.set_user(self.user)
        for right in ("read", "write"):
            with self.subTest(right=right), self.assertRaises(frappe.PermissionError):
                self.station.check_permission(right)
        self.assertNotIn(self.station.name, frappe.get_list(self.station.doctype, pluck="name"))

    def test_new_station_requires_owned_branch(self):
        doc = frappe.get_doc({"doctype": "URY Production Unit", "production": self.prefix})
        with self.assertRaisesRegex(frappe.ValidationError, "(?i)branch"):
            doc.validate()
