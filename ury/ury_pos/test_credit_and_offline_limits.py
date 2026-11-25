"""Site-less contracts for B5c+d: room-charge limits/No Post, on-account
credit-limit overrides (no silent bypass), and data-driven offline tenders
with per-till floor limits (matrix rows 33, 34, 38)."""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from ury import hooks
from ury.ury.doctype.ury_order import ury_order as order
from ury.ury.hooks import ury_pos_invoice
from ury.ury_pos import api

ROOT = Path(__file__).resolve().parents[1]


def _fixture_fields():
    rows = json.loads((ROOT / "fixtures" / "custom_field.json").read_text())
    return {row["name"]: row for row in rows}


def _exported_names():
    fixture = next(
        row for row in hooks.fixtures if isinstance(row, dict) and row.get("doctype") == "Custom Field"
    )
    return next(row[2] for row in fixture["filters"] if row[:2] == ["name", "in"])


class TestCreditAndOfflineCustomFields(unittest.TestCase):
    def test_required_custom_fields_are_declared_and_exported(self):
        fields = _fixture_fields()
        exported = _exported_names()
        expected = [
            ("Mode of Payment-custom_allow_offline", "Mode of Payment", "custom_allow_offline", "Check"),
            (
                "Mode of Payment-custom_offline_requires_reference",
                "Mode of Payment",
                "custom_offline_requires_reference",
                "Check",
            ),
            ("POS Profile-custom_offline_tender_limit", "POS Profile", "custom_offline_tender_limit", "Currency"),
            ("POS Invoice-custom_room_charge_stay", "POS Invoice", "custom_room_charge_stay", "Data"),
            ("POS Invoice-custom_override_approver", "POS Invoice", "custom_override_approver", "Link"),
            ("POS Invoice-custom_override_reason", "POS Invoice", "custom_override_reason", "Data"),
        ]
        for key, dt, fieldname, fieldtype in expected:
            with self.subTest(field=key):
                row = fields.get(key, {})
                self.assertEqual(row.get("dt"), dt)
                self.assertEqual(row.get("fieldname"), fieldname)
                self.assertEqual(row.get("fieldtype"), fieldtype)
                self.assertIn(key, exported)


class TestPosProfileOfflineTenderData(unittest.TestCase):
    """getPosProfile and getPosProfileFull expose Mode of Payment offline flags
    and per-till offline_tender_limit from data (never tender-name regex)."""

    def setUp(self):
        self.framework = self.enterContext(patch.object(api, "frappe"))
        self.enterContext(patch.object(api, "getBranch", return_value="Branch A"))
        self.enterContext(patch.object(api, "get_default_customer_config", return_value={}))
        self.framework.session.user = "cashier@example.com"
        self.framework.db.exists.return_value = "Till A"
        self.framework.get_single.return_value = frappe._dict(disable_rounded_total=0)
        self.meta_company = MagicMock()
        self.meta_company.has_field.return_value = True
        self.framework.get_meta.side_effect = lambda dt: self.meta_company
        self.profile = frappe._dict(
            name="Till A",
            branch="Branch A",
            warehouse="Stores",
            company="Company A",
            country="Uganda",
            currency="UGX",
            table_attention_time=10,
            print_format="Receipt",
            paid_limit=0,
            custom_enable_discount=0,
            custom_enable_multiple_cashier=0,
            custom_edit_order_type=0,
            custom_enable_kot_reprint=0,
            custom_offline_tender_limit=250000,
            ury_offline_capture=1,
            qz_print=0,
            applicable_for_users=[frappe._dict(user="cashier@example.com")],
            printer_settings=[],
            role_allowed_for_billing=[],
            role_restricted_for_table_order=[],
            transfer_role_permissions=[],
            payments=[
                frappe._dict(mode_of_payment="Cash", default=1),
                frappe._dict(mode_of_payment="Airtel Money", default=0),
                frappe._dict(mode_of_payment="MTN Push", default=0),
                frappe._dict(mode_of_payment="Room Charge", default=0),
                frappe._dict(mode_of_payment="Visa Card", default=0),
            ],
        )
        self.framework.get_doc.return_value = self.profile

        self.mop_meta = {
            "Cash": frappe._dict(type="Cash", custom_allow_offline=1, custom_offline_requires_reference=0),
            "Airtel Money": frappe._dict(type="Bank", custom_allow_offline=1, custom_offline_requires_reference=1),
            "MTN Push": frappe._dict(type="Phone", custom_allow_offline=1, custom_offline_requires_reference=0),
            "Room Charge": frappe._dict(type="General", custom_allow_offline=1, custom_offline_requires_reference=0),
            "Visa Card": frappe._dict(type="Bank", custom_allow_offline=0, custom_offline_requires_reference=0),
        }

        def db_get_value(dt, filters, field=None, as_dict=False, **kw):
            if dt == "Company" and field == "custom_guest_ledger_clearing":
                return "Guest Ledger Clearing - CA"
            if dt == "Mode of Payment Account":
                parent = filters.get("parent") if isinstance(filters, dict) else None
                return "Guest Ledger Clearing - CA" if parent == "Room Charge" else f"{parent} - CA"
            if dt == "Mode of Payment":
                row = self.mop_meta.get(filters)
                if not row:
                    return None
                if as_dict:
                    return row
                return row.get(field) if isinstance(field, str) else row
            return None

        self.framework.db.get_value.side_effect = db_get_value

    def test_profile_exposes_offline_modes_and_limit_excluding_room_charge_and_phone(self):
        result = api.getPosProfile()
        self.assertEqual(result.get("offline_tender_limit"), 250000)
        modes = {row["mode_of_payment"]: row for row in result.get("offline_payment_modes") or []}
        self.assertTrue(modes["Cash"]["allow_offline"])
        self.assertFalse(modes["Cash"]["requires_reference"])
        # Airtel Money works through data flag without any name regex
        self.assertTrue(modes["Airtel Money"]["allow_offline"])
        self.assertTrue(modes["Airtel Money"]["requires_reference"])
        # Live Phone push and Room Charge are NEVER offline-eligible even if flagged
        self.assertFalse(modes["MTN Push"]["allow_offline"])
        self.assertFalse(modes["Room Charge"]["allow_offline"])
        self.assertFalse(modes["Visa Card"]["allow_offline"])

        full = api.getPosProfileFull("Till A")
        self.assertEqual(full.get("offline_tender_limit"), 250000)


