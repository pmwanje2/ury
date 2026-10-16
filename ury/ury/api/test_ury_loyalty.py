"""U4: real settlement and pinned ERPNext methods; only persistence is doubled.

Run in frappe-siteops:rel-v16-cc7cf44f, network disabled, no site/database.
Native method bodies are loaded from the image, never copied into this app.
"""

import ast
import inspect
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import erpnext
from frappe import _dict
from frappe.utils import add_days, cint, flt, getdate

from ury.ury.api.test_ury_kot_notes import OrderingCases

NATIVE = Path(erpnext.__file__).parent


def today():
    return "2026-10-16"


def native_functions(relative, namespace, class_name=None, names=None):
    tree = ast.parse((NATIVE / relative).read_text())
    nodes = tree.body if class_name is None else next(
        node.body for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    functions = [node for node in nodes if isinstance(node, ast.FunctionDef) and (names is None or node.name in names)]
    for node in functions:
        node.decorator_list = []
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(NATIVE / relative), "exec"), namespace)
    return namespace


class LoyaltyTests(OrderingCases, TestCase):
    def setUp(self):
        super().setUp()
        self.framework.session.user = "cashier@example.com"
        self.framework._dict = _dict
        self.pos_profile.customer = "Walk-in Customer"
        self.pos_profile.company = "Test Co"
        self.pos_profile.ury_enable_loyalty = 1
        self.pos_profile.disabled = 0
        self.pos_profile.get = lambda key, default=None: getattr(self.pos_profile, key, default)
        self.invoice.customer = "Named Customer"
        self.invoice.company = "Test Co"
        self.invoice.doctype = "POS Invoice"
        self.invoice.docstatus = 0
        self.invoice.custom_merged_pos_invoice = None
        self.invoice.rounded_total = 100
        self.invoice.conversion_rate = 1
        self.invoice.is_pos = 1
        self.invoice.is_return = 0
        self.invoice.invoice_type_in_pos = "POS Invoice"
        self.invoice.coupon_code = None
        self.invoice.posting_date = today()
        self.invoice.loyalty_program = "Synthetic Program"
        self.invoice.redeem_loyalty_points = 0
        self.invoice.loyalty_amount = self.invoice.loyalty_points = 0
        self.invoice.loyalty_redemption_account = None
        self.invoice.loyalty_redemption_cost_center = None
        self.invoice.payments = []
        self.invoice.get.side_effect = lambda key, default=None: getattr(self.invoice, key, default)
        self.invoice.set.side_effect = lambda field, value: setattr(self.invoice, field, value)
        self.invoice.append.side_effect = lambda field, value: getattr(self.invoice, field).append(_dict(value))
        self.invoice.precision.return_value = 2
        self.invoice.is_rounded_total_disabled.return_value = False
        self.invoice.get_returned_amount.return_value = 0
        self.invoice.reload.return_value = self.invoice
        self.invoice.as_dict.side_effect = lambda: {key: getattr(self.invoice, key) for key in (
            "name", "loyalty_points", "loyalty_amount", "paid_amount", "outstanding_amount", "docstatus"
        )}
        self.program = _dict(name="Synthetic Program", company="Test Co", conversion_factor=2,
                             expense_account="Synthetic Expense", cost_center="Synthetic CC",
                             from_date="2020-01-01", to_date=None, expiry_duration=365,
                             collection_rules=[SimpleNamespace(as_dict=lambda: _dict(
                                 min_spent=0, tier_name="Synthetic", collection_factor=10))])
        self.program.as_dict = lambda: {key: value for key, value in self.program.items() if key != "as_dict"}
        self.balance = 50
        self.ledger = []
        self.enrolled = True
        self.framework.db.get_value.side_effect = self.db_value
        original_get_doc = self.framework.get_doc.side_effect

        def get_doc(doctype, name=None):
            if isinstance(doctype, dict) and doctype.get("doctype") == "Loyalty Point Entry":
                entry = _dict(doctype)
                entry.flags = SimpleNamespace()
                entry.save = lambda: self.ledger.append(entry)
                return entry
            if doctype == "Loyalty Program":
                return self.program
            return original_get_doc(doctype, name)

        self.framework.get_doc.side_effect = get_doc
        namespace = dict(frappe=self.framework, _=lambda value: value, flt=flt, cint=cint,
                         today=today, getdate=getdate, add_days=add_days)
        self.native = native_functions("accounts/doctype/loyalty_program/loyalty_program.py", namespace)
        self.native["get_loyalty_details"] = lambda *args, **kw: dict(loyalty_points=self.balance, total_spent=0)
        module = ModuleType("erpnext.accounts.doctype.loyalty_program.loyalty_program")
        module.__dict__.update(self.native)
        self.enterContext(patch.dict(__import__("sys").modules, {module.__name__: module}))
        self.pos_native = native_functions("accounts/doctype/pos_invoice/pos_invoice.py", namespace.copy(),
                                           "POSInvoice", {"validate_loyalty_transaction", "set_outstanding_amount", "on_submit"})
        self.sales_native = native_functions("accounts/doctype/sales_invoice/sales_invoice.py", namespace.copy(),
                                             "SalesInvoice", {"make_loyalty_point_entry", "apply_loyalty_points"})
        ledger_module = ModuleType("erpnext.accounts.doctype.loyalty_point_entry.loyalty_point_entry")
        ledger_module.get_loyalty_point_entries = lambda *args: [_dict(
            name="EARNED", loyalty_points=self.balance, expiry_date="2099-01-01", loyalty_program_tier="Synthetic",
            invoice_type="POS Invoice", invoice="OLDER-BILL")]
        ledger_module.get_redemption_details = lambda *args: _dict()
        self.enterContext(patch.dict(__import__("sys").modules, {ledger_module.__name__: ledger_module}))
        totals = native_functions("controllers/taxes_and_totals.py", namespace.copy(),
                                  "calculate_taxes_and_totals", {"calculate_paid_amount"})

        self.calculate_for = lambda doc: (
            totals["calculate_paid_amount"](SimpleNamespace(doc=doc)),
            self.pos_native["set_outstanding_amount"](doc),
        )

        def calculate():
            self.calculate_for(self.invoice)

        self.invoice.calculate_taxes_and_totals.side_effect = calculate
        self.invoice.save.side_effect = calculate
        self.invoice.make_loyalty_point_entry.side_effect = lambda: self.sales_native["make_loyalty_point_entry"](self.invoice)
        self.invoice.apply_loyalty_points.side_effect = lambda: self.sales_native["apply_loyalty_points"](self.invoice)

        def submit():
            calculate()
            self.pos_native["validate_loyalty_transaction"](self.invoice)
            self.invoice.docstatus = 1
            self.pos_native["on_submit"](self.invoice)

        self.invoice.submit.side_effect = submit

    def db_value(self, doctype, name, field, **kwargs):
        field = field[0] if isinstance(field, list) and len(field) == 1 else field
        if doctype == "Customer" and field == "loyalty_program":
            return self.program.name if self.enrolled else None
        if doctype == "Loyalty Program":
            return [self.program.get(key) for key in field] if isinstance(field, list) else self.program.get(field)
        return OrderingCases.get_value(doctype, name, field)

    def settle(self, points=10, **extra):
        kwargs = dict(customer="Named Customer", payments=[dict(mode_of_payment="Cash", amount=80)],
                      cashier="forged", pos_profile="POS A", owner="forged", invoice=self.invoice.name,
                      loyalty_points=points)
        kwargs.update(extra)
        # Frappe dispatch drops unknown keyword inputs. RED must show missing
        # redemption behavior, not a Python unexpected-keyword TypeError.
        supported = inspect.signature(self.order.make_invoice).parameters
        return self.order.make_invoice(**{key: value for key, value in kwargs.items() if key in supported})

    def quote(self, points=0):
        method = getattr(self.order, "get_loyalty_redemption", None)
        self.assertTrue(callable(method), "Payment balance/native redemption quote is missing")
        return method(self.invoice.name, "Named Customer", "POS A", loyalty_points=points)

    def test_native_redemption_reduces_cash_due_and_native_submit_posts_once(self):
        result = self.settle()
        self.assertEqual((result["loyalty_points"], result["loyalty_amount"], result["paid_amount"], result["outstanding_amount"]),
                         (10, 20, 100, 0))
        self.assertEqual([(entry.loyalty_points, entry.get("redeem_against")) for entry in self.ledger],
                         [(8, None), (-10, "EARNED")])

    def test_partial_tender_keeps_native_outstanding_after_redemption(self):
        self.pos_profile.allow_partial_payment = 1
        self.invoice.rounded_total = self.invoice.grand_total = 120
        result = self.settle()
        self.assertEqual(result["outstanding_amount"], 20)

    def test_over_balance_is_refused_before_any_save(self):
        self.balance = 9
        with self.assertRaisesRegex(ValueError, "enough Loyalty Points"):
            self.settle()
        self.invoice.save.assert_not_called()
        self.assertEqual(self.ledger, [])

    def test_over_bill_is_refused_before_any_save(self):
        self.balance = 100
        with self.assertRaisesRegex(ValueError, "more value than the Total"):
            self.settle(51)
        self.invoice.save.assert_not_called()

    def test_stale_prepopulated_amount_is_never_trusted(self):
        self.invoice.loyalty_amount = 99
        self.invoice.loyalty_points = 49
        result = self.settle()
        self.assertEqual((result["loyalty_points"], result["loyalty_amount"]), (10, 20))

    def test_stale_quote_revalidates_current_native_balance(self):
        self.assertEqual(self.quote(10)["amount_due"], 80)
        self.balance = 5
        with self.assertRaisesRegex(ValueError, "enough Loyalty Points"):
            self.settle()
        self.assertEqual(self.ledger, [])

    def test_disabled_after_hold_refuses_redemption_even_if_hold_flag_is_off(self):
        self.pos_profile.ury_enable_loyalty = 0
        with self.assertRaisesRegex(ValueError, "URY_LOYALTY_DISABLED"):
            self.settle()

    def test_missing_profile_gate_is_off(self):
        del self.pos_profile.ury_enable_loyalty
        self.assertEqual(self.quote(), {"enabled": False})
        with self.assertRaisesRegex(ValueError, "URY_LOYALTY_DISABLED"):
            self.settle()

    def test_default_customer_and_literal_walkin_cannot_redeem(self):
        for customer in ("Walk-in Customer", "Walk-in", "", "   "):
            with self.subTest(customer=customer):
                self.invoice.customer = customer
                kwargs = dict(customer=customer, payments=[], cashier="x", pos_profile="POS A", owner="x",
                              invoice=self.invoice.name, loyalty_points=10)
                supported = inspect.signature(self.order.make_invoice).parameters
                with self.assertRaisesRegex(ValueError, "URY_LOYALTY_CUSTOMER"):
                    self.order.make_invoice(**{k: v for k, v in kwargs.items() if k in supported})

    def test_unenrolled_customer_cannot_redeem_stale_program(self):
        self.enrolled = False
        with self.assertRaisesRegex(ValueError, "URY_LOYALTY_CUSTOMER"):
            self.settle()

    def test_malformed_points_are_not_coerced_or_silently_ignored(self):
        for points in (-1, 1.5, "NaN", "Infinity", "no", True):
            with self.subTest(points=points), self.assertRaisesRegex(ValueError, "URY_LOYALTY_POINTS"):
                self.settle(points)

    def test_no_redemption_clears_stale_draft_redemption(self):
        self.invoice.loyalty_amount = 99
        self.invoice.loyalty_points = 49
        self.invoice.redeem_loyalty_points = 1
        result = self.settle(0)
        self.assertEqual((result["loyalty_points"], result["loyalty_amount"]), (0, 0))
        self.assertEqual([entry.loyalty_points for entry in self.ledger], [10])

    def test_balance_quote_uses_native_values_without_saving_or_ledger_writes(self):
        result = self.quote(10)
        self.assertEqual({key: result[key] for key in ("enabled", "balance", "loyalty_points", "loyalty_amount", "amount_due")},
                         dict(enabled=True, balance=50, loyalty_points=10, loyalty_amount=20, amount_due=80))
        self.invoice.save.assert_not_called()
        self.invoice.submit.assert_not_called()
        self.assertEqual(self.ledger, [])

    def test_other_profile_cannot_enable_a_disabled_invoice_profile(self):
        with self.assertRaisesRegex(ValueError, "URY_LOYALTY_PROFILE"):
            method = getattr(self.order, "get_loyalty_redemption", None)
            self.assertTrue(callable(method), "Profile-bound loyalty quote is missing")
            method(self.invoice.name, "Named Customer", "Other POS", loyalty_points=10)

    def test_native_customer_permission_refusal_is_not_bypassed(self):
        self.framework.has_permission.side_effect = lambda *args, **kw: (
            self.framework.throw("Not permitted", PermissionError) if args[0] == "Customer" else True
        )
        with self.assertRaises((PermissionError, ValueError)):
            self.settle()
        self.invoice.save.assert_not_called()

    def test_customer_is_locked_before_native_balance_validation(self):
        self.settle()
        self.assertTrue(any("tabCustomer" in str(call.args[0]) and "FOR UPDATE" in str(call.args[0])
                            and call.args[1] == ("Named Customer",) for call in self.framework.db.sql.call_args_list),
                        "Concurrent redemptions must serialize before native balance validation")

    def test_held_lines_still_refuse_without_posting_loyalty(self):
        self.invoice.items = [SimpleNamespace(ury_held=1)]
        with self.assertRaisesRegex(ValueError, "URY_HELD_NOT_FIRED"):
            self.settle()
        self.assertEqual(self.ledger, [])

    def test_install_definition_adds_default_off_profile_field_only(self):
        source = Path(__file__).resolve().parents[2] / "setup_customizations.py"
        functions = native_functions(source, {}, names={"get_custom_fields"})
        fields = functions["get_custom_fields"]()["POS Profile"]
        field = next((field for field in fields if field["fieldname"] == "ury_enable_loyalty"), None)
        self.assertIsNotNone(field, "Install does not provide an editable native POS Profile loyalty gate")
        self.assertEqual((field["fieldtype"], field["default"]), ("Check", "0"))

    def test_merged_settlement_preserves_other_bill_tender_and_clears_stale_redemption(self):
        target = MagicMock()
        for field in ("customer", "company", "pos_profile", "doctype", "conversion_rate", "is_pos", "is_return"):
            setattr(target, field, getattr(self.invoice, field))
        target.name = "OTHER-BILL"
        target.restaurant_table = None
        target.docstatus = 0
        target.items = []
        target.payments = []
        target.rounded_total = target.grand_total = 60
        target.redeem_loyalty_points = 1
        target.loyalty_amount = 40
        target.loyalty_points = 20
        target.get.side_effect = lambda key, default=None: getattr(target, key, default)
        target.set.side_effect = lambda key, value: setattr(target, key, value)
        target.append.side_effect = lambda key, value: getattr(target, key).append(_dict(value))
        target.precision.return_value = 2
        target.calculate_taxes_and_totals.side_effect = lambda: self.calculate_for(target)
        target.save.side_effect = lambda **kw: self.calculate_for(target)
        self.invoice.custom_merged_pos_invoice = target.name
        original = self.framework.get_doc.side_effect
        self.framework.get_doc.side_effect = lambda dt, name=None: (
            target if dt == "POS Invoice" and name == target.name else original(dt, name)
        )
        self.settle(payments=[dict(mode_of_payment="Cash", amount=140)])
        self.assertEqual([row.amount for row in self.invoice.payments], [80])
        self.assertEqual([row.amount for row in target.payments], [60])
        self.assertEqual((target.loyalty_amount, target.loyalty_points), (0, 0))

    def test_held_linked_bill_refuses_without_saving_either_bill(self):
        target = MagicMock(items=[SimpleNamespace(ury_held=1)])
        self.invoice.custom_merged_pos_invoice = "OTHER-BILL"
        original = self.framework.get_doc.side_effect
        self.framework.get_doc.side_effect = lambda dt, name=None: (
            target if dt == "POS Invoice" and name == "OTHER-BILL" else original(dt, name)
        )
        with self.assertRaisesRegex(ValueError, "URY_HELD_NOT_FIRED"):
            self.settle()
        self.invoice.save.assert_not_called()
        target.save.assert_not_called()

    def test_redemption_cannot_replace_customer_or_profile(self):
        for extra, refusal in ((dict(customer="Other Customer"), "URY_LOYALTY_CUSTOMER"),
                               (dict(pos_profile="Other POS"), "URY_LOYALTY_PROFILE")):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, refusal):
                self.settle(**extra)
