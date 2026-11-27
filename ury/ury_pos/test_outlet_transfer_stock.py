"""Cross-outlet checks must issue each portion from its actual kitchen at closing."""
import unittest
from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening

from ury.ury.api.ury_kot_item_execution_service import mark_item_ready
from ury.ury.api.test_ury_production_plan_integration import (
    make_department, make_item, make_production_unit, make_warehouse, stock_receipt,
)
from ury.ury.api.ury_order_reservation_service import reconcile_order_reservations
from ury.ury.doctype.ury_order import ury_order
from ury.ury.tests.factories import make_branch, make_customer, make_pos_profile
from ury.ury_pos.floor_actions import rebind_outlet
from ury.ury_pos.transfer_context import outlet_transfer_save


class TestOutletTransferStock(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.addCleanup(frappe.db.rollback)
        self.company = "_Test Company"
        frappe.db.set_value("Company", self.company, {
            "stock_adjustment_account": "Stock Adjustment - _TC", "default_inventory_account": "Stock In Hand - _TC",
            "default_income_account": "Sales - _TC", "default_expense_account": "Cost of Goods Sold - _TC",
            "round_off_account": "Round Off - _TC", "default_cash_account": "Cash - _TC",
        })
        frappe.clear_document_cache("Company", self.company)
        self.prefix = "Transfer " + frappe.generate_hash(length=8)
        self.customer = make_customer(customer_name=self.prefix).name
        self.dish, self.extra = [make_item(self.prefix + s, "Nos", "Products") for s in (" Soup", " Pie")]
        self.east, self.west = [self.outlet(s) for s in (" East", " West")]
        from ury.tests.till_money_fixtures import _make_user
        east_holder = f"transfer-{frappe.generate_hash(length=8)}@example.com"
        _make_user(east_holder)
        self.open_till(self.east, east_holder)
        frappe.db.set_single_value("URY Production Settings", "manufacture_trigger_time", "On KOT Serve")
        self.opening = self.open_till(self.west, "Administrator")
        frappe.db.set_single_value("POS Settings", "invoice_type", "POS Invoice")

    def open_till(self, profile, user):
        return frappe.get_doc({
            "doctype": "POS Opening Entry", "pos_profile": profile.name, "user": user,
            "branch": profile.branch, "company": self.company, "posting_date": frappe.utils.nowdate(),
            "period_start_date": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-5),
            "balance_details": [{"mode_of_payment": "Cash", "opening_amount": 0}],
        }).insert().submit()

    def outlet(self, suffix):
        name = self.prefix + suffix
        branch = make_branch(branch=name, company=self.company).name
        wh = make_warehouse(name, self.company)
        dept = make_department(name, wh, branch, self.company, "Main - _TC")
        unit = make_production_unit(name, dept, branch, self.company)
        for item in (self.dish, self.extra):
            frappe.get_doc({
                "doctype": "URY Item Production Configuration", "item": item, "branch": branch,
                "production_unit": unit, "department": dept, "production_policy": "PRE_PRODUCED",
                "sourcing_mode": "EXTERNAL_RECEIPT", "external_receiving_warehouse": wh,
                "controlled_by_sales_plan": 0, "availability_mode": "Stock Available",
            }).insert()
            stock_receipt(item, wh, 5, self.company)
        frappe.get_doc({
            "doctype": "URY Branch Stock Policy", "branch": branch, "reservation_control_enabled": 1,
            "realtime_production_posting_enabled": 1,
        }).insert()
        menu = frappe.get_doc({
            "doctype": "URY Menu", "name": name, "branch": branch, "enabled": 1,
            "price_list": "Standard Selling", "items": [{"item": i, "item_name": i, "rate": 10}
                                                        for i in (self.dish, self.extra)],
        }).insert(ignore_mandatory=True)
        restaurant = frappe.get_doc({
            "doctype": "URY Restaurant", "name": name, "company": self.company, "branch": branch,
            "invoice_series_prefix": "TX", "active_menu": menu.name,
        }).insert(ignore_mandatory=True)
        return make_pos_profile(
            name=name, company=self.company, branch=branch, restaurant=restaurant.name,
            warehouse=wh, cost_center="Main - _TC", selling_price_list="Standard Selling",
            payments=[{"mode_of_payment": "Cash", "default": 1}], ury_enable_hold_fire=1,
            custom_kot_naming_series="TX-KOT-.#####", write_off_account="Round Off - _TC",
            write_off_cost_center="Main - _TC",
        )

    def invoice(self, qty=1):
        for item in (self.dish, self.extra):
            frappe.get_doc({"doctype": "Item Price", "item_code": item,
                            "price_list": "Standard Selling", "price_list_rate": 10}).insert()
        doc = frappe.get_doc({
            "doctype": "POS Invoice", "company": self.company, "customer": self.customer,
            "pos_profile": self.east.name, "restaurant": self.east.restaurant, "branch": self.east.branch,
            "waiter": "Administrator", "order_type": "Take Away", "is_pos": 1, "no_of_pax": 1,
            "items": [{"item_code": self.dish, "qty": qty, "ury_held": 1, "warehouse": self.east.warehouse,
                       "rate": 10, "reservation_line_key": "soup"}],
            "payments": [{"mode_of_payment": "Cash", "amount": 0}],
        }).insert()
        reconcile_order_reservations(doc.name, [], doc.items, doc.branch, self.company)
        return doc

    def transfer(self, doc):
        rebind_outlet(doc, self.west)
        with outlet_transfer_save(doc):
            doc.save()
        doc.reload()

    def fire(self, doc):
        with patch.object(ury_order, "getBranch", return_value=doc.branch):
            ury_order.fire_items(doc.name, line=doc.items[0].name)
        doc.reload()
        ticket = frappe.get_last_doc("URY KOT", filters={"invoice": doc.name, "docstatus": 1})
        self.assertEqual(ticket.branch, doc.branch)
        return ticket

    def close(self, doc, expected):
        doc.payments[0].amount = doc.grand_total
        doc.submit()
        closing = make_closing_entry_from_opening(self.opening)
        self.assertIn(doc.name, [r.pos_invoice for r in closing.pos_invoices])
        for row in closing.payment_reconciliation:
            row.closing_amount = row.expected_amount
        closing.insert().submit()
        voucher = doc.reload().consolidated_invoice
        self.assertTrue(voucher)
        rows = frappe.get_all("Stock Ledger Entry", filters={
            "voucher_type": "Sales Invoice", "voucher_no": voucher, "is_cancelled": 0,
        }, fields=["item_code", "warehouse", "actual_qty"])
        self.assertCountEqual([(r.item_code, r.warehouse, r.actual_qty) for r in rows], expected)

    def test_held_line_reserves_and_fires_at_destination(self):
        doc = self.invoice()
        self.transfer(doc)
        ticket = self.fire(doc)
        result = mark_item_ready(ticket.kot_items[0].name, "ready-soup")
        self.assertEqual(result["posting_intent_status"], "SKIPPED_NOT_MADE_TO_ORDER")
        active = frappe.get_all("URY Stock Reservation", filters={"order_ref": doc.name, "status": "Reserved"},
                                fields=["branch", "warehouse", "qty"])
        self.assertEqual([(r.branch, r.warehouse, r.qty) for r in active],
                         [(self.west.branch, self.west.warehouse, 1)])
        self.close(doc, [(self.dish, self.west.warehouse, -1)])

    def test_short_destination_preserves_source_reservation(self):
        doc = self.invoice(qty=2)
        from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
        make_stock_entry(item_code=self.dish, source=self.west.warehouse, qty=4, company=self.company)
        with self.assertRaises(frappe.ValidationError):
            self.transfer(doc)
        self.assertEqual(doc.reload().pos_profile, self.east.name)
        active = frappe.get_all("URY Stock Reservation", filters={"order_ref": doc.name, "status": "Reserved"},
                                fields=["branch", "warehouse", "qty"])
        self.assertEqual([(r.branch, r.warehouse, r.qty) for r in active],
                         [(self.east.branch, self.east.warehouse, 2)])

    def test_fired_line_keeps_issuer_on_full_cart_edit_and_closing(self):
        doc = self.invoice()
        self.fire(doc)
        self.transfer(doc)
        self.assertEqual(doc.items[0].warehouse, self.east.warehouse)
        with patch.object(ury_order, "getBranch", return_value=self.west.branch):
            ury_order.sync_order(
                items=[{"item": self.dish, "qty": 1, "reservation_line_key": "soup"},
                       {"item": self.extra, "qty": 1, "reservation_line_key": "pie"}],
                cashier=doc.cashier, owner=doc.owner, mode_of_payment="Cash", customer=doc.customer,
                no_of_pax=1, last_invoice=doc.name, waiter=doc.waiter, pos_profile=self.west.name,
                last_modified_time=doc.modified, invoice=doc.name, order_type=doc.order_type,
            )
        self.close(doc.reload(), [(self.dish, self.east.warehouse, -1), (self.extra, self.west.warehouse, -1)])

    def transferred_captain_check(self):
        from ury.tests.till_money_fixtures import assign_branch_user
        from ury.ury_pos.floor_actions import transfer_check_outlet
        from ury.ury_pos.outlet_context import set_active_outlet

        captain = frappe.db.get_value("POS Opening Entry", {"pos_profile": self.east.name}, "user")
        frappe.get_doc("User", captain).add_roles("URY Captain")
        for outlet in (self.east, self.west):
            assign_branch_user(outlet.branch, captain)
            outlet.append("applicable_for_users", {"user": captain})
            outlet.save()
        frappe.get_doc({
            "doctype": "POS Privilege Rule", "company": self.company, "pos_profile": self.east.name,
            "privilege": "transfer_check", "role": "URY Captain", "may_perform": 1,
        }).insert()
        doc = self.invoice()
        doc.db_set("waiter", captain)
        self.fire(doc)
        frappe.set_user(captain)
        self.addCleanup(frappe.set_user, "Administrator")
        transfer_check_outlet(doc.name, self.west.name)
        set_active_outlet(self.west.name)
        return doc.reload()

    def edit_soup_quantity(self, doc, quantity):
        ury_order.sync_order(
            items=[{"item": self.dish, "qty": quantity, "reservation_line_key": "soup",
                    "name": doc.items[0].name},
                   {"item": self.extra, "qty": 1, "reservation_line_key": "pie"}],
            cashier=doc.cashier, owner=doc.owner, mode_of_payment="Cash", customer=doc.customer,
            no_of_pax=1, last_invoice=doc.name, waiter=doc.waiter, pos_profile=self.west.name,
            last_modified_time=doc.modified, invoice=doc.name, order_type=doc.order_type,
        )
        doc.reload()

    def test_increased_fired_line_closes_against_both_issuing_kitchens(self):
        doc = self.transferred_captain_check()
        self.edit_soup_quantity(doc, 2)
        frappe.set_user("Administrator")
        reservations = frappe.get_all("URY Stock Reservation", filters={
            "order_ref": doc.name, "status": "Reserved",
        }, fields=["top_level_item", "warehouse", "qty"])
        tickets = frappe.get_all("URY KOT", filters={"invoice": doc.name, "docstatus": 1},
                                 fields=["name", "branch"])
        self.close(doc, [(self.dish, self.east.warehouse, -1), (self.dish, self.west.warehouse, -1),
                         (self.extra, self.west.warehouse, -1)])
        self.assertCountEqual([(r.top_level_item, r.warehouse, r.qty) for r in reservations],
                              [(self.dish, self.east.warehouse, 1), (self.dish, self.west.warehouse, 1),
                               (self.extra, self.west.warehouse, 1)])
        issued = [(t.branch, r.item, r.quantity) for t in tickets
                  for r in frappe.get_doc("URY KOT", t.name).kot_items]
        self.assertCountEqual(issued, [(self.east.branch, self.dish, 1),
                                       (self.west.branch, self.dish, 1), (self.west.branch, self.extra, 1)])

    def test_west_capacity_only_charges_the_extra_portions(self):
        from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

        make_stock_entry(item_code=self.dish, source=self.west.warehouse, qty=4, company=self.company)
        doc = self.transferred_captain_check()
        # One West soup remains. An increment of two must fail; a retry for one must fit.
        frappe.db.savepoint("captain_edit")
        with self.assertRaisesRegex(frappe.ValidationError, "Insufficient capacity"):
            self.edit_soup_quantity(doc, 3)
        frappe.db.rollback(save_point="captain_edit")
        self.assertEqual([(r.qty, r.warehouse) for r in doc.reload().items], [(1, self.east.warehouse)])
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": doc.name, "branch": self.west.branch}), 0)
        self.edit_soup_quantity(doc, 2)
        self.assertCountEqual([(r.item_code, r.qty, r.warehouse) for r in doc.items], [
            (self.dish, 1, self.east.warehouse), (self.dish, 1, self.west.warehouse),
            (self.extra, 1, self.west.warehouse),
        ])

    def save_grouped_cart(self, doc, soup_qty):
        ury_order.sync_order(
            items=[{"item": self.dish, "qty": soup_qty}, {"item": self.extra, "qty": 1}],
            cashier=doc.cashier, owner=doc.owner, mode_of_payment="Cash", customer=doc.customer,
            no_of_pax=1, last_invoice=doc.name, waiter=doc.waiter, pos_profile=self.west.name,
            last_modified_time=doc.modified, invoice=doc.name, order_type=doc.order_type,
        )

    def test_grouped_resave_preserves_split_stock_and_tickets(self):
        doc = self.transferred_captain_check()
        self.edit_soup_quantity(doc, 2)

        def issued_rows():
            return [(r.name, r.reservation_line_key, r.item_code, r.qty, r.warehouse) for r in doc.items]

        def tickets():
            return frappe.get_all("URY KOT", filters={"invoice": doc.name}, pluck="name", order_by="name")

        def reservations():
            return frappe.get_all("URY Stock Reservation", filters={"order_ref": doc.name},
                                  fields=["name", "status", "warehouse", "qty"], order_by="name")

        original = issued_rows(), tickets(), reservations()
        self.assertEqual(len(original[0]), 3)
        self.assertEqual(len(original[1]), 2)
        self.save_grouped_cart(doc, 2)
        doc.reload()
        self.assertEqual((issued_rows(), tickets(), reservations()), original)
        frappe.set_user("Administrator")
        self.close(doc, [(self.dish, self.east.warehouse, -1), (self.dish, self.west.warehouse, -1),
                         (self.extra, self.west.warehouse, -1)])

    def test_grouped_reduction_still_requires_sent_void_reason(self):
        doc = self.transferred_captain_check()
        self.edit_soup_quantity(doc, 2)
        with self.assertRaisesRegex(frappe.ValidationError, "POS-AUTH-REASON"):
            self.save_grouped_cart(doc, 1)
        self.assertCountEqual([(r.item_code, r.qty, r.warehouse) for r in doc.reload().items], [
            (self.dish, 1, self.east.warehouse), (self.dish, 1, self.west.warehouse),
            (self.extra, 1, self.west.warehouse),
        ])

    def sync_cart(self, doc, items):
        ury_order.sync_order(
            items=items, cashier=doc.cashier, owner=doc.owner, mode_of_payment="Cash",
            customer=doc.customer, no_of_pax=1, last_invoice=doc.name, waiter=doc.waiter,
            pos_profile=doc.pos_profile, last_modified_time=doc.modified,
            invoice=doc.name, order_type=doc.order_type,
        )
        doc.reload()

    def split_check_with_unkeyed_pie(self):
        doc = self.transferred_captain_check()
        self.sync_cart(doc, [{"item": self.dish, "qty": 2, "name": doc.items[0].name,
                              "reservation_line_key": doc.items[0].reservation_line_key},
                             {"item": self.extra, "qty": 1}])
        self.assertEqual([(r.item_code, r.warehouse) for r in doc.items],
                         [(self.dish, self.east.warehouse), (self.extra, self.west.warehouse),
                          (self.dish, self.west.warehouse)])
        self.assertFalse(doc.items[1].reservation_line_key)
        return doc

    def test_vue_resave_keeps_last_reserved_unkeyed_portion(self):
        from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

        make_stock_entry(item_code=self.extra, source=self.west.warehouse, qty=4, company=self.company)
        doc = self.split_check_with_unkeyed_pie()

        def reservations():
            return frappe.get_all("URY Stock Reservation", filters={"order_ref": doc.name},
                                  fields=["name", "reservation_group", "status", "qty"], order_by="name")

        before = reservations()
        self.assertEqual(len(before), 3)
        rows = [{"name": row.name, "item": row.item_code, "item_name": row.item_name,
                 "qty": row.qty, "rate": row.rate, "comment": row.comment or "",
                 "custom_course": row.custom_course, "reservation_line_key": row.reservation_line_key}
                for row in doc.items]
        self.sync_cart(doc, rows)
        self.assertEqual(reservations(), before)
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": doc.name}), 2)
        rows[1]["qty"] = 2
        with self.assertRaisesRegex(frappe.ValidationError, "FG_OUT_OF_STOCK|Insufficient capacity"):
            self.sync_cart(doc, rows)
        self.assertEqual(reservations(), before)

    def test_grouped_resave_keeps_unkeyed_child_in_invoice_order(self):
        doc = self.split_check_with_unkeyed_pie()
        def identities():
            return [(r.name, r.item_code, r.warehouse, r.reservation_line_key, r.qty) for r in doc.items]

        before = identities()
        self.save_grouped_cart(doc, 2)
        doc.reload()
        self.assertEqual(identities(), before)
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": doc.name}), 2)