class TestOnAccountOverLimitGate(unittest.TestCase):
    """On account uses native ERPNext Customer Credit Limit / bypass_credit_limit_check /
    credit_controller, NEVER silently bypasses over limit, and requires consumed
    `on_account_over_limit` approval."""

    def setUp(self):
        self.enterContext(patch.object(order, "_", lambda msg: msg))
        self.enterContext(
            patch.object(frappe, "get_system_settings", return_value="Commercial Rounding")
        )
        self.f = self.enterContext(patch.object(order, "frappe"))
        self.enterContext(patch("ury.ury_pos.credit.frappe", self.f))
        self.enterContext(patch.object(api, "getBranch", return_value="West"))
        self.f.PermissionError = PermissionError
        self.f.ValidationError = ValueError
        self.f.throw.side_effect = self._throw
        self.f.get_roles.return_value = ["URY Cashier"]
        self.f.session.user = "cashier@example.com"
        self.f.utils.cint.side_effect = lambda n: int(n or 0)
        self.f.flags = frappe._dict()
        self.profile = frappe._dict(
            name="Till A",
            branch="West",
            customer="Walk-in",
            company="Hotel",
            currency="UGX",
            allow_partial_payment=1,
            applicable_for_users=[],
            custom_order_type_customers=[],
        )
        self.f.get_cached_doc.return_value = self.profile
        self.f.get_cached_value.side_effect = lambda dt, name, field=None: (
            "UGX" if dt == "Company" and field == "default_currency" else None
        )
        self.f.get_single_value.return_value = "Accounts Manager"
        self.invoice = self._bill("INV-1", 100)
        self.f.get_doc.side_effect = lambda dt, name, **kw: (
            self.profile if dt == "POS Profile" else self.invoice
        )
        self.enterContext(patch.object(order, "get_order_invoice", return_value=self.invoice))
        self.enterContext(patch.object(order, "_enforce_order_access"))
        self.enterContext(patch.object(order, "getBranch", return_value="West"))
        self.gl = self.enterContext(
            patch(
                "erpnext.selling.doctype.customer.customer.get_customer_outstanding",
                return_value=80,
            )
        )
        self.limit = 150
        self.bypass_flag = 0
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute(
            "CREATE TABLE `tabPOS Invoice` (name, customer, company, docstatus, consolidated_invoice, status, outstanding_amount, is_return, rounded_total, grand_total, paid_amount, conversion_rate)"
        )
        self.db.create_function("least", -1, min)
        self.f.db.sql.side_effect = self._sql
        self.f.db.get_value.side_effect = self._value
        self.f.has_permission.return_value = True

    @staticmethod
    def _throw(msg, exc=ValueError, **kw):
        raise exc(msg)

    def _bill(self, name, total, currency="UGX", conversion_rate=1):
        bill = MagicMock()
        bill.name = name
        bill.branch = "West"
        bill.customer = "AC-1"
        bill.company = "Hotel"
        bill.currency = currency
        bill.conversion_rate = conversion_rate
        bill.pos_profile = "Till A"
        bill.restaurant_table = None
        bill.custom_merged_pos_invoice = None
        bill.docstatus = 0
        bill.precision.return_value = 2
        bill.items = []
        bill.payments = []
        bill.flags = frappe._dict()
        bill.rounded_total = bill.grand_total = total
        bill.set.side_effect = lambda key, value: setattr(bill, key, value)
        bill.get.side_effect = lambda key, default=None: vars(bill).get(key, default)
        bill.append.side_effect = lambda key, row: getattr(bill, key).append(frappe._dict(row))
        bill.submit.side_effect = lambda: setattr(bill, "docstatus", 1)
        return bill

    def _value(self, dt, filters, field=None, as_dict=False, **kw):
        if dt == "Customer Credit Limit":
            if filters.get("parenttype") == "Customer":
                if isinstance(field, (list, tuple)) or as_dict:
                    return frappe._dict(
                        credit_limit=self.limit,
                        bypass_credit_limit_check=self.bypass_flag,
                    )
                return self.limit
            return None
        if dt == "POS Invoice" and field == "docstatus":
            return self.invoice.docstatus
        return None

    def _sql(self, query, args=(), **kw):
        if "FROM `tabCustomer`" in query:
            return [("AC-1", 0)]
        if "SUM(" in query.upper():
            return self.db.execute(query.replace("%s", "?").replace("FOR UPDATE", ""), args).fetchall()
        return [("INV-1",)]

    def test_bypass_credit_limit_check_and_credit_controller_never_silently_bypass(self):
        # Exposure: GL 80 + bill 100 = 180 > limit 150
        self.bypass_flag = 1
        self.f.get_roles.return_value = ["URY Manager", "Accounts Manager"]
        with self.assertRaisesRegex(ValueError, r"\[POS-CREDIT-004\]") as caught:
            order.make_invoice(
                "AC-1",
                [{"mode_of_payment": "Cash", "amount": 0}],
                "cashier@example.com",
                "Till A",
                "cashier@example.com",
                invoice="INV-1",
                submit_invoice=1,
            )
        self.assertIn("POS-AUTH-REQUIRED", str(caught.exception))
        self.invoice.submit.assert_not_called()

    def test_over_limit_succeeds_with_consumed_on_account_over_limit_approval(self):
        consumed = frappe._dict(
            approver="mgr@example.com",
            amount=100.0,
            claim={"reason": "VIP corporate banquet"},
        )
        with patch("ury.ury_pos.override.consume", return_value=consumed) as mock_consume:
            out = order.make_invoice(
                "AC-1",
                [{"mode_of_payment": "Cash", "amount": 0}],
                "cashier@example.com",
                "Till A",
                "cashier@example.com",
                invoice="INV-1",
                submit_invoice=1,
                on_account_approval="valid-token-123",
                on_account_reason="VIP corporate banquet",
            )
        self.assertEqual(out["status"], "Submitted")
        mock_consume.assert_called_once()
        args = mock_consume.call_args.args
        self.assertEqual(args[0], "valid-token-123")
        self.assertEqual(args[1], "on_account_over_limit")
        self.assertEqual(args[2], "Till A")
        self.assertEqual(args[3], "INV-1")
        self.assertEqual(self.invoice.custom_override_approver, "mgr@example.com")
        self.assertEqual(self.invoice.custom_override_reason, "VIP corporate banquet")

    def test_foreign_currency_invoice_converts_to_company_currency_limit(self):
        # Invoice is 50 USD @ 3700 UGX/USD = 185,000 UGX against 100,000 UGX limit.
        # Raw numeric comparison (50 <= 100000) would wrongly pass!
        self.invoice = self._bill("INV-FX", 50, currency="USD", conversion_rate=3700)
        self.profile.currency = "USD"
        self.gl.return_value = 0
        self.limit = 100000
        with patch.object(order, "get_order_invoice", return_value=self.invoice):
            with self.assertRaisesRegex(ValueError, r"\[POS-CREDIT-004\]"):
                order.make_invoice(
                    "AC-1",
                    [{"mode_of_payment": "Cash", "amount": 0}],
                    "cashier@example.com",
                    "Till A",
                    "cashier@example.com",
                    invoice="INV-FX",
                    submit_invoice=1,
                )
        self.invoice.submit.assert_not_called()

    def test_two_step_make_invoice_then_submit_order_invoice_consumes_token_once(self):
        # Standard !offlineCapable tills call make_invoice(submit_invoice=0) then submit_order_invoice().
        # The single-use token must be consumed ONCE (on submit), never burned on draft save.
        used_tokens = set()

        def single_use_consume(token, privilege, pos_profile, ref_name, claim):
            if token in used_tokens:
                raise ValueError("POS-AUTH-EXPIRED: already used")
            used_tokens.add(token)
            return frappe._dict(approver="mgr@example.com", amount=100.0, claim=claim)

        self.enterContext(patch.object(order, "_check_held_before_settlement"))
        with patch("ury.ury_pos.override.consume", side_effect=single_use_consume) as mock_consume:
            draft = order.make_invoice(
                "AC-1",
                [{"mode_of_payment": "Cash", "amount": 0}],
                "cashier@example.com",
                "Till A",
                "cashier@example.com",
                invoice="INV-1",
                submit_invoice=0,
                on_account_approval="single-use-acct-tok",
                on_account_reason="Two-step VIP",
            )
            self.assertEqual(draft["status"], "Draft")
            self.assertEqual(mock_consume.call_count, 0)

            submitted = order.submit_order_invoice(
                "INV-1",
                on_account_approval="single-use-acct-tok",
                on_account_reason="Two-step VIP",
            )
        self.assertEqual(submitted["status"], "Submitted")
        self.assertEqual(mock_consume.call_count, 1)
        self.assertEqual(self.invoice.custom_override_approver, "mgr@example.com")

    def test_three_decimal_currency_kwd_claim_preserves_fils_precision(self):
        self.invoice = self._bill("INV-KWD", 12.345, currency="KWD", conversion_rate=1)
        self.invoice.precision.return_value = 3
        self.profile.currency = "KWD"
        self.f.get_cached_value.side_effect = lambda dt, name, field=None: (
            "KWD" if dt == "Company" and field == "default_currency" else None
        )
        self.gl.return_value = 90.0
        self.limit = 100.0
        consumed = frappe._dict(approver="mgr@example.com", amount=12.345, claim={"reason": "KWD VIP"})
        with patch.object(order, "get_order_invoice", return_value=self.invoice), \
                patch("ury.ury_pos.override.consume", return_value=consumed) as mock_consume:
            out = order.make_invoice(
                "AC-1",
                [{"mode_of_payment": "Cash", "amount": 0}],
                "cashier@example.com",
                "Till A",
                "cashier@example.com",
                invoice="INV-KWD",
                submit_invoice=1,
                on_account_approval="kwd-acct-tok",
                on_account_reason="KWD VIP",
            )
        self.assertEqual(out["status"], "Submitted")
        self.assertEqual(mock_consume.call_args.args[4]["proposed"], 12.345)

    def test_foreign_currency_on_account_override_claim_uses_company_currency_and_precision(self):
        # 20.00 USD @ 3700 UGX/USD = 74,000 UGX against 50,000 UGX company limit.
        # Claim currency & precision MUST be Company default_currency ("UGX", 0dp), not invoice currency ("USD")!
        self.invoice = self._bill("INV-FX-OA", 20.0, currency="USD", conversion_rate=3700)
        self.profile.currency = "USD"
        self.gl.return_value = 0
        self.limit = 50000
        consumed = frappe._dict(approver="mgr@example.com", amount=74000, claim={"reason": "FX VIP banquet"})
        with patch.object(order, "get_order_invoice", return_value=self.invoice), \
                patch("ury.ury_pos.override.consume", return_value=consumed) as mock_consume:
            out = order.make_invoice(
                "AC-1",
                [{"mode_of_payment": "Cash", "amount": 0}],
                "cashier@example.com",
                "Till A",
                "cashier@example.com",
                invoice="INV-FX-OA",
                submit_invoice=1,
                on_account_approval="fx-acct-tok",
                on_account_reason="FX VIP banquet",
            )
        self.assertEqual(out["status"], "Submitted")
        claim = mock_consume.call_args.args[4]
        self.assertEqual(claim["currency"], "UGX")
        self.assertEqual(claim["proposed"], 74000)
        self.assertEqual(claim["limit"], 50000)
        self.assertEqual(claim["reason"], "FX VIP banquet")

    def test_credit_accounts_exposes_conversion_rate_for_fx_pos_profile(self):
        self.profile.currency = "USD"
        self.f.get_list.return_value = [
            frappe._dict(name="AC-1", customer_name="Acme FX", limit_amount=50000.0)
        ]
        self.f.utils.flt.side_effect = lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0)
        self.gl.return_value = 10000.0
        with patch.object(api, "frappe", self.f), \
                patch("erpnext.setup.utils.get_exchange_rate", return_value=3700.0):
            rows = api.credit_accounts("Till A", "Acme")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["currency"], "UGX")
        self.assertEqual(rows[0]["conversion_rate"], 3700.0)


