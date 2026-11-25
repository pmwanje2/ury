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

    def test_foreign_unreferenced_return_uses_profile_exchange_rate(self):
        import sys
        from types import ModuleType

        case = self.set_up_harness(returns.TestPOSReturnAndRefund)
        profile = case.h.profiles["Till A"]
        profile.currency = "USD"
        profile.custom_unreferenced_return_roles = "URY Admin"
        db = case.h.frappe.db
        original_get_value = db.get_value

        def get_value(doctype, *args, **kwargs):
            if doctype == "Company":
                return "UGX"
            if doctype == "Item Price":
                return 50
            return original_get_value(doctype, *args, **kwargs)

        setup_utils = ModuleType("erpnext.setup.utils")
        setup_utils.get_exchange_rate = lambda source, target, date: 3700 if (source, target) == ("USD", "UGX") else 0
        with patch.dict(sys.modules, {setup_utils.__name__: setup_utils}):
            with patch.object(db, "get_value", side_effect=get_value):
                with patch.object(case.mod, "require_pos_override", side_effect=case.h.frappe.PermissionError) as authorize:
                    try:
                        case.mod.create_unreferenced_return(
                            "Till A", "Walk-in", [{"item_code": "BURGER", "qty": 1}],
                            [{"mode_of_payment": "Cash", "amount": 50}], reason="RET-QUALITY",
                        )
                    except (case.h.frappe.ValidationError, case.h.frappe.PermissionError) as exc:
                        failure = str(exc)
        self.assertTrue(authorize.called, failure)
        self.assertEqual(authorize.call_args.kwargs["amount"], 185000)
        self.assertEqual(authorize.call_args.args[4]["refund_total"], 50)

    def test_price_override_cap_uses_whole_line_and_preserves_claim(self):
        case = self.pricing_invoice("UGX", 3700)
        case.invoice["items"][0].update(rate=100, price_list_rate=100, qty=3)
        case.h.rules[0].update(max_percent=100, max_amount=100000)
        actions, priv_auth = case._import_pricing_actions()
        with patch.object(actions, "authorize_or_perform", wraps=priv_auth.authorize_or_perform) as authorize:
            with self.assertRaisesRegex(pricing.FakePermissionError, "POS-AUTH-REQUIRED"):
                actions.override_line_price("POS-INV-100", "Main Till", "ITEM-ROW-1", 80, reason="PRICE-MATCH")
        self.assertEqual(authorize.call_args.kwargs["amount"], 222000)
        self.assertEqual(authorize.call_args.kwargs["claim"]["amount"], 20)

    def test_open_item_halo_and_minimum_are_company_unit_prices(self):
        case = self.pricing_invoice("UGX", 3700)
        item = case.h.docs[("Item", "OPEN-FISH")]
        item.update(custom_halo_max_price=100000, custom_min_price=20000)
        actions, _ = case._import_pricing_actions()
        with patch.object(actions, "_resolve_item_cost", return_value=0):
            with patch.object(actions, "authorize_or_perform", side_effect=pricing.FakePermissionError) as authorize:
                for rate, force_pin in ((10, False), (30, True)):
                    with self.subTest(rate=rate):
                        with self.assertRaises(pricing.FakePermissionError):
                            actions.add_open_item("POS-INV-100", "Main Till", "OPEN-FISH", rate=rate, qty=2, reference_name_text="Fish")
                        self.assertEqual(authorize.call_args.kwargs["force_pin"], force_pin)
                        self.assertEqual(authorize.call_args.kwargs["amount"], rate * 2 * 3700)
                        self.assertEqual(authorize.call_args.kwargs["claim"]["rate"], rate)

    def test_waive_service_charge_uses_current_company_amount(self):
        case = self.pricing_invoice("UGX", 3700)
        case.invoice["taxes"] = [pricing.FakeDoc({"custom_pos_charge_kind": "Service Charge", "tax_amount": 50})]
        actions, _ = case._import_pricing_actions()
        with patch.object(actions, "authorize_or_perform", side_effect=pricing.FakePermissionError) as authorize:
            with self.assertRaises(pricing.FakePermissionError):
                actions.adjust_service_charge("POS-INV-100", "Main Till", action="waive", reason="SVC-WAIVE")
        self.assertEqual(authorize.call_args.kwargs["amount"], 185000)
        self.assertIsNone(authorize.call_args.kwargs["claim"]["new_amount"])

    def test_foreign_tender_halo_uses_company_currency(self):
        from ury.ury_pos import test_tender_halo

        case = self.set_up_harness(test_tender_halo.TestTenderHalo)
        doc = case._invoice(total=50, currency="USD", payments=[{"mode_of_payment": "Cash", "amount": 50}])
        doc.conversion_rate = 3700
        db = case.frappe.db
        original_get_value = db.get_value

        def get_value(doctype, *args, **kwargs):
            if doctype == "Company" and args[-1] == "default_currency":
                return "UGX"
            if doctype == "Mode of Payment" and args[-1] == "custom_halo_max_amount":
                return 100000
            return original_get_value(doctype, *args, **kwargs)

        with patch.object(db, "get_value", side_effect=get_value):
            with patch.object(case.tc, "_require_till_privilege", side_effect=PermissionError) as authorize:
                with self.assertRaises(PermissionError):
                    case.tc.apply_halo_approval_token(doc, "token")
        self.assertEqual(authorize.call_args.kwargs["amount"], 185000)
        self.assertEqual(authorize.call_args.args[3]["tenders"], [{"mode_of_payment": "Cash", "amount": 50}])

    def test_change_service_charge_prices_zero_and_nonzero_overrides(self):
        case = self.pricing_invoice("UGX", 3700)
        case.invoice["items"][0].update(qty=1, rate=1000, amount=1000, net_amount=1000)
        case.invoice.update(net_total=1000, taxes=[
            pricing.FakeDoc({"custom_pos_charge_kind": "Service Charge", "tax_amount": 50}),
        ])
        actions, _ = case._import_pricing_actions()
        with patch.object(actions, "authorize_or_perform", side_effect=pricing.FakePermissionError) as authorize:
            for change in ({"new_amount": 0}, {"new_rate": 0}, {"new_rate": 5}):
                with self.subTest(change=change):
                    with self.assertRaises(pricing.FakePermissionError):
                        actions.adjust_service_charge("POS-INV-100", "Main Till", action="change", reason="SVC-WAIVE", **change)
                    self.assertEqual(authorize.call_args.kwargs["amount"], 185000)
                    self.assertEqual(authorize.call_args.kwargs["claim"]["sc_action"], "change")

    def test_only_service_charge_status_has_get_whitelist(self):
        case = self.pricing_invoice("UGX", 3700)

        def whitelist(*, methods=None):
            def decorate(function):
                function.allowed_methods = methods
                return function
            return decorate

        with patch.object(case.h.frappe, "whitelist", whitelist):
            actions, _ = case._import_pricing_actions()
        self.assertEqual(getattr(actions.get_service_charge_status, "allowed_methods", None), ["GET"])
        self.assertIsNone(getattr(actions.current_service_charge_amount, "allowed_methods", None))

    def test_profile_quote_does_not_reuse_rate_excluded_by_erpnext_policy(self):
        import sys
        from types import ModuleType

        case = self.pricing_invoice("UGX", 3700)
        actions, _ = case._import_pricing_actions()
        erp_utils = ModuleType("erpnext.setup.utils")
        erp_utils.get_exchange_rate = lambda source, target, date: 0
        db = case.h.frappe.db
        original_get_value = db.get_value

        def get_value(doctype, *args, **kwargs):
            return 3700 if doctype == "Currency Exchange" else original_get_value(doctype, *args, **kwargs)

        profile = pricing.FakeDoc({"company": "Test Co", "currency": "USD"})
        with patch.dict(sys.modules, {erp_utils.__name__: erp_utils}):
            with patch.dict(db, {"get_value": get_value}):
                with self.assertRaisesRegex(pricing.FakeValidationError, "POS-FX-RATE-INVALID"):
                    actions.profile_approval_currency(profile, case.h.frappe)

    def test_service_charge_save_caps_post_redistribution_charge(self):
        case = self.pricing_invoice("UGX", 3700)
        doc = pricing.FlatGrandTotalDiscountInvoice(case.invoice)
        doc["items"][0].update(qty=1, rate=1000, amount=1000, net_amount=1000)
        doc.update(taxes=[], apply_discount_on="Grand Total", discount_amount=500)
        doc.calculate_taxes_and_totals()
        case.h.docs[("POS Invoice", doc.name)] = doc
        actions, _ = case._import_pricing_actions()

        def authorize(**kwargs):
            if kwargs["amount"] > 195000:
                raise pricing.FakePermissionError("POS-AUTH-NOT-ELIGIBLE")
            return {"reason": "SVC-WAIVE", "reason_note": "", "approver": "manager@example.com", "via_pin": True}

        with patch.object(actions, "authorize_or_perform", side_effect=authorize) as consume:
            with self.assertRaisesRegex(pricing.FakePermissionError, "POS-AUTH-NOT-ELIGIBLE"):
                actions.adjust_service_charge(
                    doc.name, "Main Till", action="change", new_rate=10,
                    reason="SVC-WAIVE", approval_token="token",
                )
        self.assertEqual(consume.call_args.kwargs["amount"], 201835)
        self.assertEqual(consume.call_args.kwargs["claim"]["new_rate"], 10)
        self.assertEqual(doc.net_total, 500)
        self.assertEqual(doc.taxes, [])
