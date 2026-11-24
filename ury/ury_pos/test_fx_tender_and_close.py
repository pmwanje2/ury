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
    for pkg in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field"):
        sys.modules[pkg] = types.ModuleType(pkg)
    cf_mod = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
    cf_mod.create_custom_fields = MagicMock()
    sys.modules["frappe.custom.doctype.custom_field.custom_field"] = cf_mod
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
            for mod_name in (
                "frappe",
                "frappe.utils",
                "frappe.custom",
                "frappe.custom.doctype",
                "frappe.custom.doctype.custom_field",
                "frappe.custom.doctype.custom_field.custom_field",
                "ury.ury_pos.fx",
                "ury.patches.v2_0.setup_pos_manager_authorisation",
            ):
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

    def test_missing_or_stale_rate_throws_pos_fx_rate_stale_never_falls_back(self):
        fx = self._fx()
        # Accounts Settings.stale_days unset -> fail closed to 1 day
        self.frappe.db.get_single_value.return_value = None
        self.frappe.db.get_value.return_value = None
        self.frappe.get_meta.return_value = SimpleNamespace(has_field=lambda f: False)
        self.frappe.get_all.return_value = []

        with self.assertRaises(self.frappe.ValidationError) as ctx:
            fx.resolve_pos_exchange_rate("USD", "UGX", "2026-11-24", "Grand Hotel")
        self.assertIn("[POS-FX-RATE-STALE]", str(ctx.exception))
        self.assertNotIn("URY-", str(ctx.exception))

        # Verify fail-closed stale_days=1 lower bound (2026-11-23)
        call_kwargs = self.frappe.get_all.call_args.kwargs
        filters = call_kwargs.get("filters") or {}
        date_filter = filters.get("date")
        self.assertEqual(date_filter, ["between", [datetime.date(2026, 11, 23), datetime.date(2026, 11, 24)]])

    def test_fx_override_default_pos_reason_and_patch_seed_wired(self):
        from ury.patches.v2_0 import setup_pos_manager_authorisation

        fx_reasons = [
            row
            for row in setup_pos_manager_authorisation.DEFAULT_POS_REASONS
            if "fx_rate_override" in (row[2] or "").splitlines()
        ]
        self.assertTrue(fx_reasons, "DEFAULT_POS_REASONS must seed a default POS Reason for fx_rate_override")

        patch_src = (ROOT / "ury" / "patches" / "v3_46" / "add_pos_fx_ref_offline_fields.py").read_text(encoding="utf-8")
        self.assertNotIn("ensure_company_rules", patch_src, "Dead import ensure_company_rules must be removed")
        self.assertIn("ensure_fx_override_defaults", patch_src)

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
            for mod_name in (
                "frappe",
                "frappe.utils",
                "frappe.custom",
                "frappe.custom.doctype",
                "frappe.custom.doctype.custom_field",
                "frappe.custom.doctype.custom_field.custom_field",
                "ury.ury_pos.fx",
                "ury.patches.v2_0.setup_pos_manager_authorisation",
            ):
                sys.modules.pop(mod_name, None)

    def _fx(self):
        return importlib.import_module("ury.ury_pos.fx")

    def _build_je_mock(self, je_precision=2):
        frappe_mod = self.frappe

        class FakeJE(_Dict):
            def __init__(self):
                super().__init__(doctype="Journal Entry", name="JV-FX-0001", accounts=[], docstatus=0)
                self.flags = _Dict()
                self.inserted = False
                self.submitted = False

            def append(self, field, row):
                entry = _Dict(row)
                entry.precision = lambda fn: je_precision
                self[field].append(entry)

            def set_amounts_in_company_currency(self):
                for d in self.accounts:
                    rate = float(d.get("exchange_rate") or 1.0)
                    if float(d.get("debit_in_account_currency") or 0):
                        d["debit"] = round(float(d["debit_in_account_currency"]) * rate, je_precision)
                    if float(d.get("credit_in_account_currency") or 0):
                        d["credit"] = round(float(d["credit_in_account_currency"]) * rate, je_precision)

            def insert(self, **kw):
                # Mirror ERPNext JournalEntry.validate() -> set_amounts_in_company_currency() + balance check
                self.set_amounts_in_company_currency()
                tot_dr = round(sum(float(r.get("debit") or 0) for r in self.accounts), je_precision)
                tot_cr = round(sum(float(r.get("credit") or 0) for r in self.accounts), je_precision)
                diff = round(tot_dr - tot_cr, je_precision)
                if abs(diff) > 0:
                    raise frappe_mod.ValidationError(
                        f"Total Debit must be equal to Total Credit. The difference is {diff}"
                    )
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

    def test_cash_over_short_skips_fx_rows_avoiding_double_posting(self):
        # Ensure #145's post_cash_over_short never double-posts variance for FX rows
        # already handled by post_fx_vault_sweep_journal_entry, while keeping local Cash.
        self.frappe.get_precision = MagicMock(return_value=2)
        self.frappe.get_cached_value = MagicMock(return_value="Cash")
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None, as_dict=False: (
            "UGX" if dt == "POS Profile" and field == "currency" else None
        )
        cm = importlib.import_module("ury.ury.hooks.ury_cash_movement")
        try:
            closing = _Dict(
                pos_profile="Main POS",
                company="Grand Hotel",
                payment_reconciliation=[
                    _Dict(mode_of_payment="USD Cash", difference=-18500.0, custom_tender_currency="USD"),
                    _Dict(mode_of_payment="Cash", difference=-5000.0, custom_tender_currency=""),
                ],
            )
            diffs = cm._cash_differences(closing)
            self.assertEqual(diffs, [("Cash", -5000.0)])
        finally:
            if self._stubbed:
                sys.modules.pop("ury.ury.hooks.ury_cash_movement", None)

    def test_fx_vault_sweep_balances_at_je_precision_for_0dp_ugx_2dp_usd_and_3dp_kwd(self):
        fx = self._fx()
        scenarios = [
            # Finding 2 live repro: 0dp UGX home currency with ERPNext 2dp JE debit/credit precision!
            ("UGX", "USD", 2, 12037.0, 3.24, 3.20),
            # 0dp UGX with repeating shift_rate (10000 / 3 = 3333.333333333)
            ("UGX", "USD", 2, 10000.0, 3.00, 2.00),
            # 2dp USD home currency with EUR fractional count
            ("USD", "EUR", 2, 3.52, 3.24, 3.20),
            # 3dp KWD home currency with 3dp JE precision
            ("KWD", "USD", 3, 0.995, 3.24, 3.20),
        ]
        for home_curr, fx_curr, je_prec, exp_base, exp_fx, cnt_fx in scenarios:
            with self.subTest(home=home_curr, fx=fx_curr, exp_base=exp_base, exp_fx=exp_fx, cnt_fx=cnt_fx):
                je = self._build_je_mock(je_precision=je_prec)
                self.frappe.new_doc.return_value = je
                self.frappe.get_precision = MagicMock(return_value=je_prec)
                profile = _Dict(
                    name="Main POS",
                    company="Grand Hotel",
                    currency=home_curr,
                    cost_center="Main - GH",
                    write_off_account="Cash Over and Short - GH",
                    custom_cash_over_short_account="Cash Over and Short - GH",
                    custom_fx_clearing_loss_account="Exchange Gain/Loss - GH",
                )
                self.frappe.get_cached_doc.return_value = profile
                self.frappe.db.get_value.side_effect = lambda dt, name, field=None, as_dict=False, _h=home_curr, _f=fx_curr: (
                    _Dict(
                        default_currency=_h,
                        cost_center="Main - GH",
                        write_off_account="Cash Over and Short - GH",
                        exchange_gain_loss_account="Exchange Gain/Loss - GH",
                    )
                    if dt == "Company"
                    else _Dict(
                        default_account=f"FX Till Clearing - {_f} - GH",
                        custom_fx_vault_account=f"{_f} Cash Drawer - GH",
                    )
                    if dt == "Mode of Payment Account"
                    else _f
                    if dt == "Account" and name == f"{_f} Cash Drawer - GH"
                    else _h
                )
                closing = _Dict(
                    name=f"PCE-{home_curr}-{fx_curr}",
                    company="Grand Hotel",
                    pos_profile="Main POS",
                    posting_date="2026-11-24",
                    custom_fx_vault_journal_entry=None,
                    payment_reconciliation=[
                        _Dict(
                            mode_of_payment=f"{fx_curr} Cash",
                            expected_amount=exp_base,
                            closing_amount=round(cnt_fx * (exp_base / exp_fx), je_prec),
                            difference=round(cnt_fx * (exp_base / exp_fx) - exp_base, je_prec),
                            custom_tender_currency=fx_curr,
                            custom_expected_foreign_amount=exp_fx,
                            custom_closing_foreign_amount=cnt_fx,
                            custom_foreign_difference=round(cnt_fx - exp_fx, 4),
                        )
                    ],
                    db_set=MagicMock(),
                )
                result = fx.post_fx_vault_sweep_journal_entry(closing)
                self.assertEqual(result, "JV-FX-0001")
                self.assertTrue(je.inserted)
                self.assertTrue(je.submitted)
                tot_dr = round(sum(float(r.get("debit") or 0) for r in je.accounts), je_prec)
                tot_cr = round(sum(float(r.get("credit") or 0) for r in je.accounts), je_prec)
                self.assertEqual(tot_dr, tot_cr)

    def test_drawer_accounts_excludes_fx_cash_modes_for_till_cash_movements(self):
        # Finding 3: _drawer_accounts must exclude foreign-currency cash modes so #145
        # Pickups/Loans/Paid-Outs still find exactly one home-currency cash drawer!
        self.frappe.get_cached_value.side_effect = lambda dt, name, field=None: (
            "Cash"
            if dt == "Mode of Payment" and field == "type" and name in ("Cash", "USD Cash", "EUR Cash")
            else "UGX"
            if dt == "Company" and field == "default_currency"
            else None
        )
        self.frappe.db.get_value.side_effect = lambda dt, flt_arg, field=None, as_dict=False: (
            "USD"
            if dt == "Mode of Payment" and flt_arg == "USD Cash" and field == "custom_tender_currency"
            else ""
            if dt == "Mode of Payment" and flt_arg == "Cash" and field == "custom_tender_currency"
            else "UGX Cash Drawer - GH"
            if dt == "Mode of Payment Account" and isinstance(flt_arg, dict) and flt_arg.get("parent") == "Cash"
            else "FX Till Clearing - USD - GH"
            if dt == "Mode of Payment Account" and isinstance(flt_arg, dict) and flt_arg.get("parent") == "USD Cash"
            else None
        )
        cm = importlib.import_module("ury.ury.hooks.ury_cash_movement")
        try:
            profile = _Dict(
                name="KH154 POS",
                company="Grand Hotel",
                currency="UGX",
                payments=[
                    _Dict(mode_of_payment="Cash"),
                    _Dict(mode_of_payment="USD Cash"),
                ],
            )
            drawers = cm._drawer_accounts(profile, "Grand Hotel")
            self.assertEqual(drawers, {"UGX Cash Drawer - GH"})
        finally:
            if self._stubbed:
                sys.modules.pop("ury.ury.hooks.ury_cash_movement", None)

    def test_validate_closing_cash_enforces_over_short_reason_on_fx_only_variance(self):
        # Finding 7e: custom_require_over_short_reason must also trigger when the only variance is on an FX row!
        self.frappe.get_precision = MagicMock(return_value=2)
        self.frappe.get_all = MagicMock(return_value=[])
        self.frappe.get_cached_value = MagicMock(side_effect=lambda dt, name, field=None: (
            "Cash" if dt == "Mode of Payment" and field == "type" else "UGX"
        ))

        def _db_get_value(dt, name, field=None, as_dict=False):
            if dt == "POS Profile" and field == "currency":
                return "UGX"
            if dt == "POS Profile" and field == ["custom_require_over_short_reason", "custom_over_short_reason_tolerance"]:
                return (1, 0)
            if dt == "POS Profile" and field == "custom_cash_over_short_account":
                return "Cash Over Short - GH"
            if dt == "Account":
                return _Dict(company="Grand Hotel", root_type="Expense", is_group=0, disabled=0)
            return None

        self.frappe.db.get_value.side_effect = _db_get_value
        cm = importlib.import_module("ury.ury.hooks.ury_cash_movement")
        try:
            closing_missing_reason = _Dict(
                pos_opening_entry="POE-1",
                pos_profile="Main POS",
                company="Grand Hotel",
                custom_over_short_reason="",
                payment_reconciliation=[
                    _Dict(mode_of_payment="Cash", difference=0.0, custom_tender_currency=""),
                    _Dict(
                        mode_of_payment="USD Cash",
                        difference=-18500.0,
                        custom_tender_currency="USD",
                        custom_foreign_difference=-5.0,
                    ),
                ],
            )
            with self.assertRaises(self.frappe.ValidationError) as ctx:
                cm.validate_closing_cash(closing_missing_reason)
            self.assertIn("CLOSE-OVER-SHORT-REASON", str(ctx.exception))

            closing_with_reason = _Dict(
                **{**closing_missing_reason, "custom_over_short_reason": "Short one $5 note"}
            )
            cm.validate_closing_cash(closing_with_reason)
        finally:
            if self._stubbed:
                sys.modules.pop("ury.ury.hooks.ury_cash_movement", None)


