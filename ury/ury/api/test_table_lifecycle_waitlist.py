import importlib
import unittest
from unittest.mock import Mock, patch

import frappe

from ury.ury.api import table_reservation
from ury.ury.doctype.ury_table_reservation.ury_table_reservation import URYTableReservation


def throw(message, exc=frappe.ValidationError, **kwargs):
    raise exc(message)


class TableLifecycleWaitlistTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(frappe, "throw", throw))
        self.enterContext(patch.object(frappe, "db", Mock()))
        self.enterContext(patch("ury.ury.doctype.ury_table_reservation.ury_table_reservation._", lambda value: value))

    def lifecycle(self):
        module = importlib.import_module("ury.ury.api.table_lifecycle")
        self.enterContext(patch.object(module, "_", lambda value: value))
        return module

    def test_print_keeps_table_occupied_until_last_draft_closes_and_then_marks_dirty(self):
        module = self.lifecycle()
        config = frappe._dict(
            custom_table_release_trigger=module.ON_SETTLEMENT, custom_enable_bussing_state=1,
        )
        write = Mock()
        with (
            patch.object(module, "table_policy", return_value=config),
            patch.object(module, "open_checks_on", return_value=[frappe._dict(invoice_printed=1)]) as checks,
            patch.object(module, "set_table_state", write),
        ):
            module.update_table_after_check(["T1"], "B", printed=True)
            write.assert_called_once_with("T1", "Check Printed")
            write.reset_mock()
            checks.return_value = []
            module.update_table_after_check(["T1"], "B")
            write.assert_called_once_with("T1", "Dirty")

    def test_dirty_table_can_block_seating(self):
        module = self.lifecycle()
        with (
            patch.object(module, "table_policy", return_value=frappe._dict(custom_block_dirty_table_seating=1)),
            patch.object(frappe.db, "get_value", return_value="Dirty", create=True),
        ):
            with self.assertRaisesRegex(frappe.ValidationError, "TABLE-DIRTY-NEEDS-BUSSING"):
                module.assert_table_seatable("T1", "B")

    def test_clean_requires_outlet_authorization(self):
        module = self.lifecycle()
        with (
            patch.object(module, "assert_outlet_access", side_effect=frappe.PermissionError),
            patch.object(frappe.db, "get_value", return_value="Other", create=True),
            patch.object(module, "set_table_state") as write,
        ):
            with self.assertRaises(frappe.PermissionError):
                module.mark_table_clean("T1")
            write.assert_not_called()

    def test_only_walkins_can_omit_table(self):
        doc = Mock(spec=URYTableReservation)
        doc.customer, doc.customer_phone, doc.no_of_pax = "Guest", "555", 2
        doc.reserved_table, doc.reserved_at, doc.status = None, "2026-11-25", "Waiting"
        doc.is_waitlist, doc.quoted_wait_minutes = 1, 10
        URYTableReservation.validate_mandatory_fields(doc)
        doc.is_waitlist = 0
        with self.assertRaises(frappe.ValidationError):
            URYTableReservation.validate_mandatory_fields(doc)

    def test_waitlist_room_cannot_bypass_outlet_authorization(self):
        with patch.object(table_reservation, "assert_outlet_access", side_effect=frappe.PermissionError, create=True):
            with self.assertRaises(frappe.PermissionError):
                table_reservation.estimate_wait_time("Another Outlet", 2, "Private Room")

    def test_waitlist_cannot_seat_on_dirty_table(self):
        with (
            patch.object(table_reservation, "assert_outlet_access", create=True),
            patch.object(frappe, "get_doc", side_effect=[
                frappe._dict(is_waitlist=1, branch="B", status="Waiting", no_of_pax=2),
                frappe._dict(name="T1", branch="B", custom_table_state="Dirty", occupied=0),
            ]),
            patch.object(table_reservation, "_", lambda value: value),
        ):
            with self.assertRaisesRegex(frappe.ValidationError, "TABLE-DIRTY-NEEDS-BUSSING"):
                table_reservation.seat_waitlist_party("WAIT-1", "T1")
