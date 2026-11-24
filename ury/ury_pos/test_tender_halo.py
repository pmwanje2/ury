"""High Amount Lockout (HALO) per tender Slice 3 (Matrix row 32, G24, G34) +
#145 R2 `cash_movements` in-place PIN/Paid-In bridge + `getPosProfile` metadata;
site-less (in-memory Frappe double, no bench required).

Matches Oracle Simphony Tender/Media HALO + Role Opt 35 (`Authorize Over HALO
Amounts on [Tender/Media] Keys`) and SAP POS tender limit parity.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from unittest.mock import patch

from ury.ury_pos.test_till_cash_controls import (
    APP,
    CASH_MOVEMENT_HOOK,
    DRAWER_EVENT_DIR,
    TILL_CONTROLS_PY,
    FakeDoc,
    PermissionDenied,
    Refused,
    Row,
    _build_env,
)

TENDER_HOOK = APP / "ury" / "hooks" / "ury_pos_tender.py"
CASH_MOVEMENTS_API = APP / "ury_pos" / "cash_movements.py"


class InvoiceDoc(FakeDoc):
    def __init__(self, data=None, store=None):
        super().__init__(data, store=store)
        self.setdefault("doctype", "POS Invoice")
        self.setdefault("taxes", [])
        self.setdefault("payments", [])
        self.setdefault("items", [])

    def set(self, field, value):
        self[field] = [Row(r) for r in (value or [])]

    def precision(self, field):
        currency = self.get("currency")
        if currency == "UGX":
            return 0
        return 2

    def calculate_taxes_and_totals(self):
        items_total = sum(float(i.get("amount") or i.get("rate") or 0) for i in (self.get("items") or []))
        taxes_total = sum(float(t.get("tax_amount") or 0) for t in (self.get("taxes") or []))
        self["net_total"] = items_total
        self["grand_total"] = items_total + taxes_total
        self["rounded_total"] = self["grand_total"]


class LoadedSlice3(unittest.TestCase):
    def setUp(self):
        self.store, self.override, self.frappe, brand_spec, brand_mod, modules = _build_env()
        self.enterContext(patch.dict(sys.modules, modules))
        brand_spec.loader.exec_module(brand_mod)

        # Load POS Drawer Event
        ctrl_spec = importlib.util.spec_from_file_location(
            "ury.ury.doctype.pos_drawer_event.pos_drawer_event", DRAWER_EVENT_DIR / "pos_drawer_event.py"
        )
        ctrl_mod = importlib.util.module_from_spec(ctrl_spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury.doctype.pos_drawer_event.pos_drawer_event": ctrl_mod}))
        ctrl_spec.loader.exec_module(ctrl_mod)
        self.store.drawer_event_cls = ctrl_mod.POSDrawerEvent

        # Load ury_cash_movement
        cm_spec = importlib.util.spec_from_file_location("ury.ury.hooks.ury_cash_movement", CASH_MOVEMENT_HOOK)
        self.cash_hook = importlib.util.module_from_spec(cm_spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury.hooks.ury_cash_movement": self.cash_hook}))
        cm_spec.loader.exec_module(self.cash_hook)
        self.store.je_hook_mod = self.cash_hook

        # Load till_controls
        tc_spec = importlib.util.spec_from_file_location("ury.ury_pos.till_controls", TILL_CONTROLS_PY)
        self.tc = importlib.util.module_from_spec(tc_spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury_pos.till_controls": self.tc}))
        tc_spec.loader.exec_module(self.tc)

        # Load ury_pos_tender
        th_spec = importlib.util.spec_from_file_location("ury.ury.hooks.ury_pos_tender", TENDER_HOOK)
        self.tender_hook = importlib.util.module_from_spec(th_spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury.hooks.ury_pos_tender": self.tender_hook}))
        th_spec.loader.exec_module(self.tender_hook)

        # Load cash_movements (#145 R2 API)
        cma_spec = importlib.util.spec_from_file_location("ury.ury_pos.cash_movements", CASH_MOVEMENTS_API)
        self.cma = importlib.util.module_from_spec(cma_spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury_pos.cash_movements": self.cma}))
        cma_spec.loader.exec_module(self.cma)

        # Add Mobile Money mode with HALO 2,000,000 and an Unlimited mode with HALO 0
        self.store.modes["Mobile Money"] = Row(
            name="Mobile Money", type="Bank", custom_overtender_rule="Refuse", custom_halo_max_amount=2000000
        )
        self.store.modes["Room Charge"] = Row(
            name="Room Charge", type="Bank", custom_overtender_rule="Refuse", custom_halo_max_amount=0
        )

    def _invoice(self, *, total=50000, payments=None, currency="UGX", pos_profile="Main Till", name="POS-INV-HALO-1"):
        doc = InvoiceDoc(
            {
                "name": name,
                "pos_profile": pos_profile,
                "company": "Hotel Co",
                "currency": currency,
                "is_return": 0,
                "items": [Row(item_code="Meal", qty=1, rate=total, amount=total)],
                "payments": [Row(p) for p in (payments or [])],
            },
            store=self.store,
        )
        doc.calculate_taxes_and_totals()
        return doc


class TestTenderHalo(LoadedSlice3):
    """Simphony HALO per tender on Cash and non-cash tenders."""

    def test_mistyped_cash_5m_for_50k_triggers_halo_refusal_for_cashier(self):
        # Cash HALO = 500,000 UGX; cashier mistypes 5,000,000 on a 50,000 bill
        self.store.current_user = "cashier@example.com"
        inv = self._invoice(total=50000, payments=[{"mode_of_payment": "Cash", "amount": 5000000}])
        with self.assertRaisesRegex(PermissionDenied, r"POS-TENDER-HALO"):
            self.tender_hook.before_validate(inv)

    def test_exact_halo_boundary_passes_and_one_ugx_over_refuses(self):
        self.store.current_user = "cashier@example.com"
        at_limit = self._invoice(total=500000, payments=[{"mode_of_payment": "Cash", "amount": 500000}])
        self.tender_hook.before_validate(at_limit)  # 500,000 == HALO -> OK

        one_over = self._invoice(total=500001, payments=[{"mode_of_payment": "Cash", "amount": 500001}])
        with self.assertRaisesRegex(PermissionDenied, r"POS-TENDER-HALO"):
            self.tender_hook.before_validate(one_over)

    def test_other_tender_halo_enforced_on_card_and_mobile_money(self):
        # Mobile Money HALO = 2,000,000; Card HALO = 2,000,000
        self.store.current_user = "cashier@example.com"
        momo = self._invoice(total=2500000, payments=[{"mode_of_payment": "Mobile Money", "amount": 2500000}])
        with self.assertRaisesRegex(PermissionDenied, r"POS-TENDER-HALO"):
            self.tender_hook.before_validate(momo)

        card = self._invoice(total=2100000, payments=[{"mode_of_payment": "Card", "amount": 2100000}])
        with self.assertRaisesRegex(PermissionDenied, r"POS-TENDER-HALO"):
            self.tender_hook.before_validate(card)

    def test_adversarial_split_same_mode_cannot_evade_halo(self):
        # Two Cash rows of 300,000 each = 600,000 > Cash HALO 500,000
        self.store.current_user = "cashier@example.com"
        split = self._invoice(
            total=600000,
            payments=[
                {"mode_of_payment": "Cash", "amount": 300000},
                {"mode_of_payment": "Cash", "amount": 300000},
            ],
        )
        with self.assertRaisesRegex(PermissionDenied, r"POS-TENDER-HALO"):
            self.tender_hook.before_validate(split)

    def test_over_halo_succeeds_with_manager_pin_token_and_rejects_changed_amount(self):
        self.store.current_user = "cashier@example.com"
        inv = self._invoice(total=50000, payments=[{"mode_of_payment": "Cash", "amount": 5000000}])
        claim = {
            "action": "over_halo_tender",
            "invoice": "POS-INV-HALO-1",
            "pos_profile": "Main Till",
            "tenders": [{"mode_of_payment": "Cash", "amount": 5000000}],
        }
        self.override.mint(
            "tok-halo-1",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="over_halo_tender",
            pos_profile="Main Till",
            reference_name="POS-INV-HALO-1",
            claim=claim,
        )
        self.tc.apply_halo_approval_token(inv, "tok-halo-1")
        self.tender_hook.before_validate(inv)
        self.assertEqual(inv.custom_halo_approver, "manager@example.com")
        self.assertEqual(self.store.current_user, "cashier@example.com")

        # Tampering the tender amount AFTER approval was stamped on flags must be refused
        inv.payments[0].amount = 6000000
        with self.assertRaisesRegex(PermissionDenied, r"POS-TENDER-HALO"):
            self.tender_hook.before_validate(inv)

    def test_adversarial_no_halo_limit_set_and_negative_halo_config_and_negative_tender(self):
        self.store.current_user = "cashier@example.com"
        # Room Charge has custom_halo_max_amount = 0 -> no HALO cap
        unlimited = self._invoice(total=9900000, payments=[{"mode_of_payment": "Room Charge", "amount": 9900000}])
        self.tender_hook.before_validate(unlimited)

        # Negative tender amount refused directly in before_validate
        neg = self._invoice(total=50000, payments=[{"mode_of_payment": "Cash", "amount": -1000}])
        with self.assertRaisesRegex(Refused, r"POS-TENDER-NEGATIVE"):
            self.tender_hook.before_validate(neg)

        # Negative custom_halo_max_amount on Mode of Payment refused on validate
        bad_mop = Row(name="Cash", custom_halo_max_amount=-10)
        with self.assertRaisesRegex(Refused, r"POS-TENDER-HALO-INVALID"):
            self.tender_hook.validate_mode_of_payment_halo(bad_mop)

    def test_adversarial_zero_decimal_ugx_fractional_tender_refused(self):
        self.store.current_user = "cashier@example.com"
        frac = self._invoice(total=50000, payments=[{"mode_of_payment": "Cash", "amount": 50000.5}], currency="UGX")
        with self.assertRaisesRegex(Refused, r"POS-CASH-PRECISION"):
            self.tender_hook.before_validate(frac)

    def test_cash_movements_api_bridges_paid_in_reference_and_in_place_pin(self):
        # Verify #145 R2's cash_movements.record_till_cash_movement supports Paid In, reference, and approval_token
        self.store.current_user = "cashier@example.com"
        claim = {
            "action": "paid_out",
            "movement_type": "Paid Out",
            "amount": 100000,
            "reason": "Kitchen gas",
            "reference": "VCH-99",
            "paid_out_target": "expense",
            "pos_opening_entry": "OPEN-MAIN-1",
        }
        self.override.mint(
            "tok-cma-1",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="paid_out",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=claim,
        )
        res = self.cma.record_till_cash_movement(
            pos_profile="Main Till",
            movement_type="Paid Out",
            amount=100000,
            reason="Kitchen gas",
            reference="VCH-99",
            approval_token="tok-cma-1",
        )
        self.assertEqual(res["docstatus"], 1)
        self.assertEqual(res["approver"], "manager@example.com")
        self.assertEqual(res["reference"], "VCH-99")


if __name__ == "__main__":
    unittest.main()
