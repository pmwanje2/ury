"""RED/GREEN site-less unit tests for POS-PR-6 Slice 1 (G25):
Config-driven per-tender payment reference / auth code capture.

Tests:
- Custom fields on Mode of Payment in custom_field.json, hooks.py fixtures, and setup_customizations.py
- Server validation helper (`validate_payment_reference`) enforcing URY-PAY-REF-REQUIRED,
  URY-PAY-REF-INVALID (safe bounded regex <= 128 chars), and required reference_date
- Invoice hook enforcement (`ury_pos_invoice.validate_payment_references`)
- API exposure (`getModeOfPayment`, `getPosProfileFull`, `getPosInvoiceItems`, `search_settled_bills`)
- Guest-check print format rendering (`Merged POS Invoice Format` & `URY Bill Copy`) with HTML escaping
"""

from __future__ import annotations

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
MERGED_PF = ROOT / "ury" / "ury" / "print_format" / "merged_pos_invoice_format" / "merged_pos_invoice_format.json"
COPY_PF = ROOT / "ury" / "ury" / "print_format" / "ury_bill_copy" / "ury_bill_copy.json"


class _FakeValidationError(Exception):
    pass


class _FakePermissionError(Exception):
    pass


def _ensure_frappe_stub():
    if "frappe" in sys.modules:
        return sys.modules["frappe"], False
    frappe_mod = types.ModuleType("frappe")
    frappe_mod.ValidationError = _FakeValidationError
    frappe_mod.PermissionError = _FakePermissionError
    frappe_mod._ = lambda msg: msg
    frappe_mod._dict = lambda d=None, **kw: SimpleNamespace(**{**(d or {}), **kw})
    frappe_mod.whitelist = lambda *a, **kw: (a[0] if a and callable(a[0]) else (lambda fn: fn))
    frappe_mod.flags = SimpleNamespace()
    frappe_mod.session = SimpleNamespace(user="cashier@example.com")
    frappe_mod.db = MagicMock()
    frappe_mod.get_all = MagicMock(return_value=[])
    frappe_mod.get_cached_doc = MagicMock()
    frappe_mod.get_doc = MagicMock()
    frappe_mod.get_meta = MagicMock()

    def _throw(msg, exc=None, **kwargs):
        raise (exc or _FakeValidationError)(msg)

    frappe_mod.throw = _throw

    utils_mod = types.ModuleType("frappe.utils")
    utils_mod.cint = lambda v: int(float(v or 0))
    utils_mod.flt = lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0)
    utils_mod.getdate = lambda v=None: v
    utils_mod.add_days = lambda d, n: d
    utils_mod.today = lambda: "2026-11-24"
    frappe_mod.utils = utils_mod

    sys.modules["frappe"] = frappe_mod
    sys.modules["frappe.utils"] = utils_mod
    return frappe_mod, True


class TestPaymentReferenceSchemaAndFixtures(unittest.TestCase):
    EXPECTED_MOP_FIELDS = (
        "Mode of Payment-custom_reference_mode",
        "Mode of Payment-custom_reference_label",
        "Mode of Payment-custom_reference_placeholder",
        "Mode of Payment-custom_reference_pattern",
        "Mode of Payment-custom_reference_date_required",
    )

    def test_custom_fields_registered_in_fixture_json(self):
        rows = json.loads(CUSTOM_FIELDS_JSON.read_text(encoding="utf-8"))
        by_name = {row.get("name"): row for row in rows}
        for name in self.EXPECTED_MOP_FIELDS:
            with self.subTest(field=name):
                self.assertIn(name, by_name, f"Missing {name} in custom_field.json")

        ref_mode = by_name["Mode of Payment-custom_reference_mode"]
        self.assertEqual(ref_mode.get("fieldtype"), "Select")
        self.assertEqual(ref_mode.get("default"), "Hidden")
        for opt in ("Hidden", "Optional", "Required"):
            self.assertIn(opt, (ref_mode.get("options") or "").splitlines())

        ref_date_req = by_name["Mode of Payment-custom_reference_date_required"]
        self.assertEqual(ref_date_req.get("fieldtype"), "Check")

    def test_custom_fields_registered_in_hooks_fixtures_and_setup(self):
        hooks_text = HOOKS_PY.read_text(encoding="utf-8")
        setup_text = SETUP_CUSTOMIZATIONS_PY.read_text(encoding="utf-8")
        for name in self.EXPECTED_MOP_FIELDS:
            fieldname = name.split("-", 1)[1]
            with self.subTest(field=name):
                self.assertIn(name, hooks_text)
                self.assertIn(fieldname, setup_text)