class TestRoomChargeServerGate(unittest.TestCase):
    """Server-side room charge gate in make_invoice: calls configured verifier,
    fails closed if hook missing/raising, requires consumed `room_charge_over_limit`
    for over-limit / No Post, and hard-refuses checked-out / wrong-property / house-account."""

    def setUp(self):
        self.enterContext(patch.object(order, "_", lambda msg: msg))
        self.f = self.enterContext(patch.object(order, "frappe"))
        self.f.PermissionError = PermissionError
        self.f.ValidationError = ValueError
        self.f.throw.side_effect = lambda msg, exc=ValueError, **kw: (_ for _ in ()).throw(exc(msg))
        self.f.session = SimpleNamespace(user="cashier@example.com")
        self.f.utils.cint.side_effect = lambda n: int(n or 0)
        self.f.flags = frappe._dict()
        self.profile = frappe._dict(
            name="Till A",
            branch="Branch A",
            company="Hotel Co",
            currency="UGX",
            customer="Walk-in",
            custom_room_lookup_method="kamra.pos_interface.verify_room_charge",
            payments=[frappe._dict(mode_of_payment="Room Charge")],
        )
        self.f.get_cached_doc.return_value = self.profile
        self.invoice = MagicMock()
        self.invoice.name = "INV-RC-1"
        self.invoice.docstatus = 0
        self.invoice.pos_profile = "Till A"
        self.invoice.company = "Hotel Co"
        self.invoice.currency = "UGX"
        self.invoice.rounded_total = self.invoice.grand_total = 1500
        self.invoice.custom_merged_pos_invoice = None
        self.invoice.restaurant_table = None
        self.invoice.payments = []
        self.invoice.items = []
        self.invoice.flags = frappe._dict()
        self.invoice.precision.return_value = 2
        self.invoice.get.side_effect = lambda k, d=None: vars(self.invoice).get(k, d)
        self.invoice.set.side_effect = lambda k, v: setattr(self.invoice, k, v)
        self.invoice.append.side_effect = lambda k, row: getattr(self.invoice, k).append(frappe._dict(row))
        self.enterContext(patch.object(order, "get_order_invoice", return_value=self.invoice))
        self.enterContext(patch.object(order, "_submit_and_release_tables"))
        self.enterContext(patch.object(order, "_lock_settlement_customer", return_value=0))
        self.enterContext(patch.object(order, "_enforce_on_account"))
        self.enterContext(patch.object(order, "_check_held_before_settlement"))
        self.f.get_value.return_value = "Dine In"
        self.f.get_meta.return_value = MagicMock(has_field=MagicMock(return_value=True))

        def db_get_value(dt, filters, field=None, **kw):
            if dt == "Company" and field == "custom_guest_ledger_clearing":
                return "Guest Ledger Clearing - HC"
            if dt == "Mode of Payment Account":
                return "Guest Ledger Clearing - HC"
            if dt == "POS Invoice" and field == "docstatus":
                return 0
            return None

        self.f.db.get_value.side_effect = db_get_value

    def _pay(self, **extra):
        return order.make_invoice(
            customer="Walk-in",
            payments=[{"mode_of_payment": "Room Charge", "amount": 1500}],
            cashier="cashier@example.com",
            pos_profile="Till A",
            owner="cashier@example.com",
            invoice="INV-RC-1",
            room_number="101",
            **extra,
        )

    def test_over_limit_requires_room_charge_over_limit_approval_and_consumes_it(self):
        verifier = MagicMock(return_value={
            "ok": True,
            "reservation": "RES-101",
            "room_number": "101",
            "guest_name": "Asha R.",
            "posting_allowed": False,
            "no_post": 0,
            "limit": 1000,
            "balance": 0,
            "available": 1000,
            "currency": "UGX",
            "override_required": True,
            "reason_code": "POS-RC-OVER-LIMIT",
            "reason": "Charge exceeds room charge limit.",
        })
        with patch("ury.ury_pos.room_charge._resolve_verifier", return_value=verifier):
            with self.assertRaisesRegex(PermissionError, "POS-AUTH-REQUIRED"):
                self._pay(room_charge_stay="RES-101")

            consumed = frappe._dict(
                approver="duty@example.com",
                amount=1500.0,
                claim={"reason": "VIP guest approved"},
            )
            with patch("ury.ury_pos.override.consume", return_value=consumed) as mock_consume:
                out = self._pay(
                    room_charge_stay="RES-101",
                    room_charge_approval="rc-tok-1",
                    room_charge_reason="VIP guest approved",
                )
            self.assertEqual(out["status"], "Submitted")
            mock_consume.assert_called_once()
            self.assertEqual(mock_consume.call_args.args[1], "room_charge_over_limit")
            self.assertEqual(self.invoice.custom_override_approver, "duty@example.com")
            self.assertEqual(self.invoice.custom_room_charge_stay, "RES-101")

    def test_checked_out_or_house_account_hard_refuses_even_with_approval(self):
        verifier = MagicMock(return_value={
            "ok": False,
            "posting_allowed": False,
            "override_required": False,
            "reason_code": "POS-RC-NOT-IN-HOUSE",
            "reason": "Guest is no longer checked in.",
        })
        with patch("ury.ury_pos.room_charge._resolve_verifier", return_value=verifier), \
                patch("ury.ury_pos.override.consume") as mock_consume:
            with self.assertRaisesRegex(ValueError, "POS-RC-NOT-IN-HOUSE|checked in"):
                self._pay(
                    room_charge_stay="RES-101",
                    room_charge_approval="rc-tok-1",
                    room_charge_reason="Try override",
                )
            mock_consume.assert_not_called()

    def test_missing_or_raising_room_charge_hook_fails_closed(self):
        with patch("ury.ury_pos.room_charge._resolve_verifier", side_effect=ImportError("missing")):
            with self.assertRaises(ValueError):
                self._pay()
        with patch("ury.ury_pos.room_charge._resolve_verifier", return_value=MagicMock(side_effect=RuntimeError("boom"))):
            with self.assertRaises(ValueError):
                self._pay()

    def test_two_step_make_invoice_then_submit_order_invoice_consumes_room_charge_token_once(self):
        verifier = MagicMock(return_value={
            "ok": True,
            "reservation": "RES-101",
            "room_number": "101",
            "guest_name": "Asha R.",
            "posting_allowed": False,
            "no_post": 0,
            "limit": 1000,
            "balance": 0,
            "available": 1000,
            "currency": "UGX",
            "override_required": True,
            "reason_code": "POS-RC-OVER-LIMIT",
            "reason": "Charge exceeds room charge limit.",
        })
        used_tokens = set()

        def single_use_consume(token, privilege, pos_profile, ref_name, claim):
            if token in used_tokens:
                raise ValueError("POS-AUTH-EXPIRED: already used")
            used_tokens.add(token)
            return frappe._dict(approver="duty@example.com", amount=1500.0, claim=claim)

        self.f.get_doc.side_effect = lambda dt, name, **kw: self.profile if dt == "POS Profile" else self.invoice
        with patch("ury.ury_pos.room_charge._resolve_verifier", return_value=verifier), \
                patch("ury.ury_pos.override.consume", side_effect=single_use_consume) as mock_consume:
            draft = self._pay(
                submit_invoice=0,
                room_charge_stay="RES-101",
                room_charge_approval="rc-2step-tok",
                room_charge_reason="VIP room dinner",
            )
            self.assertEqual(draft["status"], "Draft")
            self.assertEqual(mock_consume.call_count, 0)

            submitted = order.submit_order_invoice(
                "INV-RC-1",
                room_charge_approval="rc-2step-tok",
                room_charge_reason="VIP room dinner",
            )
        self.assertEqual(submitted["status"], "Submitted")
        self.assertEqual(mock_consume.call_count, 1)
        self.assertEqual(self.invoice.custom_override_approver, "duty@example.com")

    def test_three_decimal_kwd_room_charge_claim_preserves_fils_precision(self):
        self.profile.currency = "KWD"
        self.invoice.currency = "KWD"
        self.invoice.rounded_total = self.invoice.grand_total = 12.345
        verifier = MagicMock(return_value={
            "ok": True,
            "reservation": "RES-KWD",
            "room_number": "101",
            "guest_name": "Nadia K.",
            "posting_allowed": False,
            "no_post": 0,
            "limit": 10.0,
            "balance": 0.0,
            "available": 10.0,
            "currency": "KWD",
            "override_required": True,
            "reason_code": "POS-RC-OVER-LIMIT",
            "reason": "Exceeds limit.",
        })
        consumed = frappe._dict(approver="duty@example.com", amount=12.345, claim={"reason": "KWD room"})
        with patch("ury.ury_pos.room_charge._resolve_verifier", return_value=verifier), \
                patch("ury.ury_pos.override.consume", return_value=consumed) as mock_consume:
            out = order.make_invoice(
                customer="Walk-in",
                payments=[{"mode_of_payment": "Room Charge", "amount": 12.345}],
                cashier="cashier@example.com",
                pos_profile="Till A",
                owner="cashier@example.com",
                invoice="INV-RC-1",
                room_number="101",
                room_charge_stay="RES-KWD",
                room_charge_approval="rc-kwd-tok",
                room_charge_reason="KWD room",
            )
        self.assertEqual(out["status"], "Submitted")
        self.assertEqual(mock_consume.call_args.args[4]["amount"], 12.345)

    def test_blank_room_lookup_method_fails_closed_when_room_charge_tender_is_positive(self):
        for blank in ("", "   ", None):
            with self.subTest(method=blank):
                self.profile.custom_room_lookup_method = blank
                with self.assertRaisesRegex(ValueError, r"POS-RC-UNCONFIGURED") as caught:
                    self._pay(room_charge_stay="RES-101")
                msg = str(caught.exception)
                for vendor in ("ury", "kamra", "frappe", "erpnext", "huf"):
                    self.assertNotIn(vendor, msg.lower())

    def test_foreign_currency_room_charge_uses_property_currency_amount_in_claim_and_override_flag(self):
        # 20.00 USD room charge against UGX property (74,000 UGX > 50,000 UGX limit).
        # Both override.consume claim AND frappe.flags.kamra_room_charge_override must carry
        # property-currency amount (74000 UGX), never unconverted 20.0 USD!
        self.profile.currency = "USD"
        self.invoice.currency = "USD"
        self.invoice.conversion_rate = 3700
        self.invoice.rounded_total = self.invoice.grand_total = 20.0
        verifier = MagicMock(return_value={
            "ok": True,
            "reservation": "RES-101",
            "room_number": "101",
            "guest_name": "Grace O.",
            "posting_allowed": False,
            "no_post": 0,
            "limit": 50000,
            "balance": 0,
            "available": 50000,
            "currency": "UGX",
            "charge_amount": 74000,
            "override_required": True,
            "reason_code": "POS-RC-OVER-LIMIT",
            "reason": "Charge (74000 UGX) exceeds the room charge limit (50000 UGX). Manager approval is required.",
        })
        consumed = frappe._dict(approver="duty@example.com", amount=74000, claim={"reason": "VIP FX room"})
        observed_flags = []

        def capture_submit(*args, **kwargs):
            observed_flags.append(dict(getattr(self.f.flags, "kamra_room_charge_override", None) or {}))

        with patch("ury.ury_pos.room_charge._resolve_verifier", return_value=verifier), \
                patch("ury.ury_pos.override.consume", return_value=consumed) as mock_consume, \
                patch.object(order, "_submit_and_release_tables", side_effect=capture_submit):
            out = order.make_invoice(
                customer="Walk-in",
                payments=[{"mode_of_payment": "Room Charge", "amount": 20.0}],
                cashier="cashier@example.com",
                pos_profile="Till A",
                owner="cashier@example.com",
                invoice="INV-RC-1",
                room_number="101",
                room_charge_stay="RES-101",
                room_charge_approval="rc-fx-tok",
                room_charge_reason="VIP FX room",
            )
        self.assertEqual(out["status"], "Submitted")
        claim = mock_consume.call_args.args[4]
        self.assertEqual(claim["amount"], 74000)
        self.assertEqual(claim["currency"], "UGX")
        self.assertEqual(claim["room"], "101")
        self.assertEqual(claim["reservation"], "RES-101")
        self.assertEqual(len(observed_flags), 1)
        self.assertEqual(observed_flags[0].get("amount"), 74000)
        self.assertEqual(observed_flags[0].get("currency"), "UGX")
        self.assertEqual(observed_flags[0].get("reservation"), "RES-101")
        self.assertEqual(observed_flags[0].get("approver"), "duty@example.com")


