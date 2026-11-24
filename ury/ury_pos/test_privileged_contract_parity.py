"""Contract parity tests between B5e+f frontend callers, B2a ManagerApproval, and backend endpoints."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from ury.ury_pos.test_pricing_privileged_actions import (
    FakePermissionError,
    TestPricingPrivilegedActions,
)


class TestPrivilegedContractParity(TestPricingPrivilegedActions):
    def setUp(self):
        super().setUp()
        self.h.frappe.local = SimpleNamespace(response={})
        self.h.reasons.append({
            "code": "OTHER",
            "description": "Other",
            "applies_to": "price_override\nreprint_bill\nservice_charge_waive",
            "requires_note": 1,
            "disabled": 0,
            "sort_order": 99,
        })

    def test_auth_challenge_attached_on_pos_auth_required_and_pos_profile_optional(self):
        """When POS-AUTH-REQUIRED fires, response carries exact server claim for ManagerApproval."""
        pricing, _ = self._import_pricing_actions()
        self.h.frappe.session.user = "cashier@example.com"
        with self.assertRaises(FakePermissionError) as ctx:
            # Omit pos_profile (derived from invoice)
            pricing.override_line_price(
                invoice="POS-INV-100",
                item_row_name="ITEM-ROW-1",
                new_rate=10000.0,
                reason="PRICE-MATCH",
            )
        self.assertIn("POS-AUTH-REQUIRED", str(ctx.exception))
        challenge = self.h.frappe.local.response.get("pos_auth_challenge")
        self.assertIsInstance(challenge, dict)
        self.assertEqual(challenge["privilege"], "price_override")
        self.assertEqual(challenge["claim"]["action"], "price_override")
        self.assertEqual(challenge["claim"]["new_rate"], 10000.0)
        self.assertTrue(challenge["claim"]["below_cost"])

        # Manager approves the EXACT challenge claim returned by the server
        token = self.h.mint_token(
            approver="manager@example.com",
            privilege="price_override",
            pos_profile="Main Till",
            reference_name="POS-INV-100",
            claim=challenge["claim"],
        )
        res = pricing.override_line_price(
            invoice="POS-INV-100",
            item_row_name="ITEM-ROW-1",
            new_rate=10000.0,
            reason="PRICE-MATCH",
            approval_token=token,
        )
        self.assertEqual(res["rate"], 10000.0)
        self.assertEqual(res["approver"], "manager@example.com")

    def test_list_tax_exemption_options_accepts_company_without_pos_profile(self):
        pricing, _ = self._import_pricing_actions()
        res = pricing.list_tax_exemption_options(company="Test Co")
        self.assertIn("tax_categories", res)
        self.assertIn("item_tax_templates", res)

    def test_validate_pos_reason_accepts_manager_approval_formatted_other_note(self):
        """ManagerApproval.tsx formats requires_note=1 reasons as 'Other: <note>'."""
        _, auth = self._import_pricing_actions()
        code, note = auth.validate_pos_reason(
            "reprint_bill",
            "Main Till",
            "Other: Guest lost printed copy",
            "",
            required=True,
        )
        self.assertEqual(code, "OTHER")
        self.assertEqual(note, "Guest lost printed copy")

    def test_authorize_or_perform_consumes_manager_approval_claim_with_reason_field(self):
        """ManagerApproval.tsx adds finalClaim.reason = resolvedReasonText when showReasonPicker is true."""
        _, auth = self._import_pricing_actions()
        self.h.frappe.session.user = "cashier@example.com"
        base_claim = {"action": "reprint_open_bill", "invoice": "POS-INV-100", "print_number": 2}
        signed_claim = {**base_claim, "reason": "Other: Guest lost printed copy"}
        token = self.h.mint_token(
            approver="manager@example.com",
            privilege="reprint_bill",
            pos_profile="Main Till",
            reference_name="POS-INV-100",
            claim=signed_claim,
        )
        res = auth.authorize_or_perform(
            privilege="reprint_bill",
            pos_profile="Main Till",
            reference_doctype="POS Invoice",
            reference_name="POS-INV-100",
            claim=base_claim,
            token=token,
            reason="Other: Guest lost printed copy",
            require_reason=True,
        )
        self.assertTrue(res["via_pin"])
        self.assertEqual(res["approver"], "manager@example.com")
        self.assertEqual(res["reason"], "OTHER")
        self.assertEqual(res["reason_note"], "Guest lost printed copy")

    def test_service_charge_mode_override_alias_maps_to_change(self):
        pricing, _ = self._import_pricing_actions()
        self.h.frappe.session.user = "manager@example.com"
        res = pricing.adjust_service_charge(
            invoice="POS-INV-100",
            mode="override",
            rate=5.0,
            reason="PRICE-MATCH",
        )
        self.assertEqual(res["action"], "change")


if __name__ == "__main__":
    unittest.main()
