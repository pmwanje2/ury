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
        self.enterContext(patch.object(order, "_validate_additional_discount", return_value=0))
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
        self.enterContext(patch.object(order, "_validate_additional_discount", return_value=0))
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


if __name__ == "__main__":
    unittest.main()