class TestOfflineTenderReplayServerGate(unittest.TestCase):
    """Server re-checks Mode of Payment.custom_allow_offline and
    POS Profile.custom_offline_tender_limit on offline replay."""

    def setUp(self):
        self.enterContext(patch.object(order, "_", lambda msg: msg))
        self.f = self.enterContext(patch.object(order, "frappe"))
        self.f.PermissionError = PermissionError
        self.f.ValidationError = ValueError
        self.f.throw.side_effect = lambda msg, exc=ValueError, **kw: (_ for _ in ()).throw(exc(msg))
        self.f.session = SimpleNamespace(user="cashier@example.com")
        self.f.utils.cint.side_effect = lambda n: int(n or 0)
        self.f.flags = frappe._dict()
        self.profile = frappe._dict(
            name="Till A",
            branch="Branch A",
            company="Hotel Co",
            currency="UGX",
            customer="Walk-in",
            ury_offline_capture=1,
            custom_offline_tender_limit=50000,
            payments=[
                frappe._dict(mode_of_payment="Airtel Money"),
                frappe._dict(mode_of_payment="Cash"),
                frappe._dict(mode_of_payment="Room Charge"),
            ],
        )
        self.f.get_cached_doc.return_value = self.profile
        self.invoice = MagicMock()
        self.invoice.name = "INV-OFF-1"
        self.invoice.docstatus = 0
        self.invoice.pos_profile = "Till A"
        self.invoice.company = "Hotel Co"
        self.invoice.currency = "UGX"
        self.invoice.rounded_total = self.invoice.grand_total = 30000
        self.invoice.ury_offline_captured_at = "2026-11-24 08:00:00"
        self.invoice.custom_comments = "Offline ref: AIR-9988"
        self.invoice.custom_merged_pos_invoice = None
        self.invoice.restaurant_table = None
        self.invoice.payments = []
        self.invoice.items = []
        self.invoice.flags = frappe._dict()
        self.invoice.precision.return_value = 2
        self.invoice.get.side_effect = lambda k, d=None: vars(self.invoice).get(k, d)
        self.invoice.set.side_effect = lambda k, v: setattr(self.invoice, k, v)
        self.invoice.append.side_effect = lambda k, row: getattr(self.invoice, k).append(frappe._dict(row))
        self.enterContext(patch.object(order, "get_order_invoice", return_value=self.invoice))
        self.enterContext(patch.object(order, "_submit_and_release_tables"))
        self.enterContext(patch.object(order, "_lock_settlement_customer", return_value=0))
        self.enterContext(patch.object(order, "_enforce_on_account"))
        self.enterContext(patch.object(order, "_check_held_before_settlement"))
        self.enterContext(patch.object(order, "_client_order_invoice", return_value=self.invoice))
        self.enterContext(patch.object(order, "_refuse_client_order_rebind"))
        self.enterContext(patch.object(order, "_record_round"))
        self.f.get_value.return_value = "Take Away"
        self.f.get_meta.return_value = MagicMock(has_field=MagicMock(return_value=True))

        self.mop_state = {
            "Airtel Money": frappe._dict(type="Bank", custom_allow_offline=1, custom_offline_requires_reference=1),
            "Cash": frappe._dict(type="Cash", custom_allow_offline=1, custom_offline_requires_reference=0),
            "Room Charge": frappe._dict(type="General", custom_allow_offline=1, custom_offline_requires_reference=0),
        }

        def db_get_value(dt, filters, field=None, as_dict=False, **kw):
            if dt == "POS Profile" and field == "ury_offline_capture":
                return self.profile.ury_offline_capture
            if dt == "Mode of Payment":
                row = self.mop_state.get(filters)
                return row if as_dict else (row.get(field) if row and isinstance(field, str) else row)
            if dt == "Company" and field == "custom_guest_ledger_clearing":
                return "Guest Ledger Clearing - HC"
            if dt == "Mode of Payment Account":
                parent = filters.get("parent") if isinstance(filters, dict) else None
                return "Guest Ledger Clearing - HC" if parent == "Room Charge" else f"{parent} - HC"
            if dt == "POS Invoice" and field == "docstatus":
                return 0
            return None

        self.f.db.get_value.side_effect = db_get_value

    def _replay(self, mode="Airtel Money", amount=30000, ref="AIR-9988", **extra):
        self.invoice.rounded_total = self.invoice.grand_total = amount
        payment = {"mode_of_payment": mode, "amount": amount}
        if ref is not None:
            payment["reference_no"] = ref
        return order.make_invoice(
            customer="Walk-in",
            payments=[payment],
            cashier="cashier@example.com",
            pos_profile="Till A",
            owner="cashier@example.com",
            invoice="INV-OFF-1",
            client_order_id="11111111-1111-4111-8111-111111111111",
            round_id="r-1",
            offline_replay=1,
            **extra,
        )

    def test_airtel_money_within_limit_passes_replay(self):
        out = self._replay(mode="Airtel Money", amount=30000, ref="AIR-9988")
        self.assertEqual(out["status"], "Submitted")

    def test_replay_refused_after_offline_flag_switched_off(self):
        self.mop_state["Airtel Money"].custom_allow_offline = 0
        with self.assertRaisesRegex(ValueError, "POS-OFFLINE-TENDER-DISABLED"):
            self._replay(mode="Airtel Money", amount=30000, ref="AIR-9988")

    def test_room_charge_always_refused_on_offline_replay(self):
        with self.assertRaisesRegex(ValueError, "POS-OFFLINE-TENDER-DISABLED"):
            self._replay(mode="Room Charge", amount=10000, ref=None)

    def test_offline_over_limit_requires_offline_tender_pin_approval_on_replay(self):
        with self.assertRaisesRegex(PermissionError, "POS-AUTH-REQUIRED"):
            self._replay(mode="Airtel Money", amount=80000, ref="AIR-9988")

        consumed = frappe._dict(
            approver="mgr@example.com",
            amount=80000.0,
            claim={"reason": "Power outage large table"},
        )
        with patch("ury.ury_pos.override.consume", return_value=consumed) as mock_consume:
            out = self._replay(
                mode="Airtel Money",
                amount=80000,
                ref="AIR-9988",
                offline_approval="off-tok-1",
                offline_reason="Power outage large table",
            )
        self.assertEqual(out["status"], "Submitted")
        mock_consume.assert_called_once()
        self.assertEqual(mock_consume.call_args.args[1], "offline_tender")
        self.assertEqual(self.invoice.custom_override_approver, "mgr@example.com")

    def test_before_submit_hook_enforces_offline_flag_for_desk_and_rest(self):
        self.mop_state["Airtel Money"].custom_allow_offline = 0
        doc = frappe._dict(
            name="INV-REST-OFF",
            pos_profile="Till A",
            company="Hotel Co",
            is_return=0,
            ury_offline_captured_at="2026-11-24 08:00:00",
            payments=[frappe._dict(mode_of_payment="Airtel Money", amount=10000)],
            flags=frappe._dict(),
        )
        with patch("ury.ury_pos.offline_tenders.frappe", self.f):
            with self.assertRaisesRegex(ValueError, "POS-OFFLINE-TENDER-DISABLED"):
                ury_pos_invoice.validate_offline_tenders_before_submit(doc)

    def test_foreign_offline_tender_exceeds_company_currency_limit(self):
        self.profile.currency = self.invoice.currency = "USD"
        self.invoice.conversion_rate = 3700
        self.profile.custom_offline_tender_limit = 100000
        get_value = self.f.db.get_value.side_effect
        self.f.db.get_value.side_effect = (
            lambda dt, *args, **kw: "UGX"
            if dt == "Company" and args[-1] == "default_currency"
            else get_value(dt, *args, **kw)
        )
        from ury.ury_pos.offline_tenders import enforce_offline_tender_replay

        with self.assertRaisesRegex(PermissionError, "POS-OFFLINE-OVER-LIMIT"):
            enforce_offline_tender_replay(
                self.invoice, self.profile, [{"mode_of_payment": "Cash", "amount": 50}],
                offline_replay=1, _frappe=self.f,
            )

    def test_three_decimal_kwd_offline_limit_enforces_fils_and_preserves_claim_precision(self):
        self.profile.currency = "KWD"
        self.invoice.currency = "KWD"
        self.profile.custom_offline_tender_limit = 100.0
        # 100.004 KWD is 4 fils above 100.000 KWD — must NOT be rounded down to 100.00!
        with self.assertRaisesRegex(PermissionError, "POS-AUTH-REQUIRED"):
            self._replay(mode="Airtel Money", amount=100.004, ref="AIR-KWD")

        consumed = frappe._dict(approver="mgr@example.com", amount=100.004, claim={"reason": "KWD outage"})
        with patch("ury.ury_pos.override.consume", return_value=consumed) as mock_consume:
            out = self._replay(
                mode="Airtel Money",
                amount=100.004,
                ref="AIR-KWD",
                offline_approval="off-kwd-tok",
                offline_reason="KWD outage",
            )
        self.assertEqual(out["status"], "Submitted")
        self.assertEqual(mock_consume.call_args.args[4]["amount"], 100.004)