class TestDirectRestFxInvoiceValidation(unittest.TestCase):
    """Finding 4: Direct REST POS Invoice saves/submits must never bypass G22 FX rate or #147 Manager PIN."""

    def setUp(self):
        self.frappe, self._stubbed = _ensure_frappe_stub()
        self.frappe.db.reset_mock()
        self.frappe.get_all.reset_mock()
        self.frappe.get_cached_doc.reset_mock()
        self.frappe.flags = _Dict()

    def tearDown(self):
        if self._stubbed:
            for mod_name in (
                "frappe",
                "frappe.utils",
                "frappe.custom",
                "frappe.custom.doctype",
                "frappe.custom.doctype.custom_field",
                "frappe.custom.doctype.custom_field.custom_field",
                "ury.ury_pos.fx",
                "ury.patches.v2_0.setup_pos_manager_authorisation",
            ):
                sys.modules.pop(mod_name, None)

    def _fx(self):
        return importlib.import_module("ury.ury_pos.fx")

    def _make_doc(self, payments, flags=None):
        doc = _Dict(
            name="KH154-REST-1",
            company="Grand Hotel",
            currency="UGX",
            pos_profile="KH154 POS",
            posting_date="2026-11-24",
            is_return=0,
            change_amount=0.0,
            payments=[_Dict(p) for p in payments],
        )
        doc.flags = _Dict(flags or {})
        return doc

    def test_refuses_forged_rest_fx_rate_and_mismatched_base_amount(self):
        fx = self._fx()
        profile = _Dict(
            name="KH154 POS",
            company="Grand Hotel",
            currency="UGX",
            custom_allow_fx_rate_override=1,
            custom_max_fx_variance_pct=5.0,
        )
        usd_cfg = {
            "mode_of_payment": "USD Cash",
            "type": "Cash",
            "custom_reference_mode": "Hidden",
            "custom_tender_currency": "USD",
            "custom_fx_rate_type": "Buying",
            "custom_reference_date_required": 0,
        }
        market = {
            "rate": 3700.0,
            "rate_date": "2026-11-24",
            "currency_exchange": "CE-001",
            "from_currency": "USD",
            "to_currency": "UGX",
            "is_home_currency": False,
        }
        self.frappe.get_cached_doc.return_value = profile
        self.frappe.get_all.return_value = []

        with patch.object(fx, "get_mode_payment_config", return_value=usd_cfg), \
             patch.object(fx, "resolve_pos_exchange_rate", return_value=market):
            # Attack 1: Forged 1.0 USD @ 350,000.0 = UGX 350,000 (live Hydrogen R1 exploit!)
            forged_rate_doc = self._make_doc([
                {
                    "mode_of_payment": "USD Cash",
                    "amount": 350000.0,
                    "custom_tender_currency": "USD",
                    "custom_tender_amount": 1.0,
                    "custom_exchange_rate": 350000.0,
                }
            ])
            with self.assertRaises((self.frappe.ValidationError, self.frappe.PermissionError)):
                fx.validate_invoice_fx_payments(forged_rate_doc)

            # Attack 2: Forged base amount (1.0 USD @ 3700 != 350000 UGX)
            forged_amt_doc = self._make_doc([
                {
                    "mode_of_payment": "USD Cash",
                    "amount": 350000.0,
                    "custom_tender_currency": "USD",
                    "custom_tender_amount": 1.0,
                    "custom_exchange_rate": 3700.0,
                }
            ])
            with self.assertRaises(self.frappe.ValidationError):
                fx.validate_invoice_fx_payments(forged_amt_doc)

            # Attack 3: Override within 5% variance (3800) WITHOUT #147 server-verified PIN approval
            unauth_override_doc = self._make_doc([
                {
                    "mode_of_payment": "USD Cash",
                    "amount": 13300.0,
                    "custom_tender_currency": "USD",
                    "custom_tender_amount": 3.5,
                    "custom_exchange_rate": 3800.0,
                }
            ])
            with self.assertRaises(self.frappe.PermissionError):
                fx.validate_invoice_fx_payments(unauth_override_doc)

            # Legitimate 1: Authoritative market rate (100.0 USD @ 3700.0 = 370000.0 UGX) -> PASS
            legit_doc = self._make_doc([
                {
                    "mode_of_payment": "USD Cash",
                    "amount": 370000.0,
                    "custom_tender_currency": "USD",
                    "custom_tender_amount": 100.0,
                    "custom_exchange_rate": 3700.0,
                }
            ])
            fx.validate_invoice_fx_payments(legit_doc)

            # Legitimate 2: Overridden rate WITH matching consumed #147 POS Authorisation Log -> PASS
            self.frappe.get_all.return_value = [
                _Dict(
                    claim_json=json.dumps({
                        "action": "pos_fx_rate_override",
                        "mode_of_payment": "USD Cash",
                        "from_currency": "USD",
                        "to_currency": "UGX",
                        "override_rate": 3800.0,
                    })
                )
            ]
            fx.validate_invoice_fx_payments(unauth_override_doc)


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
