"""Split tender defaults and transferred kitchen work, exercised on persisted invoices."""
import unittest
from unittest.mock import Mock, patch

import frappe

from ury.ury.ai_tools import ury_ops_tools as ops
from ury.ury.api import ury_kot_display as display, ury_kot_reprint as reprints
from ury.ury.api import ury_order_kot_status as status, ury_profitability_posted_cost as costs
from ury.ury.api import ury_service_line as service
from ury.ury.doctype.ury_order import test_pos_pr9_r19 as r19, ury_order as order


class TestReceivingCheck(unittest.TestCase):
    def setUp(self):
        self.audit = r19.TestKitchenAudit()
        self.audit.setUp()
        self.addCleanup(self.audit.doCleanups)
        self.fx = self.audit.fx
        self.kitchen = self.audit.kitchen

    def owners(self, currency, places):
        issuer, middle = self.fx.prepare(currency, places)
        final = self.fx.tea(issuer)
        ticket = self.fx.fire(issuer)
        middle = self.fx.merge(middle, issuer)
        final = self.fx.merge(final, middle)
        return issuer, final, ticket

    def test_native_split_paid_amount(self):
        for currency, places in self.kitchen.currencies:
            with self.subTest(currency=currency):
                source, _ = self.fx.prepare(currency, places, qty=2)
                child = frappe.get_doc(dict(doctype="POS Invoice", name=currency + "-r21-child",
                                            disable_rounded_total=1, ignore_pricing_rule=1))
                child.set_missing_values = lambda: child.append("payments", {"mode_of_payment": "Cash"})
                source.set_missing_values = source.run_method = child.run_method = Mock()
                def insert_child():
                    self.fx.checks.reload(child)
                    child.update_child_table("payments")

                child.insert = insert_child
                source.save = lambda: self.fx.persist(source)
                with patch.object(frappe, "new_doc", return_value=child), patch.object(
                        order, "_load_check_for_split", return_value=source), patch.object(
                        order, "_authorize_check_split", return_value=source.company):
                    result = order.split_bill(source.name, [{"name": source.items[0].name, "qty": 1}])
                self.assertEqual(result["new_invoice"], child.name)
                self.assertEqual((child.payments[0].amount, child.payments[0].base_amount, child.paid_amount), (0, 0, 0))
                self.assertEqual((source.reload().grand_total, child.reload().grand_total), (100, 100))

    def partial(self, currency, places, whole):
        doc, _ = self.fx.prepare(currency, places)
        uom = currency + "-r21-unit"
        frappe.get_doc(dict(doctype="UOM", name=uom, uom_name=uom, must_be_whole_number=whole)).db_insert()
        rows = [dict(item_code="Salad", qty=2, reservation_line_key="dish")]
        for code, key, parent, qty in (("Dressing", "side", "dish", 1), ("Oil", "oil", "side", 2)):
            rows.append(dict(item_code=code, uom=uom, qty=qty, rate=2, price_list_rate=2,
                             reservation_line_key=key, custom_parent_line_key=parent,
                             custom_line_role="Modifier", custom_is_modifier=1))
        doc = self.kitchen.configure(doc, places, rows)
        ticket, = self.kitchen.issue(doc, doc.items)
        if whole:
            with self.assertRaises(frappe.ValidationError):
                self.kitchen.void(doc, doc.items[0], 1)
            self.assertEqual([r.qty for r in doc.reload().items], [2, 1, 2])
            self.assertFalse(frappe.db.exists("URY KOT", {"original_kot": ticket}))
        else:
            cancellations = self.kitchen.void(doc, doc.items[0], 1)
            self.assertEqual([r.qty for r in doc.items], [1, 0.5, 1])
            self.assertEqual(doc.grand_total, 103)
            self.assertCountEqual([(c.original_kot, r.item, r.cancelled_qty)
                                   for c in cancellations for r in c.kot_items],
                                  [(ticket, "Salad", 1), (ticket, "Dressing", 0.5), (ticket, "Oil", 1)])

    def test_partial_nested_modifiers(self):
        for currency, places in self.kitchen.currencies:
            with self.subTest(currency=currency):
                self.partial(currency, places, False)

    def test_whole_modifier_rejects_partial_atomically(self):
        for currency, places in self.kitchen.currencies:
            with self.subTest(currency=currency):
                self.partial(currency, places, True)

    def test_full_cancel_is_compensating(self):
        for currency, places in self.kitchen.currencies:
            with self.subTest(currency=currency):
                _, owner, ticket = self.owners(currency, places)
                snapshot = self.audit.snapshot([ticket.name])
                order.cancel_kot(owner.name, reason="Guest")
                self.assertTrue(frappe.db.exists("URY KOT", dict(original_kot=ticket.name, type="Cancelled", docstatus=1)))
                self.assertEqual(self.audit.snapshot([ticket.name]), snapshot)
                self.assertFalse(status.get_order_kot_status(owner.name)["kots"])
                self.assertFalse(display.expo_bump_order(owner.name, tickets=[ticket.name])["served_kots"])

    def test_expo_and_bump_after_two_transfers(self):
        for currency, places in self.kitchen.currencies:
            with self.subTest(currency=currency):
                _, owner, ticket = self.owners(currency, places)
                with patch.object(display, "_is_expo_unit", return_value=True):
                    orders = display.kot_list()["expo_orders"]
                self.assertEqual([row["invoice"] for row in orders if ticket.name in row["active_kot_names"]], [owner.name])
                self.assertEqual(display.expo_bump_order(owner.name, tickets=[ticket.name])["served_kots"], [ticket.name])
                self.assertEqual(ticket.reload().order_status, "Served")

    def test_rush_table_and_service_stage(self):
        for currency, places in self.kitchen.currencies:
            with self.subTest(currency=currency):
                _, owner, ticket = self.owners(currency, places)
                table = currency + "-r21-table"
                frappe.get_doc(dict(doctype="URY Table", name=table, branch=owner.branch, occupied=1)).db_insert()
                owner.db_set("restaurant_table", table)
                order.change_table_in_kot(owner.name, table, owner.branch)
                result = display.set_check_priority(owner.name, is_rush=1)
                key = "ury_dashboard_service_line:" + owner.branch
                frappe.cache.delete_value(key)
                self.addCleanup(frappe.cache.delete_value, key)
                stages = {row["table"]: row["stage"] for row in service.get_service_line(owner.branch)}
                ticket.reload()
                self.assertEqual((result["updated_kots"], ticket.custom_is_rush, ticket.restaurant_table, stages[table]),
                                 ([ticket.name], 1, table, "fired"))

    def test_reprint_only_as_effective_owner(self):
        for currency, places in self.kitchen.currencies:
            with self.subTest(currency=currency):
                issuer, owner, ticket = self.owners(currency, places)
                frappe.db.set_value("POS Profile", owner.pos_profile, dict(custom_enable_kot_reprint=1,
                    custom_reprint_kot_format="Standard", custom_table_order_printer="Kitchen",
                    custom_parcel_order_printer="Kitchen"))
                issuer.db_set("docstatus", 2)
                with patch.object(reprints, "getBranch", return_value=owner.branch), patch.object(
                        reprints, "_order_ownership_flags", return_value={"is_mine": True}), patch.object(
                        reprints, "print_by_server") as dispatch:
                    with self.assertRaises(frappe.ValidationError):
                        reprints.reprint_kot(issuer.name)
                    dispatch.assert_not_called()
                    self.assertEqual(reprints.reprint_kot(owner.name), "Success")
                    self.assertEqual([call.args[:2] for call in dispatch.call_args_list], [("URY KOT", ticket.name)])

    def test_posted_cost_and_stock_trace_before_and_after_payment(self):
        for currency, places in self.kitchen.currencies:
            with self.subTest(currency=currency):
                issuer, owner, ticket = self.owners(currency, places)
                posting = currency + "-r21-issue"
                frappe.get_doc(dict(doctype="Stock Entry", name=posting, docstatus=1, purpose="Material Issue",
                                    company=owner.company, total_outgoing_value=7)).db_insert()
                frappe.get_doc(dict(doctype="URY Fulfilment Record", name=currency + "-r21-posting",
                    company=owner.company, branch=owner.branch, kot=ticket.name, item_code="Soup", qty=1,
                    posted_to_erpnext=1, posting_reference=posting)).db_insert()
                lines = [dict(parent=check.name, item_code="Soup", qty=1) for check in (issuer, owner)]
                for paid in (False, True):
                    with self.subTest(paid=paid):
                        if paid:
                            owner.db_set(dict(docstatus=1, status="Paid"))
                        self.assertEqual(costs.load_posted_costs(owner.company, owner.branch, lines),
                                         {(owner.name, "Soup"): 7.0})
                        self.assertEqual(ops.trace_sale_stock_impact(owner.name)["kots"], [ticket.name])
                        self.assertFalse(ops.trace_sale_stock_impact(issuer.name)["kots"])
