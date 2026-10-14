"""Retries must exercise the real URY/ERPNext write paths and database."""

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from ury.tests.offline_pos_utils import make_offline_pos, send_order, settle_order


class TestIdempotentWrites(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.pos = make_offline_pos()
        self.client_order_id = str(uuid4())

    def tearDown(self):
        frappe.set_user("Administrator")
        super().tearDown()

    def send(self, **kwargs):
        return send_order(self.pos, client_order_id=self.client_order_id, round_id="1", **kwargs)

    def test_double_send_saves_one_invoice_and_one_kot(self):
        first = self.send()
        second = self.send()
        invoices = frappe.get_all("POS Invoice", filters={"pos_profile": self.pos.profile.name}, pluck="name")
        kot_count = frappe.db.count("URY KOT", {"pos_profile": self.pos.profile.name, "docstatus": 1})
        self.assertEqual((len(invoices), kot_count), (1, 1), "A retried round must save one invoice and one KOT")
        self.assertEqual(second["name"], first["name"])
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": first["name"], "docstatus": 1}), 1)
        self.assertEqual(frappe.db.get_value("POS Invoice", first["name"], "ury_last_round_id"), "1")

    def test_double_settle_submits_once_and_returns_saved_invoice(self):
        order = self.send()
        from erpnext.accounts.doctype.pos_invoice.pos_invoice import POSInvoice

        original = POSInvoice.on_submit
        submissions = []

        def record_submit(doc):
            original(doc)
            submissions.append(doc.name)

        with patch.object(POSInvoice, "on_submit", record_submit):
            first = settle_order(self.pos, order.name, client_order_id=self.client_order_id, round_id="2")
            try:
                second = settle_order(self.pos, order.name, client_order_id=self.client_order_id, round_id="2")
            except Exception as error:
                self.fail(f"A settled retry must return the saved invoice, not attempt a new settlement: {error}")
        self.assertEqual(submissions, [order.name])
        self.assertIsNotNone(first, "Settlement must return the persisted invoice")
        self.assertEqual(first["name"], second["name"])
        self.assertEqual(second["docstatus"], 1)
        self.assertEqual(frappe.db.count("POS Invoice", {"pos_profile": self.pos.profile.name}), 1)

    def test_database_unique_index_rejects_another_new_order_with_same_id(self):
        order = self.send()
        field = frappe.get_meta("POS Invoice").get_field("ury_client_order_id")
        self.assertIsNotNone(field, "URY must install the unique native client-order field")
        self.assertTrue(field.unique)
        self.assertTrue(field.no_copy)
        self.assertTrue(field.read_only)
        self.assertTrue(field.hidden)
        duplicate = frappe.copy_doc(frappe.get_doc("POS Invoice", order.name))
        duplicate.ury_client_order_id = self.client_order_id
        with self.assertRaises(frappe.UniqueValidationError):
            duplicate.insert()

    def test_invalid_client_order_uuid_is_refused_before_creating_any_invoice(self):
        with self.assertRaises(frappe.ValidationError):
            send_order(self.pos, client_order_id="not-a-uuid", round_id="1")
        self.assertEqual(frappe.db.count("POS Invoice", {"pos_profile": self.pos.profile.name}), 0)

    def test_empty_round_is_refused_instead_of_silently_dropping_later_rounds(self):
        with self.assertRaises(frappe.ValidationError):
            send_order(self.pos, client_order_id=self.client_order_id, round_id="")
        self.assertEqual(frappe.db.count("POS Invoice", {"pos_profile": self.pos.profile.name}), 0)

    def test_sync_cannot_rebind_an_existing_client_order_id(self):
        order = self.send()
        with self.assertRaises(frappe.ValidationError):
            send_order(
                self.pos, client_order_id=str(uuid4()), round_id="2", invoice=order.name,
                last_invoice=order.name, last_modified_time=str(order.modified),
            )
        self.assertEqual(frappe.db.get_value("POS Invoice", order.name, "ury_client_order_id"), self.client_order_id)

    def test_settle_cannot_rebind_an_existing_client_order_id(self):
        order = self.send()
        with self.assertRaises(frappe.ValidationError):
            settle_order(self.pos, order.name, client_order_id=str(uuid4()), round_id="2")
        saved = frappe.get_doc("POS Invoice", order.name)
        self.assertEqual(saved.ury_client_order_id, self.client_order_id)
        self.assertEqual(saved.docstatus, 0)

    def test_client_id_cannot_target_a_different_explicit_invoice(self):
        first = self.send()
        second = send_order(self.pos, client_order_id=str(uuid4()), round_id="1")
        with self.assertRaises(frappe.ValidationError):
            send_order(self.pos, client_order_id=self.client_order_id, invoice=second.name,
                       last_invoice=second.name, last_modified_time=str(second.modified), round_id="2")
        self.assertEqual(frappe.db.get_value("POS Invoice", first.name, "ury_last_round_id"), "1")

    def test_settle_client_id_cannot_target_a_different_explicit_invoice(self):
        first = self.send()
        second = send_order(self.pos, client_order_id=str(uuid4()), round_id="1")
        with self.assertRaises(frappe.ValidationError):
            settle_order(self.pos, second.name, client_order_id=self.client_order_id, round_id="2")
        self.assertEqual(frappe.db.get_value("POS Invoice", first.name, "docstatus"), 0)
        self.assertEqual(frappe.db.get_value("POS Invoice", second.name, "docstatus"), 0)

    def test_client_id_cannot_target_a_different_table(self):
        frappe.set_user("Administrator")
        first_table = frappe.get_doc({
            "doctype": "URY Table", "restaurant": self.pos.restaurant.name,
            "restaurant_room": self.pos.room.name, "branch": self.pos.branch.name,
        }).insert()
        second_table = frappe.get_doc({
            "doctype": "URY Table", "restaurant": self.pos.restaurant.name,
            "restaurant_room": self.pos.room.name, "branch": self.pos.branch.name,
        }).insert()
        frappe.set_user(self.pos.user.name)
        self.send(table=first_table.name, order_type="Dine In")
        with self.assertRaises(frappe.ValidationError):
            send_order(self.pos, client_order_id=self.client_order_id, table=second_table.name,
                       order_type="Dine In", round_id="2")

    def test_retry_keeps_existing_active_branch_permission_check(self):
        order = self.send()
        make_offline_pos()  # Another real cashier, assigned to another branch.
        with self.assertRaises(frappe.PermissionError):
            self.send(invoice=order.name, last_invoice=order.name,
                      last_modified_time=str(order.modified))

    def test_next_round_updates_existing_order_not_a_new_invoice(self):
        order = self.send()
        updated = send_order(
            self.pos, client_order_id=self.client_order_id, round_id="2",
            invoice=order.name, last_invoice=order.name, last_modified_time=str(order.modified),
            items=[{"item": self.pos.item.name, "item_name": self.pos.item.item_name, "qty": 2}],
        )
        retry = send_order(
            self.pos, client_order_id=self.client_order_id, round_id="2",
            invoice=order.name, last_invoice=order.name, last_modified_time=str(order.modified),
            items=[{"item": self.pos.item.name, "item_name": self.pos.item.item_name, "qty": 2}],
        )
        self.assertEqual(updated["name"], order["name"])
        self.assertEqual(retry.get("name"), order["name"], "A retry must return the saved round despite its stale timestamp")
        self.assertEqual(frappe.db.get_value("POS Invoice", order["name"], "ury_last_round_id"), "2")
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": order["name"]}), 2)