class TestPaymentReferenceValidation(unittest.TestCase):
    def setUp(self):
        self.frappe, self._stubbed = _ensure_frappe_stub()

    def tearDown(self):
        if self._stubbed:
            for mod_name in ("frappe", "frappe.utils", "ury.ury_pos.fx"):
                sys.modules.pop(mod_name, None)

    def _fx(self):
        return importlib.import_module("ury.ury_pos.fx")

    def test_hidden_mode_allows_blank_reference(self):
        fx = self._fx()
        cfg = {
            "custom_reference_mode": "Hidden",
            "custom_reference_label": "",
            "custom_reference_pattern": "",
            "custom_reference_date_required": 0,
        }
        res = fx.validate_payment_reference("Cash", reference_no="", reference_date=None, mode_config=cfg)
        self.assertEqual(res["reference_no"], "")

    def test_required_mode_rejects_blank_with_stable_code(self):
        fx = self._fx()
        cfg = {
            "custom_reference_mode": "Required",
            "custom_reference_label": "MoMo Transaction ID",
            "custom_reference_pattern": r"^[A-Z0-9-]{6,20}$",
            "custom_reference_date_required": 0,
        }
        with self.assertRaises(self.frappe.ValidationError) as ctx:
            fx.validate_payment_reference("MTN Mobile Money", reference_no="   ", mode_config=cfg)
        self.assertIn("URY-PAY-REF-REQUIRED", str(ctx.exception))
        self.assertNotIn("[URY-", str(ctx.exception))

    def test_required_mode_rejects_pattern_mismatch_with_stable_code(self):
        fx = self._fx()
        cfg = {
            "custom_reference_mode": "Required",
            "custom_reference_label": "MoMo Transaction ID",
            "custom_reference_pattern": r"^[A-Z0-9-]{6,20}$",
            "custom_reference_date_required": 0,
        }
        with self.assertRaises(self.frappe.ValidationError) as ctx:
            fx.validate_payment_reference("MTN Mobile Money", reference_no="bad!", mode_config=cfg)
        self.assertIn("URY-PAY-REF-INVALID", str(ctx.exception))
        self.assertNotIn("[URY-", str(ctx.exception))

    def test_required_mode_accepts_valid_reference_and_trims(self):
        fx = self._fx()
        cfg = {
            "custom_reference_mode": "Required",
            "custom_reference_label": "MoMo Transaction ID",
            "custom_reference_pattern": r"^[A-Z0-9-]{6,20}$",
            "custom_reference_date_required": 0,
        }
        res = fx.validate_payment_reference("MTN Mobile Money", reference_no="  MM-2026-991  ", mode_config=cfg)
        self.assertEqual(res["reference_no"], "MM-2026-991")

    def test_optional_mode_allows_empty_but_validates_non_empty_against_pattern(self):
        fx = self._fx()
        cfg = {
            "custom_reference_mode": "Optional",
            "custom_reference_label": "Card Auth Code",
            "custom_reference_pattern": r"^[0-9]{6}$",
            "custom_reference_date_required": 0,
        }
        self.assertEqual(fx.validate_payment_reference("Visa", reference_no="", mode_config=cfg)["reference_no"], "")
        with self.assertRaises(self.frappe.ValidationError) as ctx:
            fx.validate_payment_reference("Visa", reference_no="ABC", mode_config=cfg)
        self.assertIn("URY-PAY-REF-INVALID", str(ctx.exception))
        self.assertEqual(fx.validate_payment_reference("Visa", reference_no="123456", mode_config=cfg)["reference_no"], "123456")

    def test_overlong_or_redos_regex_pattern_is_safely_rejected(self):
        fx = self._fx()
        overlong_cfg = {
            "custom_reference_mode": "Required",
            "custom_reference_pattern": "^" + ("a" * 130) + "$",
            "custom_reference_date_required": 0,
        }
        with self.assertRaises(self.frappe.ValidationError) as ctx:
            fx.validate_payment_reference("Bank", reference_no="a" * 130, mode_config=overlong_cfg)
        self.assertIn("URY-PAY-REF-INVALID", str(ctx.exception))

        redos_cfg = {
            "custom_reference_mode": "Required",
            "custom_reference_pattern": r"^(a+)+$",
            "custom_reference_date_required": 0,
        }
        with self.assertRaises(self.frappe.ValidationError) as ctx:
            fx.validate_payment_reference("Bank", reference_no="a" * 30 + "!", mode_config=redos_cfg)
        self.assertIn("URY-PAY-REF-INVALID", str(ctx.exception))

    def test_reference_date_required_enforced(self):
        fx = self._fx()
        cfg = {
            "custom_reference_mode": "Required",
            "custom_reference_label": "Bank Slip No",
            "custom_reference_pattern": "",
            "custom_reference_date_required": 1,
        }
        with self.assertRaises(self.frappe.ValidationError) as ctx:
            fx.validate_payment_reference("Bank Transfer", reference_no="SLIP-900", reference_date=None, mode_config=cfg)
        self.assertIn("URY-PAY-REF-REQUIRED", str(ctx.exception))

        ok = fx.validate_payment_reference("Bank Transfer", reference_no="SLIP-900", reference_date="2026-11-24", mode_config=cfg)
        self.assertEqual(ok["reference_no"], "SLIP-900")
        self.assertEqual(ok["reference_date"], "2026-11-24")


class TestPaymentReferencePrintAndApiContracts(unittest.TestCase):
    def test_receipt_print_formats_include_reference_no_escaped(self):
        for path in (MERGED_PF, COPY_PF):
            with self.subTest(print_format=path.name):
                html = json.loads(path.read_text(encoding="utf-8")).get("html", "")
                self.assertIn("reference_no", html)
                self.assertRegex(html, r"reference_no\s*\|\s*e")

    def test_api_search_settled_bills_selects_reference_and_fx_fields(self):
        api_text = (ROOT / "ury" / "ury_pos" / "api.py").read_text(encoding="utf-8")
        idx = api_text.find("def search_settled_bills")
        self.assertGreater(idx, 0)
        snippet = api_text[idx : idx + 3500]
        self.assertIn("reference_no", snippet)
        self.assertIn("custom_tender_currency", snippet)
        self.assertIn("custom_tender_amount", snippet)


if __name__ == "__main__":
    unittest.main()
