"""MariaDB regressions for compensated recall and paid checks at occupied tables."""
import unittest
from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

from ury.ury.api import kitchen_ticket_history as history, ury_kot_display as display
from ury.ury.api import ury_service_line as service
from ury.ury.doctype.ury_order import test_pos_pr9_r21 as r21, ury_order


class TestKitchenService(unittest.TestCase):
    def setUp(self):
        self.fixture = r21.TestReceivingCheck()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def recall(self, owner):
        return {row.name: row.invoice for row in display.recent_served_kots(branch=owner.branch)["kots"]}

    def test_compensation_tokens_match_the_recall_guard(self):
        for currency, places in self.fixture.kitchen.currencies:
            _, owner, ticket = self.fixture.owners(currency, places)
            display.expo_bump_order(owner.name, tickets=[ticket.name])
            self.assertEqual(self.recall(owner)[ticket.name], owner.name)
            ury_order.cancel_kot(owner.name, reason="Guest")
            cancel = frappe.db.get_value("URY KOT", {"original_kot": ticket.name, "type": "Cancelled"})
            self.assertTrue(cancel)
            for padding in ("  ", "\t", "\n", "\r\n", "\u00a0", "\x1c"):
                for exact in (False, True):
                    with self.subTest(currency=currency, whitespace=repr(padding), exact=exact):
                        token = ticket.name if exact else ticket.name + "0"
                        links = f"{padding}, ,KOT-OTHER{padding},{padding}{token}{padding},,"
                        frappe.db.set_value("URY KOT", cancel, "original_kot", links)
                        self.assertEqual(history.is_wholly_cancelled(frappe, ticket.name), exact)
                        sql = history.WHOLE_COMPENSATION.format(ticket="%s")
                        self.assertEqual(bool(frappe.db.sql("SELECT " + sql, (ticket.name,))[0][0]), exact)
                        self.assertEqual(frappe.db.sql("SHOW WARNINGS"), ())
                        self.assertEqual(ticket.name not in self.recall(owner), exact)

    def test_padded_compensations_do_not_fill_recall_page(self):
        for currency, places in self.fixture.kitchen.currencies:
            with self.subTest(currency=currency):
                _, owner, issued = self.fixture.owners(currency, places)
                start = now_datetime() - timedelta(minutes=2)
                served = []
                for index in range(101):
                    copy = frappe.copy_doc(issued)
                    copy.name = f"r24-{currency}-{index}"
                    copy.update(dict(creation=start, modified=start + timedelta(seconds=index), docstatus=1,
                                     validation_dedup_key=None, order_status="Served", production_time=0))
                    copy.db_insert()
                    served.append(copy.name)
                cancellation = frappe.copy_doc(issued)
                cancellation.name = f"r24-{currency}-compensation"
                cancellation.update(dict(docstatus=1, type="Cancelled", validation_dedup_key=None,
                                         original_kot=" ,\r\n  ".join(served[1:])))
                cancellation.db_insert()
                self.assertEqual(list(self.recall(owner)), served[:1])

    def check_paid_table(self, currency, places, *, cancel):
        _, owner, ticket = self.fixture.owners(currency, places)
        seated = get_datetime(owner.creation) - timedelta(minutes=10)
        table = frappe.get_doc(dict(doctype="URY Table", name=currency + "-r24-table", branch=owner.branch,
                                    occupied=1, latest_invoice_time=seated.time()))
        table.db_insert()

        # A prior party left an unbumped ticket at the same table and clock time yesterday.
        prior = frappe.copy_doc(owner)
        prior.name = currency + "-previous-party"
        prior.update(dict(restaurant_table=table.name, docstatus=1, status="Paid",
                          creation=seated - timedelta(days=1), ury_applied_rounds=None))
        prior.db_insert()
        old_ticket = frappe.copy_doc(ticket)
        old_ticket.name = currency + "-previous-release"
        old_ticket.update(dict(invoice=prior.name, docstatus=1, validation_dedup_key=None))
        old_ticket.db_insert()

        cache_key = "ury_dashboard_service_line:" + owner.branch
        self.addCleanup(frappe.cache.delete_value, cache_key)

        def stage():
            frappe.cache.delete_value(cache_key)
            return next(r["stage"] for r in service.get_service_line(owner.branch) if r["table"] == table.name)

        self.assertEqual(stage(), "seated")
        owner.db_set(dict(restaurant_table=table.name, docstatus=1, status="Paid"))
        ury_order.change_table_in_kot(owner.name, table.name, owner.branch)
        # Another check can stay open at the table while the paid check is in the kitchen.
        if cancel:
            sibling = frappe.copy_doc(owner)
            sibling.name = currency + "-current-draft"
            sibling.update(dict(docstatus=0, status="Draft", ury_applied_rounds=None))
            sibling.db_insert()
        self.assertEqual(stage(), "fired")
        if cancel:
            ury_order.cancel_kot(owner.name, reason="Guest")
        else:
            self.assertEqual(display.expo_bump_order(owner.name, tickets=[ticket.name])["served_kots"], [ticket.name])
        self.assertEqual(stage(), "seated")
        self.assertNotEqual(old_ticket.reload().order_status, "Served")

    def test_paid_table_until_bump(self):
        for currency, places in self.fixture.kitchen.currencies:
            with self.subTest(currency=currency):
                self.check_paid_table(currency, places, cancel=False)

    def test_paid_table_until_compensation(self):
        for currency, places in self.fixture.kitchen.currencies:
            with self.subTest(currency=currency):
                self.check_paid_table(currency, places, cancel=True)
