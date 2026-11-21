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
        # v16 make_invoice answers {"name", "status"}; the retry gives the same answer.
        self.assertEqual(second, {"name": order.name, "status": "Submitted"})
        self.assertEqual(frappe.db.get_value("POS Invoice", order.name, "docstatus"), 1)
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
        with self.assertRaisesRegex(frappe.ValidationError, "URY_CLIENT_ORDER_ID_INVALID"):
            send_order(self.pos, client_order_id="not-a-uuid", round_id="1")
        self.assertEqual(frappe.db.count("POS Invoice", {"pos_profile": self.pos.profile.name}), 0)

    def test_empty_round_is_refused_instead_of_silently_dropping_later_rounds(self):
        with self.assertRaisesRegex(frappe.ValidationError, "URY_ROUND_ID_EMPTY"):
            send_order(self.pos, client_order_id=self.client_order_id, round_id="")
        self.assertEqual(frappe.db.count("POS Invoice", {"pos_profile": self.pos.profile.name}), 0)

    def test_sync_cannot_rebind_an_existing_client_order_id(self):
        order = self.send()
        with self.assertRaisesRegex(frappe.ValidationError, "URY_CLIENT_ORDER_ID_REBIND"):
            send_order(
                self.pos, client_order_id=str(uuid4()), round_id="2", invoice=order.name,
                last_invoice=order.name, last_modified_time=str(order.modified),
            )
        self.assertEqual(frappe.db.get_value("POS Invoice", order.name, "ury_client_order_id"), self.client_order_id)

    def test_settle_cannot_rebind_an_existing_client_order_id(self):
        order = self.send()
        with self.assertRaisesRegex(frappe.ValidationError, "URY_CLIENT_ORDER_ID_REBIND"):
            settle_order(self.pos, order.name, client_order_id=str(uuid4()), round_id="2")
        saved = frappe.get_doc("POS Invoice", order.name)
        self.assertEqual(saved.ury_client_order_id, self.client_order_id)
        self.assertEqual(saved.docstatus, 0)

    def test_client_id_cannot_target_a_different_explicit_invoice(self):
        first = self.send()
        second = send_order(self.pos, client_order_id=str(uuid4()), round_id="1")
        with self.assertRaisesRegex(frappe.ValidationError, "URY_CLIENT_ORDER_ID_MISMATCH"):
            send_order(self.pos, client_order_id=self.client_order_id, invoice=second.name,
                       last_invoice=second.name, last_modified_time=str(second.modified), round_id="2")
        self.assertEqual(frappe.db.get_value("POS Invoice", first.name, "ury_last_round_id"), "1")

    def test_settle_client_id_cannot_target_a_different_explicit_invoice(self):
        first = self.send()
        second = send_order(self.pos, client_order_id=str(uuid4()), round_id="1")
        with self.assertRaisesRegex(frappe.ValidationError, "URY_CLIENT_ORDER_ID_MISMATCH"):
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
        with self.assertRaisesRegex(frappe.ValidationError, "URY_CLIENT_ORDER_ID_MISMATCH"):
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

    def test_retry_of_an_earlier_append_round_returns_saved_order_without_reapplying(self):
        order = self.send()
        for round_id in ("2", "3"):
            send_order(self.pos, client_order_id=self.client_order_id, round_id=round_id, append=1,
                       invoice=order.name, last_invoice=order.name, last_modified_time=str(order.modified))
        late = send_order(self.pos, client_order_id=self.client_order_id, round_id="2", append=1,
                          invoice=order.name, last_invoice=order.name, last_modified_time=str(order.modified))
        self.assertEqual(late.get("name"), order.name)
        self.assertEqual([row.qty for row in frappe.get_doc("POS Invoice", order.name).items], [1, 1, 1])
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": order.name}), 3)
        self.assertEqual(frappe.db.get_value("POS Invoice", order.name, "ury_last_round_id"), "3")

    def test_retry_of_the_first_round_after_later_rounds_returns_saved_order(self):
        order = self.send()
        send_order(self.pos, client_order_id=self.client_order_id, round_id="2", append=1,
                   invoice=order.name, last_invoice=order.name, last_modified_time=str(order.modified))
        late = self.send()
        self.assertEqual(late.get("name"), order.name)
        self.assertEqual([row.qty for row in frappe.get_doc("POS Invoice", order.name).items], [1, 1])
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": order.name}), 2)

    def test_late_retry_of_an_order_round_after_settlement_returns_saved_order(self):
        order = self.send()
        settle_order(self.pos, order.name, client_order_id=self.client_order_id, round_id="2")
        late = self.send()
        self.assertEqual(late.get("name"), order.name)
        self.assertEqual(frappe.db.get_value("POS Invoice", order.name, "docstatus"), 1)
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": order.name}), 1)

    def test_settle_retry_with_client_order_id_alone_returns_submitted_invoice(self):
        order = self.send()
        settle_order(self.pos, order.name, client_order_id=self.client_order_id, round_id="2")
        retry = settle_order(self.pos, order.name, client_order_id=self.client_order_id)
        self.assertEqual(retry, {"name": order.name, "status": "Submitted"})
        self.assertEqual(frappe.db.count("POS Invoice", {"pos_profile": self.pos.profile.name}), 1)

    def test_same_round_with_different_items_is_refused_with_stable_id(self):
        order = self.send()
        with self.assertRaisesRegex(frappe.ValidationError, "URY_ROUND_PAYLOAD_MISMATCH"):
            self.send(items=[{"item": self.pos.item.name, "item_name": self.pos.item.item_name, "qty": 5}])
        self.assertEqual([row.qty for row in frappe.get_doc("POS Invoice", order.name).items], [1])
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": order.name}), 1)

    def test_same_round_retry_ignores_client_rate_and_line_order_noise(self):
        order = self.send()
        retry = self.send(items=[{"item": self.pos.item.name, "item_name": "renamed", "qty": "1", "rate": 999}])
        self.assertEqual(retry.get("name"), order.name)

    def test_concurrent_first_send_collision_is_refused_with_stable_id(self):
        from ury.ury.doctype.ury_order import ury_order

        real = ury_order._client_order_invoice
        calls = []

        def miss_once(*args, **kwargs):
            # The racing worker's lookup ran before the winner committed.
            calls.append(1)
            return None if len(calls) == 1 else real(*args, **kwargs)

        self.send()
        with patch.object(ury_order, "_client_order_invoice", miss_once), self.assertRaisesRegex(
            frappe.ValidationError, "URY_CLIENT_ORDER_ID_IN_USE"
        ):
            self.send()
        self.assertEqual(frappe.db.count("POS Invoice", {"pos_profile": self.pos.profile.name}), 1)

    def test_settled_client_order_cannot_take_a_new_round(self):
        order = self.send()
        settle_order(self.pos, order.name, client_order_id=self.client_order_id, round_id="2")
        with self.assertRaisesRegex(frappe.ValidationError, "URY_ORDER_NOT_OPEN"):
            send_order(self.pos, client_order_id=self.client_order_id, round_id="3")
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": order.name}), 1)

    def test_refusals_are_validation_errors_not_authorization_denials(self):
        for kwargs in ({"client_order_id": "not-a-uuid", "round_id": "1"},
                       {"client_order_id": self.client_order_id, "round_id": " "}):
            with self.subTest(kwargs=kwargs):
                try:
                    send_order(self.pos, **kwargs)
                except frappe.PermissionError:
                    self.fail("A malformed id is a validation error, not an authorization denial")
                except frappe.ValidationError:
                    pass
                else:
                    self.fail("A malformed id must be refused")

    def test_split_group_cashier_settles_the_original_with_its_own_client_id(self):
        from ury.ury.doctype.ury_order.ury_order import split_bill
        from ury.ury_pos.api import get_split_group

        dish = {"item": self.pos.item.name, "item_name": self.pos.item.item_name, "qty": 2}
        original = self.send(items=[dish])
        child = split_bill(original.name, [{"name": original["items"][0].name, "qty": 1}])["new_invoice"]
        checks = {row.name: row for row in get_split_group(child)["invoices"]}
        with self.assertRaisesRegex(frappe.ValidationError, "URY_CLIENT_ORDER_ID_REBIND"):
            settle_order(self.pos, original.name, client_order_id=str(uuid4()))
        self.assertEqual(checks[original.name].get("ury_client_order_id"), self.client_order_id)
        self.assertFalse(checks[child].get("ury_client_order_id"))
        result = settle_order(self.pos, original.name, client_order_id=checks[original.name].ury_client_order_id)
        self.assertEqual(result, {"name": original.name, "status": "Submitted"})
        paid = frappe.get_doc("POS Invoice", original.name)
        self.assertEqual((paid.docstatus, paid.grand_total, paid.outstanding_amount), (1, 100, 0))
        self.assertEqual(frappe.db.get_value("POS Invoice", child, "docstatus"), 0)


class TestOfflineRefusalVocabulary(IntegrationTestCase):
    def test_offline_refusal_ids_are_a_closed_append_only_set(self):
        from ury.ury.doctype.ury_order import ury_order

        self.assertEqual(
            tuple(getattr(ury_order, "OFFLINE_REFUSAL_IDS", ())),
            (
                "URY_CLIENT_ORDER_ID_INVALID",
                "URY_ROUND_ID_EMPTY",
                "URY_CLIENT_ORDER_ID_REBIND",
                "URY_CLIENT_ORDER_ID_MISMATCH",
                "URY_ORDER_NOT_OPEN",
                "URY_OFFLINE_OUTSIDE_SHIFT",
                "URY_OFFLINE_TOTAL_MISMATCH",
                "URY_OFFLINE_APPEND_QTY",
                "URY_OFFLINE_CAPTURE_DISABLED",
                "URY_ROUND_PAYLOAD_MISMATCH",
                "URY_CLIENT_ORDER_ID_IN_USE",
            ),
        )
