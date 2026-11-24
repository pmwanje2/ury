"""RED/GREEN site-less unit tests for POS-PR-6 Slice 2 (G22, P0):
Foreign currency tender with local cash change, stale-rate guard,
optional manager PIN FX override, currency-wise POS Closing Entry,
and balanced multi-currency FX Vault Sweep Journal Entry.
"""

from __future__ import annotations

import datetime
import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
CUSTOM_FIELDS_JSON = ROOT / "ury" / "fixtures" / "custom_field.json"
HOOKS_PY = ROOT / "ury" / "hooks.py"
SETUP_CUSTOMIZATIONS_PY = ROOT / "ury" / "setup_customizations.py"
PRIVILEGE_RULE_JSON = ROOT / "ury" / "ury" / "doctype" / "pos_privilege_rule" / "pos_privilege_rule.json"
MERGED_PF = ROOT / "ury" / "ury" / "print_format" / "merged_pos_invoice_format" / "merged_pos_invoice_format.json"
COPY_PF = ROOT / "ury" / "ury" / "print_format" / "ury_bill_copy" / "ury_bill_copy.json"


class _FakeValidationError(Exception):
    pass


class _FakePermissionError(Exception):
    pass


class _Dict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __setattr__(self, key, value):
        self[key] = value


def _to_date(val):
    if val is None:
        return datetime.date(2026, 11, 24)
    if isinstance(val, datetime.datetime):
        return val.date()
    if isinstance(val, datetime.date):
        return val
    return datetime.date.fromisoformat(str(val)[:10])


def _ensure_frappe_stub():
    if "frappe" in sys.modules:
        return sys.modules["frappe"], False
    frappe_mod = types.ModuleType("frappe")
    frappe_mod.ValidationError = _FakeValidationError
    frappe_mod.PermissionError = _FakePermissionError
    frappe_mod._ = lambda msg: msg
    frappe_mod._dict = lambda d=None, **kw: _Dict({**(d or {}), **kw})
    frappe_mod.whitelist = lambda *a, **kw: (a[0] if a and callable(a[0]) else (lambda fn: fn))
    frappe_mod.flags = _Dict()
    frappe_mod.session = _Dict(user="cashier@example.com")
    frappe_mod.db = MagicMock()
    frappe_mod.get_all = MagicMock(return_value=[])
    frappe_mod.get_cached_doc = MagicMock()
    frappe_mod.get_cached_value = MagicMock()
    frappe_mod.get_doc = MagicMock()
    frappe_mod.new_doc = MagicMock()
    frappe_mod.get_meta = MagicMock()
    frappe_mod.get_precision = MagicMock(return_value=2)

    def _throw(msg, exc=None, **kwargs):
        raise (exc or _FakeValidationError)(msg)

    frappe_mod.throw = _throw

    utils_mod = types.ModuleType("frappe.utils")
    utils_mod.cint = lambda v: int(float(v or 0))
    utils_mod.flt = lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0)
    utils_mod.getdate = _to_date
    utils_mod.add_days = lambda d, n: _to_date(d) + datetime.timedelta(days=int(n))
    utils_mod.today = lambda: "2026-11-24"
    utils_mod.nowdate = lambda: "2026-11-24"
    utils_mod.get_datetime = lambda v: datetime.datetime.fromisoformat(str(v)) if isinstance(v, str) else v
    utils_mod.get_time = lambda v: v
    frappe_mod.utils = utils_mod

    sys.modules["frappe"] = frappe_mod
    sys.modules["frappe.utils"] = utils_mod
    return frappe_mod, True


