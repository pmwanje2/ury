"""Recover a continuously occupied table and conserve exact minor-unit split shares."""
import unittest
from datetime import timedelta
from decimal import Decimal

import frappe
from frappe.utils import now_datetime

from ury.ury.api import table_lifecycle as floor
from ury.ury.doctype.ury_order import test_pos_pr9_r27 as r27, ury_order
from ury.ury_pos.split_balances import allocate_split_balances


class TestUninterruptedParty(unittest.TestCase):
    def setUp(self):
        self.fx = r27.TestReleasedAndRecoveredSeatings()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def recover_after_prints(self, linked):
        for currency, places in self.fx.currencies:
            with self.subTest(currency=currency):
                a, ticket, _, table = self.fx.case.seating(currency, places)
                frappe.db.set_value("Branch", a.branch, {
                    "custom_table_release_trigger": "On Bill Print (Legacy)", "custom_enable_bussing_state": 0,
                })
                now = now_datetime()
                old = self.fx.draft(a, "-departed", now - timedelta(hours=1), restaurant_table=table.name,
                                    invoice_printed=1, custom_printing_time=now - timedelta(minutes=30))
                self.assertTrue(ury_order.release_tables_after_print(old.name))
                a.db_set(dict(restaurant_table=table.name, creation=now))
                ury_order.change_table_in_kot(a.name, table.name, a.branch)
                floor.set_table_state(table.name, "Seated", check=a.name)
                b = self.fx.draft(a, "-middle", now_datetime(), custom_split_from=a.name if linked else None)
                kitchen = self.fx.case.fx.kitchen
                b = kitchen.configure(b, places, [dict(item_code="Tea"), dict(item_code="Bread")])

                a.db_set(dict(invoice_printed=1, custom_printing_time=now_datetime()))
                self.assertTrue(ury_order.release_tables_after_print(a.name))
                self.assertEqual(table.reload().occupied, 1)
                c = kitchen.split(b, [next(row for row in b.items if row.item_code == "Bread")])
                self.assertEqual(c.custom_split_from, a.name if linked else b.name)
                b.db_set(dict(invoice_printed=1, custom_printing_time=now_datetime()))
                self.assertTrue(ury_order.release_tables_after_print(b.name))
                self.assertEqual(table.reload().occupied, 1)

                frappe.db.delete("Version", {"ref_doctype": "URY Table", "docname": table.name})
                self.assertEqual(self.fx.recover(table), {a.name, b.name, c.name})
                self.assertEqual(self.fx.case.stage(table), "fired")
                self.assertNotEqual(ticket.reload().order_status, "Served")

    def test_split_linked_printed_siblings(self):
        self.recover_after_prints(linked=True)

    def test_overlapping_unlinked_draft_bridges_two_prints(self):
        self.recover_after_prints(linked=False)


class TestRationalAllocation(unittest.TestCase):
    def test_stored_line_amounts_and_remainders(self):
        frappe.db.savepoint("r32_shares")
        self.addCleanup(lambda: frappe.db.rollback(save_point="r32_shares"))
        for currency, places in (("UGX", 0), ("USD", 2), ("KWD", 3)):
            unit = Decimal(1).scaleb(-places)
            # (parent quantity, moved, linked quantity, modifier minor units, expected modifier share)
            for qty, moved, linked_qty, minor, share in (
                (14, 5, 7, 21, 8), (14, 5, 7, -21, -8), (3, 1, 3, 5, 2), (7, 2, 7, 5, 1),
                (Decimal("10000.0001"), 5000, Decimal("10000.0001"), 200000003, 100000000),
            ):
                with self.subTest(currency=currency, qty=qty, minor=minor):
                    rate = int(qty < 100)
                    total = qty * rate + minor * unit
                    doc = frappe.get_doc(dict(doctype="POS Invoice", currency=currency, name=frappe.generate_hash(),
                                              total=total, net_total=total, grand_total=total, conversion_rate=1))
                    doc.db_insert()
                    for name, count, amount, parent in (("dish", qty, qty * rate, None),
                                                        ("side", linked_qty, minor * unit, "dish")):
                        doc.append("items", dict(item_code=name, qty=count, rate=amount / Decimal(count),
                                                 amount=amount, net_amount=amount, reservation_line_key=name,
                                                 custom_parent_line_key=parent, custom_is_modifier=int(bool(parent)))).db_insert()
                    doc.reload()
                    dish, side = doc.items
                    # Shareable fractional UOM for the large near-half control.
                    if qty != int(qty) and not frappe.db.exists("Item", "dish"):
                        frappe.get_doc(dict(doctype="Item", name="dish", item_code="dish", custom_is_shareable=1)).db_insert()
                    plan, = ury_order._plan_split_checks(doc, [[dict(name=dish.name, qty=moved)]], "manual", None)
                    rest, child = allocate_split_balances(doc, plan, places, places)
                    expected = moved * rate + share * unit
                    self.assertEqual(Decimal(str(child["items"][side.name]["amount"])), share * unit)
                    self.assertEqual(Decimal(str(child["header"]["grand_total"])), expected)
                    self.assertEqual(Decimal(str(rest["header"]["grand_total"])), total - expected)
