"""Read back a void across the same commit/rollback boundary as a POST request."""

import unittest
from unittest.mock import patch

import frappe
from frappe.model.document import Document

from ury.ury.doctype.ury_order import ury_order


class TestCancelOrderTransaction(unittest.TestCase):
    def setUp(self):
        # Never commit another test's fixtures when simulating separate requests.
        original = frappe.local.db
        frappe.connect(set_admin_as_user=False)
        self.addCleanup(setattr, frappe.local, "db", original)
        self.addCleanup(frappe.db.close)
        self.table = frappe.new_doc("URY Table")
        self.table.name = "TEST-VOID-TABLE-" + frappe.generate_hash(length=12)
        self.table.occupied = 1
        self.table.db_insert()
        self.invoice = frappe.new_doc("POS Invoice")
        self.invoice.name = "TEST-VOID-INVOICE-" + frappe.generate_hash(length=12)
        self.invoice.restaurant_table = self.table.name
        self.invoice.db_insert()
        self.addCleanup(self.remove_records)
        kot = patch.object(ury_order, "cancel_kot")
        self.kot = kot.start()
        self.addCleanup(kot.stop)
        frappe.db.commit()

    def remove_records(self):
        frappe.db.rollback()
        for doc in (self.invoice, self.table):
            frappe.db.delete(doc.doctype, {"name": doc.name})
        frappe.db.commit()

    def state(self):
        return (
            frappe.db.get_value("URY Table", self.table.name, "occupied"),
            frappe.db.get_value(
                "POS Invoice", self.invoice.name, ["docstatus", "status", "cancel_reason", "modified"]
            ),
        )

    def assert_refusal(self, error):
        before = self.state()
        with self.assertRaises(error):
            ury_order.cancel_order(self.invoice.name, "Duplicate check")
        frappe.db.rollback()
        self.assertEqual(self.state(), before)

    def test_native_write_denial_rolls_back_table_release(self):
        original = Document.has_permission

        def permission(doc, permtype="read", **kwargs):
            if doc.name == self.invoice.name and permtype == "write":
                return False
            return original(doc, permtype, **kwargs)

        with patch.object(Document, "has_permission", permission):
            self.assert_refusal(frappe.PermissionError)

    def test_stale_draft_rolls_back_table_release(self):
        self.kot.side_effect = lambda name: frappe.db.set_value("POS Invoice", name, "modified", "2001-01-01")
        self.assert_refusal(frappe.TimestampMismatchError)

    def test_already_cancelled_leaves_reoccupied_table_untouched(self):
        self.invoice.db_set({"docstatus": 2, "status": "Cancelled", "cancel_reason": "Original reason"})
        frappe.db.commit()
        self.assert_refusal(frappe.ValidationError)
        self.kot.assert_not_called()

    def test_discard_and_reason_commit_with_table_release(self):
        ury_order.cancel_order(self.invoice.name, "Duplicate check")
        frappe.db.commit()
        occupied, (docstatus, status, reason, _) = self.state()
        self.assertEqual((occupied, docstatus, status, reason), (0, 2, "Cancelled", "Duplicate check"))
        self.assertFalse(frappe.db.exists("GL Entry", {"voucher_no": self.invoice.name}))