class TestFxSchemaAndPrivilege(unittest.TestCase):
    EXPECTED_FIELDS = (
        "Mode of Payment-custom_tender_currency",
        "Mode of Payment-custom_fx_rate_type",
        "Mode of Payment Account-custom_fx_vault_account",
        "POS Profile-custom_allow_fx_rate_override",
        "POS Profile-custom_max_fx_variance_pct",
        "POS Profile-custom_fx_clearing_loss_account",
        "Sales Invoice Payment-custom_tender_currency",
        "Sales Invoice Payment-custom_tender_amount",
        "Sales Invoice Payment-custom_exchange_rate",
        "Sales Invoice Payment-custom_exchange_rate_date",
        "POS Closing Entry Detail-custom_tender_currency",
        "POS Closing Entry Detail-custom_expected_foreign_amount",
        "POS Closing Entry Detail-custom_closing_foreign_amount",
        "POS Closing Entry Detail-custom_foreign_difference",
        "POS Closing Entry-custom_fx_vault_journal_entry",
    )

    def test_all_fx_custom_fields_exist_in_fixtures_hooks_and_setup(self):
        rows = json.loads(CUSTOM_FIELDS_JSON.read_text(encoding="utf-8"))
        by_name = {row.get("name"): row for row in rows}
        hooks_text = HOOKS_PY.read_text(encoding="utf-8")
        setup_text = SETUP_CUSTOMIZATIONS_PY.read_text(encoding="utf-8")

        for name in self.EXPECTED_FIELDS:
            fieldname = name.split("-", 1)[1]
            with self.subTest(field=name):
                self.assertIn(name, by_name, f"Missing {name} in custom_field.json")
                self.assertIn(name, hooks_text, f"Missing {name} in hooks.py fixtures")
                self.assertIn(fieldname, setup_text, f"Missing {fieldname} in setup_customizations.py")

    def test_fx_rate_override_privilege_registered(self):
        schema = json.loads(PRIVILEGE_RULE_JSON.read_text(encoding="utf-8"))
        priv_field = next(f for f in schema["fields"] if f["fieldname"] == "privilege")
        self.assertIn("fx_rate_override", (priv_field.get("options") or "").splitlines())
        override_text = (ROOT / "ury" / "ury_pos" / "override.py").read_text(encoding="utf-8")
        self.assertIn('"fx_rate_override"', override_text)


