"""Issued KOT ownership through Add Check and manager-authorized line voids."""

import unittest
from datetime import datetime
from unittest.mock import patch

import frappe

from ury.ury.api import ury_kot_generate as kitchen, ury_pos_invoice_qty_reduction as reduction
from ury.ury.doctype.ury_order import test_pos_pr9_r15 as r15, ury_order as order


class TestIssuedSoupTickets(unittest.TestCase):
    def setUp(self):
        self.fx = r15.TestAllocatedVoids()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        if not frappe.db.exists("Item", "Soup"):
            frappe.get_doc(dict(doctype="Item", name="Soup", item_code="Soup")).db_insert()
        self.enterContext(patch("ury.ury.api.ury_waiter_print.print_combined_waiter_order_slip"))

    def checks(self, currency, places, together):
        first, second = self.fx.checks.fixture.priced_checks(currency, places, siblings=True)
        branch, profile = currency + "-R16-branch", currency + "-R16-till"
        frappe.get_doc(dict(doctype="Branch", name=branch, branch=branch, company=first.company)).db_insert()
        frappe.get_doc(dict(doctype="POS Profile", name=profile, branch=branch, company=first.company,
                            custom_kot_naming_series="KOT-.####")).db_insert()
        station = currency + "-R16-kitchen"
        frappe.get_doc(dict(doctype="URY Production Unit", name=station, branch=branch)).db_insert()
        self.enterContext(patch.object(kitchen, "_resolve_units_for_item_code", return_value=[station]))
        for doc, minute in ((first, 0), (second, 30)):
            doc.branch, doc.pos_profile = branch, profile
            doc.order_type = "Dine In"
            doc.items[0].update(dict(ury_held=1, reservation_line_key=f"{currency}-soup-{minute}",
                                     fire_at=datetime(2026, 11, 25, 19, minute)))
        tea = frappe.copy_doc(first)
        tea.name = currency + "-tea"
        tea.docstatus = 0
        tea.custom_split_balance = tea.custom_split_group = None
        tea.set("items", [])
        tea.append("items", dict(name=tea.name + "-item", item_code="Tea", qty=1, rate=15,
                                 price_list_rate=15, conversion_factor=1))
        tea.set_paid_amount = tea.set_outstanding_amount = lambda: None
        tea.calculate_taxes_and_totals()
        if together:
            self.fx.checks.merge(first, second)
            soups = [self.fx.checks.reload(first)]
        else:
            soups = [self.fx.checks.reload(doc) for doc in (first, second)]
        return soups, self.fx.checks.reload(tea)

    def fire_soups(self, soups):
        issued = {}
        for doc in soups:
            doc.save = lambda invoice=doc, **kwargs: self.fx.floor.persist(invoice)
            with patch.object(order, "_staff_held_order", return_value=(doc, None)):
                for row in doc.items:
                    order.fire_items(doc.name, line=row.name)
                    key = "ref:Soup:" + row.reservation_line_key
                    line, = frappe.get_all("URY KOT Items", filters={"reservation_line_key": key,
                                           "docstatus": 1}, fields=["parent", "quantity"])
                    self.assertEqual(line.quantity, 1)
                    issued[key] = line.parent
        self.assertEqual(len(set(issued.values())), 2)
        return issued

    def round_trip(self, layout):
        for currency, places in self.fx.checks.fixture.currencies:
            with self.subTest(currency=currency):
                soups, tea = self.checks(currency, places, together=layout != "one each")
                issued = self.fire_soups(soups)
                if layout == "source":
                    destination, sources = tea, soups
                else:
                    destination, sources = soups[0], [tea, *soups[1:]]
                for source in sources:
                    self.fx.checks.merge(destination, source)
                self.fx.floor.persist(destination)
                doc = destination.reload()
                self.assertEqual(doc.grand_total, 215)
                rows = [row for row in doc.items if row.item_code == "Soup"]
                merged_keys = [(row.reservation_line_key, row.qty) for row in rows]
                cancelled = []
                reason = dict(code="Guest", display="Guest", note="", is_waste=False)
                with (patch.object(reduction, "resolve_pos_reason", return_value=reason),
                      patch.object(reduction, "require_pos_override", return_value="Manager")):
                    for row in rows:
                        result = self.fx.void(doc, row.name)
                        name, = result["cancel_kot_names"]
                        ticket = frappe.get_doc("URY KOT", name)
                        line, = ticket.kot_items
                        self.assertEqual((ticket.docstatus, ticket.invoice), (1, doc.name))
                        cancelled.append((ticket.original_kot, line.reservation_line_key, line.cancelled_qty))
                        doc.reload()
                self.assertCountEqual(cancelled, [(ticket, key, 1) for key, ticket in issued.items()])
                self.assertCountEqual(merged_keys, [(currency + "-soup-0", 1), (currency + "-soup-30", 1)])
                self.assertEqual((doc.grand_total, [r.item_code for r in doc.items]), (15, ["Tea"]))

    def test_two_fired_rows_on_source(self):
        self.round_trip("source")

    def test_two_fired_rows_on_destination(self):
        self.round_trip("destination")

    def test_one_fired_row_on_each_side(self):
        self.round_trip("one each")

    def test_interchangeable_siblings_still_combine(self):
        cases = (("USD", 2, 1, "first", "second"), ("KWD", 3, 0, "shared", "shared"))
        for currency, places, held, first_key, second_key in cases:
            with self.subTest(held=held):
                first, second = self.fx.checks.fixture.priced_checks(
                    currency, places, siblings=True,
                )
                for doc, key in ((first, first_key), (second, second_key)):
                    doc.items[0].update(dict(ury_held=held, reservation_line_key=key))
                self.fx.checks.merge(first, second)
                self.assertEqual([(row.reservation_line_key, row.qty) for row in first.items], [(first_key, 2)])