class TestServerOnlyOverrideFieldsGuard(unittest.TestCase):
    """Clients must not forge server-only room-charge stay or override attribution fields via REST."""

    def test_validate_server_only_override_fields_blocks_client_forging_custom_room_charge_stay(self):
        fake_frappe = MagicMock()
        fake_frappe.flags = frappe._dict()
        fake_frappe.session = SimpleNamespace(user="cashier@example.com")
        fake_frappe.PermissionError = PermissionError
        fake_frappe._ = lambda m: m
        fake_frappe.throw.side_effect = lambda msg, exc=PermissionError, **kw: (_ for _ in ()).throw(exc(msg))

        # 1. Forged on new document via REST insert
        new_doc = MagicMock()
        new_doc.is_new.return_value = True
        new_doc.get.side_effect = lambda k, d=None: "RES-FORGED" if k == "custom_room_charge_stay" else None
        with patch.object(ury_pos_invoice, "frappe", fake_frappe):
            with self.assertRaises(PermissionError):
                ury_pos_invoice.validate_server_only_override_fields(new_doc)

        # 2. Mutated on existing draft document via REST save
        before_doc = frappe._dict(
            custom_room_charge_stay="",
            custom_override_approver="",
            custom_override_reason="",
        )
        existing_doc = MagicMock()
        existing_doc.is_new.return_value = False
        existing_doc.get_doc_before_save.return_value = before_doc
        existing_doc.get.side_effect = lambda k, d=None: "RES-FORGED" if k == "custom_room_charge_stay" else ""
        with patch.object(ury_pos_invoice, "frappe", fake_frappe):
            with self.assertRaises(PermissionError):
                ury_pos_invoice.validate_server_only_override_fields(existing_doc)

        # 3. Allowed when server flow sets pos_server_override_fields=True
        fake_frappe.flags.pos_server_override_fields = True
        with patch.object(ury_pos_invoice, "frappe", fake_frappe):
            ury_pos_invoice.validate_server_only_override_fields(existing_doc)


if __name__ == "__main__":
    unittest.main()