class TestFxRateResolutionAndStaleGuard(unittest.TestCase):
    def setUp(self):
        self.frappe, self._stubbed = _ensure_frappe_stub()
        self.frappe.db.reset_mock()
        self.frappe.get_all.reset_mock()
        self.frappe.get_meta.reset_mock()

    def tearDown(self):
        if self._stubbed:
            for mod_name in ("frappe", "frappe.utils", "ury.ury_pos.fx"):
                sys.modules.pop(mod_name, None)

    def _fx(self):
        return importlib.import_module("ury.ury_pos.fx")

    def test_home_currency_returns_rate_one(self):
        fx = self._fx()
        res = fx.resolve_pos_exchange_rate("UGX", "UGX", "2026-11-24", "Grand Hotel")
        self.assertEqual(res["rate"], 1.0)
        self.assertTrue(res["is_home_currency"])

    def test_resolves_fresh_approved_currency_exchange_rate(self):
        fx = self._fx()
        self.frappe.db.get_single_value.return_value = 2
        self.frappe.db.get_value.side_effect = lambda dt, flt_arg, field=None: (
            "WF-CE-1" if dt == "Workflow" else None
        )
        self.frappe.get_meta.return_value = SimpleNamespace(has_field=lambda f: f == "workflow_state")
        self.frappe.get_all.return_value = [
            _Dict(name="CE-001", date=datetime.date(2026, 11, 24), exchange_rate=3700.0, workflow_state="Approved")
        ]

        res = fx.resolve_pos_exchange_rate("USD", "UGX", "2026-11-24", "Grand Hotel", rate_type="Buying")
        self.assertEqual(res["rate"], 3700.0)
        self.assertEqual(res["rate_date"], "2026-11-24")
        self.assertFalse(res["is_home_currency"])

        # Verify query enforced workflow_state == "Approved" and for_buying == 1
        call_kwargs = self.frappe.get_all.call_args.kwargs
        filters = call_kwargs.get("filters") or {}
        self.assertEqual(filters.get("workflow_state"), "Approved")
        self.assertEqual(filters.get("for_buying"), 1)

    def test_missing_or_stale_rate_throws_ury_fx_rate_stale_never_falls_back(self):
        fx = self._fx()
        # Accounts Settings.stale_days unset -> fail closed to 1 day
        self.frappe.db.get_single_value.return_value = None
        self.frappe.db.get_value.return_value = None
        self.frappe.get_meta.return_value = SimpleNamespace(has_field=lambda f: False)
        self.frappe.get_all.return_value = []

        with self.assertRaises(self.frappe.ValidationError) as ctx:
            fx.resolve_pos_exchange_rate("USD", "UGX", "2026-11-24", "Grand Hotel")
        self.assertIn("URY-FX-RATE-STALE", str(ctx.exception))
        self.assertNotIn("[URY-", str(ctx.exception))

        # Verify fail-closed stale_days=1 lower bound (2026-11-23)
        call_kwargs = self.frappe.get_all.call_args.kwargs
        filters = call_kwargs.get("filters") or {}
        date_filter = filters.get("date")
        self.assertEqual(date_filter, ["between", [datetime.date(2026, 11, 23), datetime.date(2026, 11, 24)]])

    def test_prepare_settlement_payments_ignores_client_rate_and_stamps_fx_fields(self):
        fx = self._fx()
        profile = _Dict(
            name="Main POS",
            company="Grand Hotel",
            currency="UGX",
            custom_allow_fx_rate_override=0,
            custom_max_fx_variance_pct=0,
        )
        mode_map = {
            "USD Cash": _Dict(
                mode_of_payment="USD Cash",
                type="Cash",
                custom_tender_currency="USD",
                custom_fx_rate_type="Buying",
                custom_reference_mode="Hidden",
                custom_reference_label="",
                custom_reference_pattern="",
                custom_reference_date_required=0,
            )
        }
        with patch.object(fx, "get_mode_payment_config", side_effect=lambda m, **kw: mode_map[m]), \
             patch.object(fx, "resolve_pos_exchange_rate", return_value={
                 "rate": 3700.0,
                 "rate_date": "2026-11-24",
                 "currency_exchange": "CE-001",
                 "from_currency": "USD",
                 "to_currency": "UGX",
                 "is_home_currency": False,
             }):
            prepared = fx.prepare_settlement_payments(
                payments=[{
                    "mode_of_payment": "USD Cash",
                    "custom_tender_currency": "USD",
                    "custom_tender_amount": 100.0,
                    "custom_exchange_rate": 9999.0,  # tampered client rate MUST be ignored
                    "amount": 999900.0,  # tampered client local amount MUST be ignored
                }],
                pos_profile=profile,
                posting_date="2026-11-24",
            )
        self.assertEqual(len(prepared), 1)
        row = prepared[0]
        self.assertEqual(row["custom_tender_currency"], "USD")
        self.assertEqual(row["custom_tender_amount"], 100.0)
        self.assertEqual(row["custom_exchange_rate"], 3700.0)
        self.assertEqual(row["custom_exchange_rate_date"], "2026-11-24")
        self.assertEqual(row["amount"], 370000.0)

    def test_ensure_local_change_payment_row_appends_zero_local_cash_row(self):
        fx = self._fx()
        profile = _Dict(
            name="Main POS",
            company="Grand Hotel",
            currency="UGX",
            account_for_change_amount="UGX Cash Drawer - GH",
            payments=[
                _Dict(mode_of_payment="Cash", default=1),
                _Dict(mode_of_payment="USD Cash", default=0),
            ],
        )
        payments = [
            _Dict(
                mode_of_payment="USD Cash",
                account="FX Till Clearing - USD - GH",
                amount=370000.0,
                custom_tender_currency="USD",
                custom_tender_amount=100.0,
                custom_exchange_rate=3700.0,
            )
        ]

        class FakeInvoice(_Dict):
            def append(self, field, row):
                self[field].append(_Dict(row))

        inv = FakeInvoice(
            company="Grand Hotel",
            currency="UGX",
            is_return=0,
            change_amount=20000.0,
            account_for_change_amount="UGX Cash Drawer - GH",
            payments=payments,
        )
        self.frappe.db.get_value.side_effect = lambda dt, flt_arg, field=None: (
            "UGX Cash Drawer - GH"
            if dt == "Mode of Payment Account" and flt_arg.get("parent") == "Cash"
            else "FX Till Clearing - USD - GH"
            if dt == "Mode of Payment Account" and flt_arg.get("parent") == "USD Cash"
            else None
        )

        fx.ensure_local_change_payment_row(inv, pos_profile=profile)
        self.assertEqual(len(inv.payments), 2)
        added = inv.payments[1]
        self.assertEqual(added["mode_of_payment"], "Cash")
        self.assertEqual(added["account"], "UGX Cash Drawer - GH")
        self.assertEqual(added["amount"], 0.0)

    def test_validate_fx_payment_modes_rejects_foreign_default_account_and_accepts_clearing_plus_vault(self):
        fx = self._fx()
        profile = _Dict(
            name="Main POS",
            company="Grand Hotel",
            currency="UGX",
            payments=[_Dict(mode_of_payment="USD Cash")],
        )
        # Case 1: default_account wrongly in USD -> MUST throw (prevents ERPNext v16 GL bug!)
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None, as_dict=False: (
            _Dict(custom_tender_currency="USD", custom_fx_rate_type="Buying")
            if dt == "Mode of Payment"
            else _Dict(default_account="USD Cash Drawer - GH", custom_fx_vault_account="USD Cash Drawer - GH")
            if dt == "Mode of Payment Account"
            else "USD"
            if dt == "Account" and name == "USD Cash Drawer - GH"
            else "UGX"
        )
        with self.assertRaises(self.frappe.ValidationError):
            fx.validate_fx_payment_modes(profile)

        # Case 2: default_account in UGX (FX Till Clearing) + custom_fx_vault_account in USD -> PASS
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None, as_dict=False: (
            _Dict(custom_tender_currency="USD", custom_fx_rate_type="Buying")
            if dt == "Mode of Payment"
            else _Dict(default_account="FX Till Clearing - USD - GH", custom_fx_vault_account="USD Cash Drawer - GH")
            if dt == "Mode of Payment Account"
            else "USD"
            if dt == "Account" and name == "USD Cash Drawer - GH"
            else "UGX"
        )
        fx.validate_fx_payment_modes(profile)


