"""POS credit controls at the public settlement boundary (isolated ERP runtime)."""

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

import frappe
from ury.ury.doctype.ury_order import ury_order as order
from ury.ury_pos import api


class Credit(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(order, "_", lambda msg: msg))
        self.enterContext(
            patch.object(
                frappe, "get_system_settings", return_value="Commercial Rounding"
            )
        )
        self.f = self.enterContext(patch.object(order, "frappe"))
        self.enterContext(patch("ury.ury_pos.credit.frappe", self.f))
        self.enterContext(patch.object(api, "getBranch", return_value="West"))
        self.f.PermissionError = PermissionError
        self.f.ValidationError = ValueError
        self.f.throw.side_effect = lambda msg, exc=ValueError, **kw: self.fail_with(
            exc, msg
        )
        self.f.get_roles.return_value = ["URY Cashier"]
        self.f.session.user = "Cashier"
        self.f.utils.cint.side_effect = lambda n: int(n or 0)
        self.profile = frappe._dict(
            name="Till",
            branch="West",
            customer="Walk-in",
            company="Hotel",
            currency="UGX",
            allow_partial_payment=1,
            applicable_for_users=[],
            custom_order_type_customers=[frappe._dict(customer="Counter")],
        )
        self.f.get_cached_doc.return_value = self.profile
        self.invoice = self.bill("B1", 100)
        self.target = self.bill("B2", 80)
        self.f.get_doc.side_effect = lambda dt, name, **kw: (
            self.profile
            if dt == "POS Profile"
            else self.target
            if name == "B2"
            else self.invoice
        )
        self.enterContext(
            patch.object(order, "get_order_invoice", return_value=self.invoice)
        )
        self.enterContext(patch.object(order, "_enforce_order_access"))
        self.enterContext(patch.object(order, "getBranch", return_value="West"))
        self.gl = self.enterContext(
            patch(
                "erpnext.selling.doctype.customer.customer.get_customer_outstanding",
                return_value=20,
            )
        )
        self.disabled = 0
        self.limit = 150
        self.events = []
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute(
            "CREATE TABLE `tabPOS Invoice` (name, customer, company, docstatus, consolidated_invoice, status, outstanding_amount, is_return, rounded_total, grand_total, paid_amount, conversion_rate)"
        )
        self.db.create_function("least", -1, min)
        self.f.db.sql.side_effect = self.sql
        self.f.db.get_value.side_effect = self.value
        self.f.has_permission.return_value = True

    @staticmethod
    def fail_with(exc, msg):
        raise exc(msg)

    def bill(self, name, total):
        bill = MagicMock()
        bill.name = name
        bill.branch = "West"
        bill.customer = "AC-1"
        bill.company = "Hotel"
        bill.currency = "UGX"
        bill.conversion_rate = 1
        bill.pos_profile = "Till"
        bill.restaurant_table = None
        bill.custom_merged_pos_invoice = None
        bill.docstatus = 0
        bill.precision.return_value = 2
        bill.items = []
        bill.payments = []
        bill.rounded_total = bill.grand_total = total
        bill.set.side_effect = lambda key, value: setattr(bill, key, value)
        bill.get.side_effect = lambda key, default=None: vars(bill).get(key, default)
        bill.append.side_effect = lambda key, row: getattr(bill, key).append(
            frappe._dict(row)
        )
        bill.save.side_effect = lambda **kw: self.events.append("save " + name)
        bill.submit.side_effect = lambda: (
            self.events.append("submit " + name),
            setattr(bill, "docstatus", 1),
        )
        return bill

    def value(self, dt, filters, field=None, **kw):
        if dt == "Customer":
            return self.disabled
        if dt == "Customer Credit Limit":
            self.assertEqual(filters["parenttype"], "Customer")
            self.assertEqual(filters["company"], "Hotel")
            self.assertEqual(filters["parent"], "AC-1")
            return self.limit
        if dt == "POS Invoice" and field == "docstatus":
            return self.invoice.docstatus
        return None

    def sql(self, query, args=(), **kw):
        if "FROM `tabCustomer`" in query:
            self.assertIn("FOR UPDATE", query.upper())
            self.events.append("customer lock")
            return [("AC-1",)]
        if "SUM(" in query.upper():
            return self.db.execute(
                query.replace("%s", "?").replace("FOR UPDATE", ""), args
            ).fetchall()
        self.events.append("invoice lock")
        return [("B1",)]

    def settle(self, customer="AC-1", cash=0, submit=1):
        return order.make_invoice(
            customer,
            [dict(mode_of_payment="Cash", amount=cash)],
            "Cashier",
            "Till",
            "Captain",
            invoice="B1",
            submit_invoice=submit,
        )

    def test_account_eligibility(self):
        for customer, disabled, limit, code in [
            ("Walk-in", 0, 1000, "001"),
            ("Counter", 0, 1000, "001"),
            ("AC-1", 1, 1000, "002"),
            ("AC-1", 0, None, "003"),
            ("AC-1", 0, 0, "003"),
            ("AC-1", 0, -1, "003"),
        ]:
            with self.subTest(customer=customer, disabled=disabled, limit=limit):
                self.disabled, self.limit = disabled, limit
                with self.assertRaisesRegex(ValueError, "URY-CREDIT-" + code):
                    self.settle(customer)
                self.invoice.save.assert_not_called()

    def test_uncleared_exposure_counts_once_and_unpaid_returns_reduce_it(self):
        for name, state, link, status, due, ret, total, paid in [
            ("pending", 1, "", "Unpaid", 70, 0, 70, 0),
            ("credit-note", 1, None, "Return", 0, 1, -15, -5),
            ("posted", 1, "SI-1", "Unpaid", 999, 0, 999, 0),
            ("closing", 1, "", "Consolidated", 999, 0, 999, 0),
            ("draft", 0, "", "Draft", 999, 0, 999, 0),
            ("B1", 1, "", "Unpaid", 999, 0, 999, 0),
        ]:
            self.db.execute(
                "INSERT INTO `tabPOS Invoice` VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    name,
                    "AC-1",
                    "Hotel",
                    state,
                    link,
                    status,
                    due,
                    ret,
                    total,
                    total,
                    paid,
                    1,
                ),
            )
        self.limit = 139  # GL 20 + pending 70 - return 10 + this shortfall 60 = 140
        with self.assertRaisesRegex(ValueError, "URY-CREDIT-004"):
            self.settle(cash=40)
        self.limit = 140
        self.assertEqual(self.settle(cash=40)["status"], "Submitted")
        self.gl.assert_called_with("AC-1", "Hotel", ignore_outstanding_sales_order=True)
        self.assertEqual(
            self.invoice.payments, [dict(mode_of_payment="Cash", amount=40)]
        )

    def test_combined_limit_and_lock_order(self):
        self.invoice.custom_merged_pos_invoice = "B2"
        self.target.customer = "Walk-in"
        self.limit = 159  # 20 GL + 180 combined less 40 cash
        with self.assertRaisesRegex(ValueError, "URY-CREDIT-004"):
            self.settle(cash=40)
        self.assertFalse(any(e.startswith("save") for e in self.events))
        self.events.clear()
        self.limit = 160
        self.settle(cash=40)
        self.assertEqual(self.events[0], "customer lock")
        self.assertEqual(self.events.count("customer lock"), 1)
        self.assertFalse(any(c.args[0] == "Customer" and c.kwargs.get("for_update")
                             for c in self.f.db.get_value.call_args_list), "second Customer locking read")
        self.assertEqual(self.target.customer, "AC-1")
        self.assertEqual([p.amount for p in self.target.payments], [0])

    def test_print_then_submit_rechecks_exposure(self):
        self.assertEqual(self.settle(submit=0)["status"], "Draft")
        self.limit = 119
        with self.assertRaisesRegex(ValueError, "URY-CREDIT-004"):
            order.submit_order_invoice("B1")
        self.invoice.submit.assert_not_called()

    def test_retry_by_invoice_never_posts_twice(self):
        self.settle()
        self.settle()
        self.invoice.submit.assert_called_once()

    def test_paid_walkin_does_not_need_a_credit_limit(self):
        self.limit = None
        self.settle("Walk-in", 100)
        self.gl.assert_not_called()

    def test_cashier_only_and_same_profile(self):
        self.f.get_roles.return_value = ["URY Captain"]
        with self.assertRaises(PermissionError):
            self.settle()
        self.f.get_roles.return_value = ["URY Cashier"]
        self.invoice.pos_profile = "Other till"
        with self.assertRaises(PermissionError):
            self.settle()

    def test_picker_refuses_unprivileged_user(self):
        fn = getattr(api, "credit_accounts", None)
        self.assertTrue(callable(fn), "Missing approved credit-account lookup")
        with patch.object(api, "frappe", self.f):
            self.f.get_roles.return_value = ["URY Captain"]
            with self.assertRaises(PermissionError):
                fn("Till", "Acme")
            self.gl.assert_not_called()

    def test_till_assignment_allows_cashier_without_profile_desk_read(self):
        self.f.has_permission.side_effect = lambda dt, *a, **kw: dt != "POS Profile"
        self.profile.applicable_for_users = [frappe._dict(user="Cashier")]
        self.assertEqual(self.settle()["status"], "Submitted")

    def test_wrong_till_assignment_is_denied(self):
        self.profile.applicable_for_users = [frappe._dict(user="Other cashier")]
        with self.assertRaises(PermissionError):
            self.settle()

    def test_partner_submit_failure_aborts_merged_credit(self):
        self.invoice.custom_merged_pos_invoice = "B2"
        self.limit = 500
        self.settle(cash=40, submit=0)
        self.target.submit.side_effect = ValueError("Partner refused")
        with self.assertRaisesRegex(ValueError, "Partner refused"):
            order.submit_order_invoice("B1")
        self.invoice.submit.assert_not_called()

    def test_native_save_repricing_is_rechecked_before_submit(self):
        self.invoice.save.side_effect = lambda: setattr(
            self.invoice, "rounded_total", 200
        )
        with self.assertRaisesRegex(ValueError, "URY-CREDIT-004"):
            self.settle()
        self.invoice.submit.assert_not_called()
