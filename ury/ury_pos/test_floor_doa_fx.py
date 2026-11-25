"""Siteless regression coverage for company-currency floor authorization."""

import unittest
from unittest.mock import patch

from ury.ury_pos import test_pos_return as returns
from ury.ury_pos import test_pricing_privileged_actions as pricing
from ury.ury_pos import test_void_sent_and_cancel as voids


class TestFloorDoaFx(unittest.TestCase):
    def set_up_harness(self, case_type):
        case = case_type()
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    def pricing_invoice(self, company_currency, rate):
        case = self.set_up_harness(pricing.TestPricingPrivilegedActions)
        case.h.docs[("Company", "Test Co")] = pricing.FakeDoc({"default_currency": company_currency})
        case.invoice.update(currency="USD", conversion_rate=rate)
        return case

    def test_ugx_price_override_requires_manager_and_keeps_document_claim(self):
        case = self.pricing_invoice("UGX", 3700)
        case.invoice["items"][0].update(rate=100, price_list_rate=100)
        case.h.rules[0].update(max_percent=100, max_amount=100000)
        actions, priv_auth = case._import_pricing_actions()

        with patch.object(actions, "authorize_or_perform", wraps=priv_auth.authorize_or_perform) as authorize:
            with self.assertRaisesRegex(pricing.FakePermissionError, "POS-AUTH-REQUIRED"):
                actions.override_line_price("POS-INV-100", "Main Till", "ITEM-ROW-1", 50, reason="PRICE-MATCH")
        self.assertEqual(authorize.call_args.kwargs["amount"], 185000)
        self.assertEqual(authorize.call_args.kwargs["claim"]["amount"], 50)
        self.assertEqual(case.invoice["items"][0].rate, 100)

    def test_ugx_comp_passes_company_amount_separately_from_claim(self):
        case = self.pricing_invoice("UGX", 3700)
        case.invoice["items"][0].update(rate=50, price_list_rate=50, amount=50)
        actions, priv_auth = case._import_pricing_actions()

        with patch.object(actions, "authorize_or_perform", wraps=priv_auth.authorize_or_perform) as authorize:
            with self.assertRaisesRegex(pricing.FakePermissionError, "POS-AUTH-REQUIRED"):
                actions.apply_complimentary(
                    "POS-INV-100", "Main Till", scope="item", item_row_name="ITEM-ROW-1", reason="VIP-COMP"
                )
        self.assertEqual(authorize.call_args.kwargs["amount"], 185000)
        self.assertEqual(authorize.call_args.kwargs["claim"]["amount"], 50)

    def test_kwd_price_override_uses_three_decimal_precision(self):
        case = self.pricing_invoice("KWD", 0.307)
        case.invoice["items"][0].update(rate=200, price_list_rate=200)
        actions, _ = case._import_pricing_actions()

        with patch.object(actions, "authorize_or_perform", side_effect=pricing.FakePermissionError) as authorize:
            for new_rate, expected in [(100, 30.700), (99.99, 30.703)]:
                with self.subTest(new_rate=new_rate):
                    with self.assertRaises(pricing.FakePermissionError):
                        actions.override_line_price("POS-INV-100", "Main Till", "ITEM-ROW-1", new_rate, reason="PRICE-MATCH")
                    self.assertEqual(authorize.call_args.kwargs["amount"], expected)

    def test_same_currency_missing_rate_uses_document_amount(self):
        case = self.pricing_invoice("USD", None)
        case.invoice["items"][0].update(rate=100, price_list_rate=100)
        actions, _ = case._import_pricing_actions()
        with patch.object(actions, "authorize_or_perform", side_effect=pricing.FakePermissionError) as authorize:
            with self.assertRaises(pricing.FakePermissionError):
                actions.override_line_price("POS-INV-100", "Main Till", "ITEM-ROW-1", 50, reason="PRICE-MATCH")
        self.assertEqual(authorize.call_args.kwargs["amount"], 50)

    def test_invalid_foreign_rates_fail_before_authorization(self):
        case = self.pricing_invoice("UGX", None)
        actions, _ = case._import_pricing_actions()
        with patch.object(actions, "authorize_or_perform", return_value={"reason": "", "reason_note": "", "approver": "", "via_pin": False}) as authorize:
            for conversion_rate in (None, "", 0, -1, "bad", float("nan"), float("inf")):
                with self.subTest(conversion_rate=conversion_rate):
                    case.invoice.conversion_rate = conversion_rate
                    with self.assertRaisesRegex(pricing.FakeValidationError, "POS-FX"):
                        actions.override_line_price(
                            "POS-INV-100", "Main Till", "ITEM-ROW-1", 18000, reason="PRICE-MATCH"
                        )
            authorize.assert_not_called()

    def test_ugx_return_uses_company_amount_for_partial_refund(self):
        case = self.set_up_harness(returns.TestPOSReturnAndRefund)
        case.h.frappe.session.user = "manager2@example.test"
        inv = case.h.add_closed_check(
            "POS-FX-RETURN", currency="USD",
            items=[{"name": "ROW-1", "item_code": "COFFEE", "qty": 2, "rate": 50}],
            payments=[{"mode_of_payment": "Cash", "amount": 100}],
        )
        inv.conversion_rate = 3700
        original_get_value = case.h.frappe.db.get_value

        def get_value(doctype, *args, **kwargs):
            if doctype == "Company":
                return "UGX"
            return original_get_value(doctype, *args, **kwargs)

        with patch.object(case.h.frappe.db, "get_value", side_effect=get_value):
            with patch.object(case.mod, "require_pos_override", side_effect=case.h.frappe.PermissionError) as authorize:
                with self.assertRaises(case.h.frappe.PermissionError):
                    case.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")
        self.assertEqual(authorize.call_args.kwargs["amount"], 185000)
        self.assertEqual(authorize.call_args.args[4]["refund_total"], 50)

    def test_ugx_sent_item_reduction_uses_company_cap(self):
        case = self.set_up_harness(voids.TestSentItemVoidAndCancelOrder)
        case.h.frappe.session.user = "manager@example.test"
        inv = case.h.add_invoice()
        inv.update(currency="USD", conversion_rate=3700)
        inv.items[0].rate = 50
        original_get_value = case.h.frappe.db.get_value

        def get_value(doctype, *args, **kwargs):
            if doctype == "Company":
                return "UGX"
            return original_get_value(doctype, *args, **kwargs)

        with patch.object(case.h.frappe.db, "get_value", side_effect=get_value):
            with patch.object(case.h.override, "may_perform", side_effect=lambda *a, amount=None, **kw: amount <= 100000):
                with self.assertRaisesRegex(case.h.frappe.PermissionError, "POS-AUTH-REQUIRED"):
                    case.qty_mod.reduce_order_item_qty(inv.name, "ROW-1", 1, reason="VOID-GUEST")
        self.assertEqual(inv.items[0].qty, 2)