class TestFxVaultSweepJournalEntry(unittest.TestCase):
    def setUp(self):
        self.frappe, self._stubbed = _ensure_frappe_stub()
        self.frappe.db.reset_mock()
        self.frappe.get_doc.reset_mock()
        self.frappe.new_doc.reset_mock()

    def tearDown(self):
        if self._stubbed:
            for mod_name in ("frappe", "frappe.utils", "ury.ury_pos.fx"):
                sys.modules.pop(mod_name, None)

    def _fx(self):
        return importlib.import_module("ury.ury_pos.fx")

    def _build_je_mock(self):
        class FakeJE(_Dict):
            def __init__(self):
                super().__init__(doctype="Journal Entry", name="JV-FX-0001", accounts=[], docstatus=0)
                self.flags = _Dict()
                self.inserted = False
                self.submitted = False

            def append(self, field, row):
                self[field].append(_Dict(row))

            def insert(self, **kw):
                self.inserted = True
                return self

            def submit(self):
                self.submitted = True
                self.docstatus = 1
                return self

        return FakeJE()

    def test_exact_fx_vault_sweep_posts_100_usd_not_370000_usd(self):
        fx = self._fx()
        je = self._build_je_mock()
        self.frappe.new_doc.return_value = je

        profile = _Dict(
            name="Main POS",
            company="Grand Hotel",
            currency="UGX",
            cost_center="Main - GH",
            write_off_account="Cash Over and Short - GH",
            custom_fx_clearing_loss_account="Exchange Gain/Loss - GH",
        )
        self.frappe.get_cached_doc.return_value = profile
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None, as_dict=False: (
            _Dict(default_currency="UGX", cost_center="Main - GH", write_off_account="Cash Over and Short - GH", exchange_gain_loss_account="Exchange Gain/Loss - GH")
            if dt == "Company"
            else _Dict(default_account="FX Till Clearing - USD - GH", custom_fx_vault_account="USD Cash Drawer - GH")
            if dt == "Mode of Payment Account"
            else "USD"
            if dt == "Account" and name == "USD Cash Drawer - GH"
            else "UGX"
        )

        closing = _Dict(
            name="PCE-001",
            company="Grand Hotel",
            pos_profile="Main POS",
            posting_date="2026-11-24",
            custom_fx_vault_journal_entry=None,
            payment_reconciliation=[
                _Dict(
                    mode_of_payment="USD Cash",
                    expected_amount=370000.0,
                    closing_amount=370000.0,
                    difference=0.0,
                    custom_tender_currency="USD",
                    custom_expected_foreign_amount=100.0,
                    custom_closing_foreign_amount=100.0,
                    custom_foreign_difference=0.0,
                ),
                _Dict(
                    mode_of_payment="Cash",
                    expected_amount=-20000.0,
                    closing_amount=-20000.0,
                    difference=0.0,
                    custom_tender_currency="",
                ),
            ],
            db_set=MagicMock(),
        )

        result = fx.post_fx_vault_sweep_journal_entry(closing)
        self.assertEqual(result, "JV-FX-0001")
        self.assertTrue(je.inserted)
        self.assertTrue(je.submitted)
        self.assertEqual(je.multi_currency, 1)

        clearing_line = next(r for r in je.accounts if r.account == "FX Till Clearing - USD - GH")
        vault_line = next(r for r in je.accounts if r.account == "USD Cash Drawer - GH")
        self.assertEqual(clearing_line.credit, 370000.0)
        self.assertEqual(clearing_line.credit_in_account_currency, 370000.0)
        # CRITICAL GL INVARIANT: 100.00 USD in account currency, 370,000 UGX in base currency!
        self.assertEqual(vault_line.debit_in_account_currency, 100.0)
        self.assertEqual(vault_line.debit, 370000.0)
        self.assertEqual(vault_line.exchange_rate, 3700.0)
        self.assertEqual(sum(r.get("debit", 0) for r in je.accounts), sum(r.get("credit", 0) for r in je.accounts))

    def test_fx_shortage_routes_variance_to_cash_over_short(self):
        fx = self._fx()
        je = self._build_je_mock()
        self.frappe.new_doc.return_value = je
        profile = _Dict(
            name="Main POS",
            company="Grand Hotel",
            currency="UGX",
            cost_center="Main - GH",
            write_off_account="Cash Over and Short - GH",
            custom_fx_clearing_loss_account="Exchange Gain/Loss - GH",
        )
        self.frappe.get_cached_doc.return_value = profile
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None, as_dict=False: (
            _Dict(default_currency="UGX", cost_center="Main - GH", write_off_account="Cash Over and Short - GH", exchange_gain_loss_account="Exchange Gain/Loss - GH")
            if dt == "Company"
            else _Dict(default_account="FX Till Clearing - USD - GH", custom_fx_vault_account="USD Cash Drawer - GH")
            if dt == "Mode of Payment Account"
            else "USD"
            if dt == "Account" and name == "USD Cash Drawer - GH"
            else "UGX"
        )

        closing = _Dict(
            name="PCE-002",
            company="Grand Hotel",
            pos_profile="Main POS",
            posting_date="2026-11-24",
            custom_fx_vault_journal_entry=None,
            payment_reconciliation=[
                _Dict(
                    mode_of_payment="USD Cash",
                    expected_amount=370000.0,
                    closing_amount=351500.0,
                    difference=-18500.0,
                    custom_tender_currency="USD",
                    custom_expected_foreign_amount=100.0,
                    custom_closing_foreign_amount=95.0,
                    custom_foreign_difference=-5.0,
                )
            ],
            db_set=MagicMock(),
        )
        fx.post_fx_vault_sweep_journal_entry(closing)
        vault_line = next(r for r in je.accounts if r.account == "USD Cash Drawer - GH")
        var_line = next(r for r in je.accounts if r.account == "Cash Over and Short - GH")
        self.assertEqual(vault_line.debit_in_account_currency, 95.0)
        self.assertEqual(vault_line.debit, 351500.0)
        self.assertEqual(var_line.debit, 18500.0)
        self.assertEqual(sum(r.get("debit", 0) for r in je.accounts), sum(r.get("credit", 0) for r in je.accounts))


class TestFxReceiptTemplates(unittest.TestCase):
    def test_receipt_print_formats_render_fx_tender_and_local_change(self):
        for path in (MERGED_PF, COPY_PF):
            with self.subTest(print_format=path.name):
                html = json.loads(path.read_text(encoding="utf-8")).get("html", "")
                self.assertIn("custom_tender_currency", html)
                self.assertIn("custom_tender_amount", html)
                self.assertIn("custom_exchange_rate", html)


if __name__ == "__main__":
    unittest.main()
