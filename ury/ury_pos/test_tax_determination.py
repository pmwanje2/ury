"""G42 Order-type tax determination, live server tax preview, and optional EFRIS pre-check.

Site-less unit tests (frappe double, no bench):
- POS Order Type Tax Category child DocType schema & POS Profile custom field validation;
- SAP MWST / Oracle Simphony tax category precedence:
  1. custom_tax_exempt == 1 lock (never overwritten)
  2. Customer.tax_category (Diplomat / NGO / Duty-Free)
  3. POS Profile.custom_order_type_tax_categories[order_type]
  4. POS Profile.tax_category
  5. Native erpnext.accounts.party.set_taxes(...) Tax Rule lookup -> fallback POS Profile.taxes_and_charges
  6. Legacy Branch.custom_no_taxes fallback only when Aggregators has no order-type mapping
  7. Per-item Item Tax Template preservation (mixed baskets & per-line exemption)
- change_order_type transition re-evaluates taxes and logs an audit comment
- Stateless preview_order_totals: zero DB writes, exact 0/2/3dp precision, itemized tax_rows & efris_warnings
- Country-gated optional EFRIS pre-check hook: untouched when uganda_compliance absent or non-EFRIS company;
  throws EFRIS-ITEM-001 before print/settle and returns warnings in preview when EFRIS-enabled company has
  incomplete item metadata.
"""

import importlib.util
import json
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
URY_PKG = ROOT / "ury"
TODAY = date(2026, 11, 24)


class Row(dict):
    __getattr__ = dict.get

    def __setattr__(self, key, value):
        self[key] = value


def _stub(name, **attrs):
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _flt(value, precision=None):
    val = float(value or 0)
    if precision is not None:
        return round(val, int(precision))
    return val


def _cint(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _getdate(value=None):
    if value is None:
        return TODAY
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class Refused(Exception):
    pass


class NotPermitted(Exception):
    pass


class FakeInvoice(Row):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.setdefault("doctype", "POS Invoice")
        self.setdefault("items", [])
        self.setdefault("taxes", [])
        self.setdefault("payments", [])
        self.setdefault("docstatus", 0)
        self.setdefault("is_pos", 1)
        self.setdefault("custom_tax_exempt", 0)
        self.setdefault("currency", "UGX")
        self.setdefault("additional_discount_percentage", 0)
        self.setdefault("discount_amount", 0)
        self.setdefault("net_total", 0)
        self.setdefault("total", 0)
        self.setdefault("total_taxes_and_charges", 0)
        self.setdefault("grand_total", 0)
        self.setdefault("rounded_total", 0)
        self.setdefault("rounding_adjustment", 0)
        self._precision_map = {"UGX": 0, "USD": 2, "BHD": 3}
        self._comments = []
        self._appended_templates = []
        self._tax_templates = {}
        self._saved = False
        self._inserted = False
        self.flags = SimpleNamespace()
        self.meta = SimpleNamespace(
            has_field=lambda fn: True,
            get_field=lambda fn: SimpleNamespace(options="Sales Taxes and Charges Template")
            if fn == "taxes_and_charges"
            else SimpleNamespace(fieldname=fn),
        )

    def is_new(self):
        return not bool(self.get("name"))

    def set(self, field, value):
        self[field] = value

    def append(self, field, row):
        entry = Row(row) if isinstance(row, dict) else row
        self.setdefault(field, []).append(entry)
        return entry

    def precision(self, fieldname, parentfield=None):
        return self._precision_map.get(self.get("currency") or "USD", 2)

    def append_taxes_from_master(self, tax_master_doctype=None):
        tpl = self.get("taxes_and_charges")
        self._appended_templates.append(tpl)
        if tpl and tpl in self._tax_templates:
            for row in self._tax_templates[tpl]:
                self.append("taxes", dict(row))

    def set_taxes(self):
        if not self.get("taxes") and self.get("taxes_and_charges"):
            self.append_taxes_from_master()

    def set_missing_values(self, for_validate=False):
        if not self.get("posting_date"):
            self["posting_date"] = TODAY

    def calculate_taxes_and_totals(self):
        prec = self.precision("grand_total")
        total = sum(_flt(r.get("rate")) * _flt(r.get("qty")) for r in self.get("items") or [])
        for r in self.get("items") or []:
            r["amount"] = _flt(_flt(r.get("rate")) * _flt(r.get("qty")), prec)
            r["net_amount"] = r["amount"]
        disc_pct = _flt(self.get("additional_discount_percentage"))
        disc_amt = _flt(total * disc_pct / 100.0, prec) if disc_pct > 0 else _flt(self.get("discount_amount"), prec)
        net_total = _flt(max(0.0, total - disc_amt), prec)
        self["total"] = _flt(total, prec)
        self["discount_amount"] = disc_amt
        self["net_total"] = net_total

        total_tax = 0.0
        for tax in self.get("taxes") or []:
            rate = _flt(tax.get("rate"))
            # Per-item tax override: if item has item_tax_rate JSON, honor per-item rate
            taxable_amount = 0.0
            for item in self.get("items") or []:
                item_net = _flt(item.get("net_amount", item.get("amount")))
                if total > 0 and disc_amt > 0:
                    item_net = item_net * (net_total / total)
                item_rate = rate
                raw_map = item.get("item_tax_rate")
                if raw_map:
                    parsed = json.loads(raw_map) if isinstance(raw_map, str) else raw_map
                    if isinstance(parsed, dict) and tax.get("account_head") in parsed:
                        item_rate = _flt(parsed[tax["account_head"]])
                taxable_amount += item_net * (item_rate / 100.0)
            tax["tax_amount"] = _flt(taxable_amount, prec)
            total_tax += tax["tax_amount"]

        self["total_taxes_and_charges"] = _flt(total_tax, prec)
        self["grand_total"] = _flt(net_total + total_tax, prec)
        self["rounded_total"] = self["grand_total"]
        self["rounding_adjustment"] = 0.0

    def add_comment(self, comment_type="Comment", text=""):
        self._comments.append((comment_type, text))

    def save(self, **kwargs):
        self._saved = True

    def insert(self, **kwargs):
        self._inserted = True

    def as_dict(self):
        return dict(self)

    def check_permission(self, perm):
        return True


def _make_frappe_env():
    framework = MagicMock()
    framework.ValidationError = Refused
    framework.PermissionError = NotPermitted
    framework.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
    framework._ = lambda msg, *args: msg
    framework.bold = lambda x: f"<b>{x}</b>"
    framework.whitelist = lambda *args, **kwargs: (lambda fn: fn)
    framework.flags = SimpleNamespace()
    framework.session = SimpleNamespace(user="cashier@example.com")
    framework.get_roles = MagicMock(return_value=["URY Cashier"])
    framework.has_permission = MagicMock(return_value=True)
    framework.get_installed_apps = MagicMock(return_value=["frappe", "erpnext", "ury"])
    framework.conf = {}
    framework.cache = MagicMock()
    framework.logger = MagicMock()
    framework.parse_json = json.loads

    def throw(msg, exc=None, **kwargs):
        raise (exc or Refused)(msg)

    framework.throw = throw
    utils = _stub(
        "frappe.utils",
        cint=_cint,
        flt=_flt,
        getdate=_getdate,
        get_datetime=lambda v=None: v if isinstance(v, datetime) else datetime(2026, 11, 24, 14, 0, 0),
        now_datetime=lambda: datetime(2026, 11, 24, 14, 0, 0),
        get_system_timezone=lambda: "Africa/Kampala",
        nestedset=MagicMock(),
        validate_phone_number=MagicMock(),
        now=lambda: "2026-11-24 14:00:00",
        today=lambda: "2026-11-24",
    )
    framework.utils = utils
    return framework, utils


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None:
        raise FileNotFoundError(f"Module file missing: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestTaxSchemaAndCustomFields(unittest.TestCase):
    def test_pos_order_type_tax_category_doctype_exists_and_is_child_table(self):
        schema_path = (
            URY_PKG
            / "ury_pos/doctype/pos_order_type_tax_category/pos_order_type_tax_category.json"
        )
        self.assertTrue(schema_path.is_file(), f"Missing DocType schema: {schema_path}")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema.get("name"), "POS Order Type Tax Category")
        self.assertEqual(schema.get("module"), "URY POS")
        self.assertEqual(schema.get("istable"), 1)
        fields = {f["fieldname"]: f for f in schema.get("fields", [])}
        self.assertIn("order_type", fields)
        self.assertEqual(fields["order_type"].get("reqd"), 1)
        self.assertIn("tax_category", fields)
        self.assertEqual(fields["tax_category"].get("fieldtype"), "Link")
        self.assertEqual(fields["tax_category"].get("options"), "Tax Category")
        self.assertEqual(fields["tax_category"].get("reqd"), 1)

    def test_custom_fields_registered_in_setup_customizations_and_hooks(self):
        framework, utils = _make_frappe_env()
        cf_mod = _stub("frappe.custom.doctype.custom_field.custom_field", create_custom_fields=MagicMock())
        with patch.dict(
            sys.modules,
            {
                "frappe": framework,
                "frappe.utils": utils,
                "frappe.custom": _stub("frappe.custom"),
                "frappe.custom.doctype": _stub("frappe.custom.doctype"),
                "frappe.custom.doctype.custom_field": _stub("frappe.custom.doctype.custom_field"),
                "frappe.custom.doctype.custom_field.custom_field": cf_mod,
                "click": _stub("click"),
            },
        ):
            sc = _load_module("ury.setup_customizations", URY_PKG / "setup_customizations.py")
            fields_by_dt = sc.get_custom_fields()

        pos_profile_fields = {f["fieldname"]: f for f in fields_by_dt.get("POS Profile", [])}
        self.assertIn("custom_order_type_tax_categories", pos_profile_fields)
        self.assertEqual(pos_profile_fields["custom_order_type_tax_categories"]["fieldtype"], "Table")
        self.assertEqual(
            pos_profile_fields["custom_order_type_tax_categories"]["options"],
            "POS Order Type Tax Category",
        )

        pos_inv_fields = {f["fieldname"]: f for f in fields_by_dt.get("POS Invoice", [])}
        for fname in (
            "custom_serving_period",
            "custom_transaction_local_time",
            "custom_transaction_weekday",
        ):
            self.assertIn(fname, pos_inv_fields, f"POS Invoice missing custom field {fname}")

        hooks_text = (URY_PKG / "hooks.py").read_text(encoding="utf-8")
        for key in (
            "POS Profile-custom_order_type_tax_categories",
            "POS Invoice-custom_serving_period",
            "POS Invoice-custom_transaction_local_time",
            "POS Invoice-custom_transaction_weekday",
        ):
            self.assertIn(key, hooks_text, f"hooks.py fixtures filter missing {key}")


class TestOrderTypeTaxPrecedence(unittest.TestCase):
    def setUp(self):
        self.frappe, self.utils = _make_frappe_env()
        self.set_taxes_mock = MagicMock(return_value=None)
        self.party_mod = _stub("erpnext.accounts.party", set_taxes=self.set_taxes_mock)
        self.erpnext_mod = _stub("erpnext", accounts=_stub("erpnext.accounts", party=self.party_mod))
        modules = {
            "frappe": self.frappe,
            "frappe.utils": self.utils,
            "erpnext": self.erpnext_mod,
            "erpnext.accounts": self.erpnext_mod.accounts,
            "erpnext.accounts.party": self.party_mod,
        }
        self.enterContext(patch.dict(sys.modules, modules))
        mod_path = URY_PKG / "ury_pos/tax_determination.py"
        if not mod_path.is_file():
            self.fail(f"Missing module {mod_path}")
        self.tax_mod = _load_module("ury.ury_pos.tax_determination", mod_path)

    def _profile(self, *, tax_category=None, taxes_and_charges="Default Profile Tax", mappings=None):
        rows = [Row(idx=i + 1, order_type=ot, tax_category=tc) for i, (ot, tc) in enumerate(mappings or [])]
        return Row(
            name="Main Till",
            company="Kampala Bistro Ltd",
            branch="Main Branch",
            currency="UGX",
            tax_category=tax_category,
            taxes_and_charges=taxes_and_charges,
            custom_order_type_tax_categories=rows,
        )

    def test_tax_exempt_lock_is_never_overwritten(self):
        inv = FakeInvoice(
            order_type="Dine In",
            customer="Walk In",
            company="Kampala Bistro Ltd",
            custom_tax_exempt=1,
            tax_category="Diplomatic Exempt",
            taxes_and_charges="Zero Exempt 0%",
        )
        profile = self._profile(
            tax_category="Standard VAT",
            mappings=[("Dine In", "Standard Dine-In VAT")],
        )
        self.set_taxes_mock.return_value = "VAT 18% Template"
        res = self.tax_mod.apply_order_type_tax(
            inv, profile, customer_doc=Row(name="Walk In", tax_category="Customer VAT")
        )
        self.assertTrue(res["locked"])
        self.assertEqual(inv.tax_category, "Diplomatic Exempt")
        self.assertEqual(inv.taxes_and_charges, "Zero Exempt 0%")
        self.set_taxes_mock.assert_not_called()

    def test_customer_tax_category_wins_over_order_type_and_profile(self):
        inv = FakeInvoice(
            order_type="Dine In",
            customer="Embassy Guest",
            company="Kampala Bistro Ltd",
            branch="Main Branch",
        )
        inv._tax_templates["Zero Diplomatic 0%"] = [
            {"charge_type": "On Net Total", "account_head": "VAT Output", "rate": 0, "description": "Exempt 0%"}
        ]
        profile = self._profile(
            tax_category="Profile Default Cat",
            mappings=[("Dine In", "Dine-In VAT 18%")],
        )
        self.set_taxes_mock.return_value = "Zero Diplomatic 0%"
        res = self.tax_mod.apply_order_type_tax(
            inv,
            profile,
            customer_doc=Row(name="Embassy Guest", customer_group="Diplomat", tax_category="Diplomat Exempt"),
        )
        self.assertEqual(res["source"], "customer")
        self.assertEqual(inv.tax_category, "Diplomat Exempt")
        self.assertEqual(inv.taxes_and_charges, "Zero Diplomatic 0%")
        self.set_taxes_mock.assert_called_once()
        _, kwargs = self.set_taxes_mock.call_args
        self.assertEqual(kwargs.get("tax_category"), "Diplomat Exempt")

    def test_order_type_tax_category_resolves_via_native_set_taxes_tax_rule(self):
        inv = FakeInvoice(
            order_type="Take Away",
            customer="Walk In",
            company="Kampala Bistro Ltd",
            branch="Main Branch",
        )
        inv._tax_templates["Takeaway 0% Template"] = [
            {"charge_type": "On Net Total", "account_head": "VAT Output", "rate": 0, "description": "Takeaway 0%"}
        ]
        profile = self._profile(
            tax_category="Standard VAT",
            taxes_and_charges="VAT 18% Template",
            mappings=[("Dine In", "Dine-In VAT 18%"), ("Take Away", "Take-Away Zero")],
        )
        self.set_taxes_mock.return_value = "Takeaway 0% Template"
        res = self.tax_mod.apply_order_type_tax(inv, profile, customer_doc=Row(name="Walk In", tax_category=None))
        self.assertEqual(res["source"], "order_type")
        self.assertEqual(inv.tax_category, "Take-Away Zero")
        self.assertEqual(inv.taxes_and_charges, "Takeaway 0% Template")
        self.assertEqual(len(inv.taxes), 1)
        self.assertEqual(inv.taxes[0].rate, 0)

    def test_falls_back_to_pos_profile_tax_category_and_taxes_and_charges_when_no_tax_rule_matches(self):
        inv = FakeInvoice(
            order_type="Delivery",
            customer="Walk In",
            company="Kampala Bistro Ltd",
            branch="Main Branch",
        )
        inv._tax_templates["Default Profile Tax"] = [
            {"charge_type": "On Net Total", "account_head": "VAT Output", "rate": 18, "description": "VAT 18%"}
        ]
        profile = self._profile(
            tax_category="Profile Standard",
            taxes_and_charges="Default Profile Tax",
            mappings=[("Dine In", "Dine-In VAT 18%")],
        )
        self.set_taxes_mock.return_value = None
        res = self.tax_mod.apply_order_type_tax(inv, profile, customer_doc=Row(name="Walk In", tax_category=None))
        self.assertEqual(res["source"], "pos_profile")
        self.assertEqual(inv.tax_category, "Profile Standard")
        self.assertEqual(inv.taxes_and_charges, "Default Profile Tax")

    def test_order_type_mapping_supersedes_legacy_branch_custom_no_taxes_for_aggregators(self):
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None, **kw: (
            1 if dt == "Branch" and field == "custom_no_taxes" else None
        )
        inv = FakeInvoice(
            order_type="Aggregators",
            customer="Glovo",
            company="Kampala Bistro Ltd",
            branch="Main Branch",
        )
        inv._tax_templates["Aggregator VAT 18%"] = [
            {"charge_type": "On Net Total", "account_head": "VAT Output", "rate": 18, "description": "VAT 18%"}
        ]
        profile = self._profile(
            mappings=[("Aggregators", "Aggregator Standard VAT")],
        )
        self.set_taxes_mock.return_value = "Aggregator VAT 18%"
        res = self.tax_mod.apply_order_type_tax(inv, profile, customer_doc=Row(name="Glovo", tax_category=None))
        self.assertEqual(res["source"], "order_type")
        self.assertEqual(inv.taxes_and_charges, "Aggregator VAT 18%")

    def test_legacy_branch_custom_no_taxes_fallback_when_aggregators_unconfigured(self):
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None, **kw: (
            1 if dt == "Branch" and field == "custom_no_taxes" else None
        )
        inv = FakeInvoice(
            order_type="Aggregators",
            customer="Glovo",
            company="Kampala Bistro Ltd",
            branch="Main Branch",
            taxes_and_charges="VAT 18% Template",
            taxes=[Row(description="VAT 18%", rate=18)],
        )
        profile = self._profile(mappings=[("Dine In", "Dine-In VAT 18%")])
        res = self.tax_mod.apply_order_type_tax(inv, profile, customer_doc=Row(name="Glovo", tax_category=None))
        self.assertEqual(res["source"], "legacy_no_taxes")
        self.assertFalse(inv.taxes_and_charges)
        self.assertEqual(inv.taxes, [])

    def test_validate_order_type_tax_categories_rejects_duplicate_and_disabled(self):
        self.frappe.get_all.return_value = [
            Row(name="VAT Standard", disabled=0),
            Row(name="Old Tax Cat", disabled=1),
        ]
        dup_profile = self._profile(
            mappings=[("Dine In", "VAT Standard"), ("Dine In", "VAT Standard")]
        )
        with self.assertRaises(Refused):
            self.tax_mod.validate_order_type_tax_categories(dup_profile)

        disabled_profile = self._profile(mappings=[("Take Away", "Old Tax Cat")])
        with self.assertRaises(Refused):
            self.tax_mod.validate_order_type_tax_categories(disabled_profile)

        valid_profile = self._profile(mappings=[("Dine In", "VAT Standard")])
        self.tax_mod.validate_order_type_tax_categories(valid_profile)


class TestEfrisOptionalHook(unittest.TestCase):
    def setUp(self):
        self.frappe, self.utils = _make_frappe_env()
        self.enterContext(patch.dict(sys.modules, {"frappe": self.frappe, "frappe.utils": self.utils}))
        mod_path = URY_PKG / "ury_pos/tax_determination.py"
        if not mod_path.is_file():
            self.fail(f"Missing module {mod_path}")
        self.tax_mod = _load_module("ury.ury_pos.tax_determination", mod_path)

    def test_noop_when_uganda_compliance_not_installed(self):
        self.frappe.get_installed_apps.return_value = ["frappe", "erpnext", "ury"]
        inv = FakeInvoice(
            company="Nairobi Kitchen Ltd",
            items=[Row(idx=1, item_code="BURGER", item_name="Burger", uom="Nos")],
        )
        with patch.dict(sys.modules, {"uganda_compliance": None}):
            warnings = self.tax_mod.validate_efris_readiness(inv, throw=True)
        self.assertEqual(warnings, [])

    def test_noop_for_non_efris_company_even_when_uganda_compliance_installed(self):
        self.frappe.get_installed_apps.return_value = ["frappe", "erpnext", "ury", "uganda_compliance"]
        settings_mod = _stub(
            "uganda_compliance.efris.doctype.e_invoicing_settings.e_invoicing_settings",
            get_e_company_settings=MagicMock(return_value=None),
        )
        self.frappe.db.get_value.return_value = 0
        inv = FakeInvoice(
            company="Kenya Bistro Ltd",
            items=[Row(idx=1, item_code="BURGER", item_name="Burger", uom="Nos")],
        )
        with patch.dict(
            sys.modules,
            {
                "uganda_compliance": _stub("uganda_compliance"),
                "uganda_compliance.efris": _stub("uganda_compliance.efris"),
                "uganda_compliance.efris.doctype": _stub("uganda_compliance.efris.doctype"),
                "uganda_compliance.efris.doctype.e_invoicing_settings": _stub(
                    "uganda_compliance.efris.doctype.e_invoicing_settings"
                ),
                "uganda_compliance.efris.doctype.e_invoicing_settings.e_invoicing_settings": settings_mod,
            },
        ):
            warnings = self.tax_mod.validate_efris_readiness(inv, throw=True)
        self.assertEqual(warnings, [])

    def test_efris_enabled_company_warns_in_preview_and_throws_on_print_or_settle(self):
        self.frappe.get_installed_apps.return_value = ["frappe", "erpnext", "ury", "uganda_compliance"]
        settings_mod = _stub(
            "uganda_compliance.efris.doctype.e_invoicing_settings.e_invoicing_settings",
            get_e_company_settings=MagicMock(return_value=Row(company="Kampala Bistro Ltd", enabled=1)),
        )

        def db_get_value(dt, name, field=None, **kw):
            if dt == "Company":
                return 1
            if dt == "Item":
                if isinstance(field, (list, tuple)):
                    return Row(efris_commodity_code="", stock_uom="Nos")
                return "" if field == "efris_commodity_code" else "Nos"
            if dt == "UOM":
                return ""
            return None

        self.frappe.db.get_value.side_effect = db_get_value
        self.frappe.get_all.return_value = []
        inv = FakeInvoice(
            company="Kampala Bistro Ltd",
            items=[Row(idx=1, item_code="UNMAPPED-ITEM", item_name="Mystery Dish", uom="Nos")],
        )
        uc_modules = {
            "uganda_compliance": _stub("uganda_compliance"),
            "uganda_compliance.efris": _stub("uganda_compliance.efris"),
            "uganda_compliance.efris.doctype": _stub("uganda_compliance.efris.doctype"),
            "uganda_compliance.efris.doctype.e_invoicing_settings": _stub(
                "uganda_compliance.efris.doctype.e_invoicing_settings"
            ),
            "uganda_compliance.efris.doctype.e_invoicing_settings.e_invoicing_settings": settings_mod,
        }
        with patch.dict(sys.modules, uc_modules):
            warnings = self.tax_mod.validate_efris_readiness(inv, throw=False)
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0]["code"], "EFRIS-ITEM-001")
            self.assertEqual(warnings[0]["item_code"], "UNMAPPED-ITEM")
            with self.assertRaisesRegex(Refused, "EFRIS-ITEM-001"):
                self.tax_mod.validate_efris_readiness(inv, throw=True)


class TestPreviewOrderTotalsEndpoint(unittest.TestCase):
    def setUp(self):
        self.frappe, self.utils = _make_frappe_env()
        self.set_taxes_mock = MagicMock(return_value="VAT 18% Template")
        self.party_mod = _stub("erpnext.accounts.party", set_taxes=self.set_taxes_mock)
        self.erpnext_mod = _stub(
            "erpnext",
            accounts=_stub("erpnext.accounts", party=self.party_mod),
            controllers=_stub("erpnext.controllers", queries=_stub("erpnext.controllers.queries", item_query=MagicMock())),
        )
        self.brand_mod = _stub("ury.brand", coded_message=lambda k, m="": f"[POS-{k}] {m}")
        self.dc_mod = _stub(
            "ury.ury_pos.default_customer",
            get_default_customer_config=MagicMock(return_value={"order_type_customer_map": {}, "default_customer": None}),
            resolve_default_customer=MagicMock(return_value="Walk In"),
        )
        self.ip_mod = _stub(
            "ury.ury.api.item_price",
            item_price_release_states=MagicMock(return_value=(None, None)),
            price_valid_on=MagicMock(side_effect=lambda code, pl, dt, rf, rel, customer=None: Row(price_list_rate=10000 if code == "BURGER" else 5000)),
        )
        self.enterContext(
            patch.dict(
                sys.modules,
                {
                    "frappe": self.frappe,
                    "frappe.utils": self.utils,
                    "erpnext": self.erpnext_mod,
                    "erpnext.accounts": self.erpnext_mod.accounts,
                    "erpnext.accounts.party": self.party_mod,
                    "erpnext.controllers.queries": self.erpnext_mod.controllers.queries,
                    "ury.brand": self.brand_mod,
                    "ury.ury_pos.default_customer": self.dc_mod,
                    "ury.ury.api.item_price": self.ip_mod,
                },
            )
        )
        if not (URY_PKG / "ury_pos/tax_determination.py").is_file():
            self.fail("Missing ury/ury_pos/tax_determination.py")
        if not (URY_PKG / "ury_pos/dayparts.py").is_file():
            self.fail("Missing ury/ury_pos/dayparts.py")
        self.dayparts = _load_module("ury.ury_pos.dayparts", URY_PKG / "ury_pos/dayparts.py")
        self.tax_mod = _load_module("ury.ury_pos.tax_determination", URY_PKG / "ury_pos/tax_determination.py")
        self.api = _load_module("ury.ury_pos.api", URY_PKG / "ury_pos/api.py")

    def test_preview_order_totals_is_stateless_and_returns_exact_breakdown(self):
        self.assertTrue(
            hasattr(self.api, "preview_order_totals"),
            "preview_order_totals endpoint missing in ury/ury_pos/api.py",
        )
        profile = Row(
            name="Main Till",
            branch="Main Branch",
            restaurant="Kampala Bistro",
            company="Kampala Bistro Ltd",
            currency="UGX",
            cost_center="Main - KB",
            warehouse="Stores - KB",
            tax_category="Standard VAT",
            taxes_and_charges="VAT 18% Template",
            custom_order_type_tax_categories=[Row(order_type="Dine In", tax_category="Dine-In VAT 18%")],
            custom_enable_discount=1,
            allow_discount_change=1,
            role_allowed_for_billing=[Row(role="URY Cashier")],
        )
        created_docs = []

        def new_doc(dt):
            assert dt == "POS Invoice"
            doc = FakeInvoice()
            doc._tax_templates["VAT 18% Template"] = [
                {
                    "charge_type": "On Net Total",
                    "account_head": "VAT Output - KB",
                    "rate": 18,
                    "description": "VAT 18%",
                }
            ]
            created_docs.append(doc)
            return doc

        def get_doc(dt, name=None):
            if dt == "POS Profile":
                return profile
            if dt == "Customer":
                return Row(name=name, customer_name=name, customer_group="Individual", tax_category=None)
            raise AssertionError(f"Unexpected get_doc({dt}, {name})")

        def db_get_value(dt, filters=None, fieldname=None, **kw):
            if dt == "URY Restaurant":
                return "Main Menu" if fieldname == "active_menu" else "Kampala Bistro"
            if dt == "Price List":
                return "Main Prices"
            if dt == "Branch":
                return "Kampala Bistro Ltd" if fieldname == "company" else 0
            if dt == "POS Profile":
                return "Main - KB" if fieldname == "cost_center" else "Main Branch"
            if dt == "Item":
                return "Beef Burger" if filters == "BURGER" else "Fresh Milk"
            if dt == "URY Branch Operating Schedule":
                return "Africa/Kampala"
            return None

        self.frappe.new_doc.side_effect = new_doc
        self.frappe.get_doc.side_effect = get_doc
        self.frappe.db.get_value.side_effect = db_get_value
        self.frappe.get_all.return_value = []
        self.frappe.db.set_value.side_effect = AssertionError("preview_order_totals must never write to DB")
        self.frappe.db.commit.side_effect = AssertionError("preview_order_totals must never commit")

        with patch.object(self.api, "getBranch", return_value="Main Branch"):
            result = self.api.preview_order_totals(
                pos_profile="Main Till",
                order_type="Dine In",
                customer="Walk In",
                items=[{"item": "BURGER", "qty": 2}],
                no_of_pax=2,
            )

        self.assertEqual(len(created_docs), 1)
        self.assertFalse(created_docs[0]._saved)
        self.assertFalse(created_docs[0]._inserted)
        for key in (
            "net_total",
            "discount_amount",
            "tax_rows",
            "grand_total",
            "rounded_total",
            "item_taxes",
            "efris_warnings",
        ):
            self.assertIn(key, result, f"Missing key {key} in preview_order_totals response")
        self.assertEqual(result["net_total"], 20000)
        self.assertEqual(result["tax_rows"][0]["rate"], 18.0)
        self.assertEqual(result["tax_rows"][0]["tax_amount"], 3600)
        self.assertEqual(result["grand_total"], 23600)
        self.assertEqual(result["rounded_total"], 23600)
        self.assertEqual(result["efris_warnings"], [])


class TestChangeOrderTypeTaxTransition(unittest.TestCase):
    def test_change_order_type_exists_and_re_evaluates_tax_with_audit_comment(self):
        frappe_mock, utils = _make_frappe_env()
        set_taxes_mock = MagicMock(return_value="Takeaway 0% Template")
        party_mod = _stub("erpnext.accounts.party", set_taxes=set_taxes_mock)
        erpnext_mod = _stub(
            "erpnext",
            accounts=_stub("erpnext.accounts", party=party_mod),
            controllers=_stub("erpnext.controllers", queries=_stub("erpnext.controllers.queries", item_query=MagicMock())),
        )
        override_mod = _stub(
            "ury.ury_pos.override",
            may_perform=MagicMock(return_value=True),
            consume=MagicMock(),
            mark_verified_approval=MagicMock(),
            _validate_reason=MagicMock(),
        )
        modules = {
            "frappe": frappe_mock,
            "frappe.utils": utils,
            "frappe.model.document": _stub("frappe.model.document", Document=object),
            "erpnext": erpnext_mod,
            "erpnext.accounts": erpnext_mod.accounts,
            "erpnext.accounts.party": party_mod,
            "erpnext.controllers.queries": erpnext_mod.controllers.queries,
            "ury.brand": _stub("ury.brand", coded_message=lambda k, m="": f"[POS-{k}] {m}"),
            "ury.ury_pos.api": _stub(
                "ury.ury_pos.api", getBranch=MagicMock(return_value="Main Branch"),
                getBranchRoom=MagicMock(), getRoom=MagicMock(return_value=[]), posOpening=MagicMock(),
            ),
            "ury.ury_pos.default_customer": _stub(
                "ury.ury_pos.default_customer",
                get_default_customer_config=MagicMock(),
                resolve_default_customer=MagicMock(),
            ),
            "ury.ury_pos.override": override_mod,
            "ury.ury.api.ury_kot_generate": _stub(
                "ury.ury.api.ury_kot_generate", _execute_kot=MagicMock(), process_items_for_cancel_kot=MagicMock(),
            ),
            "ury.ury.api.ury_order_reservation_service": _stub(
                "ury.ury.api.ury_order_reservation_service", _line_key=MagicMock(), _line_ref=MagicMock(),
                reconcile_order_reservations=MagicMock(), release_order_reservations=MagicMock(),
                resolve_production_context=MagicMock(return_value=None), _warehouse_for_context=MagicMock(),
            ),
            "ury.ury.api.ury_stock_policy": _stub("ury.ury.api.ury_stock_policy", get_branch_stock_policy=MagicMock()),
            "ury.ury.doctype.alert_settings.alert_settings": _stub(
                "ury.ury.doctype.alert_settings.alert_settings", get_alert_rule=MagicMock(),
            ),
            "ury.ury.api.ury_kot_notification": _stub(
                "ury.ury.api.ury_kot_notification", create_system_notification=MagicMock(),
                get_users_with_role=MagicMock(),
            ),
            "ury.ury.api.item_price": _stub(
                "ury.ury.api.item_price", item_price_release_states=MagicMock(), price_valid_on=MagicMock(),
            ),
        }
        with patch.dict(sys.modules, modules):
            if not (URY_PKG / "ury_pos/tax_determination.py").is_file():
                self.fail("Missing ury/ury_pos/tax_determination.py")
            _load_module("ury.ury_pos.tax_determination", URY_PKG / "ury_pos/tax_determination.py")
            if (URY_PKG / "ury_pos/dayparts.py").is_file():
                _load_module("ury.ury_pos.dayparts", URY_PKG / "ury_pos/dayparts.py")
            order_mod = _load_module(
                "ury.ury.doctype.ury_order.ury_order",
                URY_PKG / "ury/doctype/ury_order/ury_order.py",
            )
            self.assertTrue(
                hasattr(order_mod, "change_order_type"),
                "change_order_type endpoint missing on ury_order.py",
            )
            inv = FakeInvoice(
                name="POS-001",
                company="Kampala Bistro Ltd",
                branch="Main Branch",
                pos_profile="Main Till",
                order_type="Dine In",
                restaurant_table="T-1",
                custom_restaurant_room="Main Hall",
                customer="Walk In",
                waiter="cashier@example.com",
                tax_category="Dine-In VAT 18%",
                taxes_and_charges="VAT 18% Template",
                items=[Row(item_code="BURGER", rate=10000, qty=1, item_tax_template="Standard 18%", custom_tax_exempt=0)],
            )
            inv._tax_templates["Takeaway 0% Template"] = [
                {"charge_type": "On Net Total", "account_head": "VAT Output", "rate": 0, "description": "Zero 0%"}
            ]
            profile = Row(
                name="Main Till",
                company="Kampala Bistro Ltd",
                branch="Main Branch",
                custom_edit_order_type=1,
                transfer_role_permissions=[Row(role="URY Manager")],
                role_allowed_for_billing=[Row(role="URY Cashier")],
                role_restricted_for_table_order=[],
                custom_order_type_tax_categories=[
                    Row(order_type="Dine In", tax_category="Dine-In VAT 18%"),
                    Row(order_type="Take Away", tax_category="Take-Away Zero"),
                ],
                tax_category="Standard VAT",
                taxes_and_charges="VAT 18% Template",
            )
            frappe_mock.get_doc.side_effect = lambda dt, name=None, **kw: (
                inv if dt == "POS Invoice" else (profile if dt == "POS Profile" else Row(name=name, tax_category=None))
            )
            frappe_mock.get_all.return_value = []
            frappe_mock.db.get_value.return_value = None

            out = order_mod.change_order_type("POS-001", "Take Away")
            self.assertEqual(out["order_type"], "Take Away")
            self.assertEqual(inv.tax_category, "Take-Away Zero")
            self.assertEqual(inv.taxes_and_charges, "Takeaway 0% Template")
            self.assertIsNone(inv.restaurant_table)
            self.assertTrue(inv._saved)
            self.assertTrue(
                any("Take Away" in text and "Takeaway 0% Template" in text for _, text in inv._comments),
                f"Expected tax transition audit comment, got: {inv._comments}",
            )


class TestR2SecurityParityAndEfrisFixes(unittest.TestCase):
    """R2 RED->GREEN regression tests closing all 5 Hydrogen R1 blocking findings + white-label check."""

    def test_custom_field_descriptions_contain_zero_vendor_names(self):
        import re

        vendor = re.compile(r"\b(?:URY|Frappe|ERPNext|Kamra|HUF|Tridz|HeyKoala)\b", re.I)
        framework, utils = _make_frappe_env()
        cf_mod = _stub("frappe.custom.doctype.custom_field.custom_field", create_custom_fields=MagicMock())
        with patch.dict(
            sys.modules,
            {
                "frappe": framework,
                "frappe.utils": utils,
                "frappe.custom": _stub("frappe.custom"),
                "frappe.custom.doctype": _stub("frappe.custom.doctype"),
                "frappe.custom.doctype.custom_field": _stub("frappe.custom.doctype.custom_field"),
                "frappe.custom.doctype.custom_field.custom_field": cf_mod,
                "click": _stub("click"),
            },
        ):
            sc = _load_module("ury.setup_customizations", URY_PKG / "setup_customizations.py")
            fields_by_dt = sc.get_custom_fields()

        target_fields = {
            ("POS Profile", "custom_order_type_tax_categories"),
            ("POS Invoice", "custom_serving_period"),
            ("POS Invoice", "custom_transaction_local_time"),
            ("POS Invoice", "custom_transaction_weekday"),
            ("POS Invoice", "custom_tax_exempt"),
            ("POS Invoice Item", "custom_tax_exempt"),
        }
        for dt, fname in target_fields:
            fdef = next((f for f in fields_by_dt.get(dt, []) if f.get("fieldname") == fname), None)
            self.assertIsNotNone(fdef, f"Missing {dt}-{fname} in setup_customizations")
            for prop in ("label", "description"):
                self.assertNotRegex(str(fdef.get(prop) or ""), vendor, f"{dt}-{fname}.{prop} leaks vendor wording")

        cf_json = json.loads((URY_PKG / "fixtures/custom_field.json").read_text(encoding="utf-8"))
        by_name = {row["name"]: row for row in cf_json}
        for dt, fname in target_fields:
            row = by_name.get(f"{dt}-{fname}")
            if row:
                for prop in ("label", "description"):
                    self.assertNotRegex(str(row.get(prop) or ""), vendor, f"fixture {dt}-{fname}.{prop} leaks vendor wording")

    def test_tax_exempt_customer_does_not_inherit_blank_category_18pct_item_tax(self):
        frappe_mock, utils = _make_frappe_env()
        set_taxes_mock = MagicMock(return_value="UG Exempt 0% - MFU")
        party_mod = _stub("erpnext.accounts.party", set_taxes=set_taxes_mock)
        erpnext_mod = _stub("erpnext", accounts=_stub("erpnext.accounts", party=party_mod))
        with patch.dict(
            sys.modules,
            {
                "frappe": frappe_mock,
                "frappe.utils": utils,
                "erpnext": erpnext_mod,
                "erpnext.accounts": erpnext_mod.accounts,
                "erpnext.accounts.party": party_mod,
            },
        ):
            tax_mod = _load_module("ury.ury_pos.tax_determination", URY_PKG / "ury_pos/tax_determination.py")

            def get_all(dt, filters=None, fields=None, **kw):
                if dt == "Item Tax":
                    return [Row(item_tax_template="Item VAT 18% - MFU", tax_category="")]
                if dt == "Sales Taxes and Charges":
                    tpl = (filters or {}).get("parent")
                    if tpl == "UG Exempt 0% - MFU":
                        return [Row(rate=0.0, tax_amount=0.0, charge_type="On Net Total")]
                    return [Row(rate=18.0, tax_amount=0.0, charge_type="On Net Total")]
                return []

            frappe_mock.get_all.side_effect = get_all
            profile = Row(
                name="POS Profile A",
                company="Maximus Foods UG",
                branch="Main Branch A",
                currency="UGX",
                taxes_and_charges="UG VAT 18% - MFU",
                custom_order_type_tax_categories=[Row(order_type="Take Away", tax_category="Take Away VAT")],
            )
            diplomat = Row(name="Diplomat Exempt Guest", customer_group="Diplomat", tax_category="Zero Exempt Guest")

            # Case 1: Fresh invoice for Diplomat Exempt Guest
            inv_fresh = FakeInvoice(
                order_type="Take Away",
                customer="Diplomat Exempt Guest",
                company="Maximus Foods UG",
                branch="Main Branch A",
                items=[Row(item_code="KH-BURGER-18", rate=20000, qty=1, item_tax_template="", custom_tax_exempt=0)],
            )
            inv_fresh._tax_templates["UG Exempt 0% - MFU"] = [
                {"charge_type": "On Net Total", "account_head": "VAT - MFU", "rate": 0.0, "description": "UG Exempt 0%"}
            ]
            tax_mod.apply_order_type_tax(inv_fresh, profile, customer_doc=diplomat)
            self.assertEqual(inv_fresh.taxes_and_charges, "UG Exempt 0% - MFU")
            self.assertEqual(inv_fresh["items"][0].item_tax_template, "")
            inv_fresh.calculate_taxes_and_totals()
            self.assertEqual(inv_fresh.total_taxes_and_charges, 0.0)

            # Case 2: Existing invoice switched from Walk In (had stale Item VAT 18%) to Diplomat Exempt Guest
            inv_switch = FakeInvoice(
                order_type="Take Away",
                customer="Diplomat Exempt Guest",
                company="Maximus Foods UG",
                branch="Main Branch A",
                tax_category="Take Away VAT",
                taxes_and_charges="UG VAT 18% - MFU",
                items=[
                    Row(
                        item_code="KH-BURGER-18",
                        rate=20000,
                        qty=1,
                        item_tax_template="Item VAT 18% - MFU",
                        item_tax_rate='{"VAT - MFU": 18.0}',
                        custom_tax_exempt=0,
                    )
                ],
            )
            inv_switch._tax_templates["UG Exempt 0% - MFU"] = [
                {"charge_type": "On Net Total", "account_head": "VAT - MFU", "rate": 0.0, "description": "UG Exempt 0%"}
            ]
            tax_mod.apply_order_type_tax(inv_switch, profile, customer_doc=diplomat)
            self.assertEqual(inv_switch["items"][0].item_tax_template, "")
            self.assertEqual(inv_switch["items"][0].item_tax_rate, "{}")
            inv_switch.calculate_taxes_and_totals()
            self.assertEqual(inv_switch.total_taxes_and_charges, 0.0)

    def test_efris_readiness_flags_item_with_empty_item_taxes_table(self):
        frappe_mock, utils = _make_frappe_env()
        frappe_mock.get_installed_apps.return_value = ["frappe", "erpnext", "ury", "uganda_compliance"]
        settings_mod = _stub(
            "uganda_compliance.efris.doctype.e_invoicing_settings.e_invoicing_settings",
            get_e_company_settings=MagicMock(return_value=Row(company="Maximus Foods UG", enabled=1)),
        )
        frappe_mock.db.get_value.side_effect = lambda dt, name, field=None, **kw: (
            Row(efris_commodity_code="50181905", stock_uom="Nos")
            if dt == "Item"
            else ("101" if dt == "UOM" else 1)
        )
        # Item has commodity code + mapped UOM, but ZERO rows in Item.taxes!
        frappe_mock.get_all.return_value = []
        uc_modules = {
            "frappe": frappe_mock,
            "frappe.utils": utils,
            "uganda_compliance": _stub("uganda_compliance"),
            "uganda_compliance.efris": _stub("uganda_compliance.efris"),
            "uganda_compliance.efris.doctype": _stub("uganda_compliance.efris.doctype"),
            "uganda_compliance.efris.doctype.e_invoicing_settings": _stub(
                "uganda_compliance.efris.doctype.e_invoicing_settings"
            ),
            "uganda_compliance.efris.doctype.e_invoicing_settings.e_invoicing_settings": settings_mod,
        }
        with patch.dict(sys.modules, uc_modules):
            tax_mod = _load_module("ury.ury_pos.tax_determination", URY_PKG / "ury_pos/tax_determination.py")
            inv = FakeInvoice(
                company="Maximus Foods UG",
                items=[Row(idx=1, item_code="KH-NO-ITEM-TAX", item_name="Mapped Code Missing Tax Row", uom="Nos")],
            )
            warnings = tax_mod.validate_efris_readiness(inv, throw=False)
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0]["code"], "EFRIS-ITEM-001")
            self.assertEqual(warnings[0]["item_code"], "KH-NO-ITEM-TAX")
            with self.assertRaisesRegex(Refused, "EFRIS-ITEM-001"):
                tax_mod.validate_efris_readiness(inv, throw=True)

    def _load_api_env(self):
        frappe_mock, utils = _make_frappe_env()
        set_taxes_mock = MagicMock(return_value="UG VAT 18% - MFU")
        party_mod = _stub("erpnext.accounts.party", set_taxes=set_taxes_mock)
        pricing_rule_mock = MagicMock(return_value={})
        pricing_rule_mod = _stub(
            "erpnext.accounts.doctype.pricing_rule.pricing_rule",
            get_pricing_rule_for_item=pricing_rule_mock,
        )
        erpnext_mod = _stub(
            "erpnext",
            accounts=_stub(
                "erpnext.accounts",
                party=party_mod,
                doctype=_stub(
                    "erpnext.accounts.doctype",
                    pricing_rule=_stub("erpnext.accounts.doctype.pricing_rule", pricing_rule=pricing_rule_mod),
                ),
            ),
            controllers=_stub("erpnext.controllers", queries=_stub("erpnext.controllers.queries", item_query=MagicMock())),
        )
        brand_mod = _stub("ury.brand", coded_message=lambda k, m="": f"[POS-{k}] {m}")
        dc_mod = _stub(
            "ury.ury_pos.default_customer",
            get_default_customer_config=MagicMock(return_value={"order_type_customer_map": {}, "default_customer": None}),
            resolve_default_customer=MagicMock(return_value=None),
        )
        ip_mod = _stub(
            "ury.ury.api.item_price",
            item_price_release_states=MagicMock(return_value=(None, None)),
            price_valid_on=MagicMock(side_effect=lambda code, pl, dt, rf, rel, customer=None: Row(price_list_rate=20000 if code == "KH-BURGER-18" else 15000)),
        )
        self.enterContext(
            patch.dict(
                sys.modules,
                {
                    "frappe": frappe_mock,
                    "frappe.utils": utils,
                    "erpnext": erpnext_mod,
                    "erpnext.accounts": erpnext_mod.accounts,
                    "erpnext.accounts.party": party_mod,
                    "erpnext.accounts.doctype": erpnext_mod.accounts.doctype,
                    "erpnext.accounts.doctype.pricing_rule": erpnext_mod.accounts.doctype.pricing_rule,
                    "erpnext.accounts.doctype.pricing_rule.pricing_rule": pricing_rule_mod,
                    "erpnext.controllers.queries": erpnext_mod.controllers.queries,
                    "ury.brand": brand_mod,
                    "ury.ury_pos.default_customer": dc_mod,
                    "ury.ury.api.item_price": ip_mod,
                },
            )
        )
        dayparts = _load_module("ury.ury_pos.dayparts", URY_PKG / "ury_pos/dayparts.py")
        tax_mod = _load_module("ury.ury_pos.tax_determination", URY_PKG / "ury_pos/tax_determination.py")
        api = _load_module("ury.ury_pos.api", URY_PKG / "ury_pos/api.py")
        return frappe_mock, api, tax_mod, dayparts, set_taxes_mock, pricing_rule_mock

    def test_preview_order_totals_enforces_profile_read_permission_and_blocks_branchless_idor(self):
        frappe_mock, api, _, _, _, _ = self._load_api_env()

        # Case 1: POS Profile read permission denied -> PermissionError immediately
        denied_profile = Row(name="POS Profile B", branch="Main Branch B", company="Maximus Foods UG")
        denied_profile.check_permission = MagicMock(side_effect=NotPermitted("No read permission"))
        frappe_mock.get_doc.return_value = denied_profile
        with patch.object(api, "getBranch", return_value="Main Branch A"):
            with self.assertRaises(NotPermitted):
                api.preview_order_totals(pos_profile="POS Profile B", order_type="Take Away", items=[])
        denied_profile.check_permission.assert_called_once_with("read")

        # Case 2: Branchless POS Profile with cashier NOT in applicable_for_users -> PermissionError BEFORE new_doc
        branchless_profile = Row(
            name="POS Profile C NoBranch",
            branch="",
            company="Other Co",
            applicable_for_users=[],
        )
        branchless_profile.check_permission = MagicMock(return_value=True)
        frappe_mock.get_doc.return_value = branchless_profile
        frappe_mock.new_doc.side_effect = AssertionError("must reject branchless IDOR before creating invoice")
        with patch.object(api, "getBranch", return_value="Main Branch A"):
            with self.assertRaises(NotPermitted):
                api.preview_order_totals(pos_profile="POS Profile C NoBranch", order_type="Take Away", items=[])

    def test_preview_order_totals_blocks_cross_outlet_foreign_customer_idor(self):
        frappe_mock, api, _, _, _, _ = self._load_api_env()
        profile_a = Row(
            name="POS Profile A",
            branch="Main Branch A",
            restaurant="Rest A",
            company="Maximus Foods UG",
            currency="UGX",
            taxes_and_charges="UG VAT 18% - MFU",
            custom_order_type_tax_categories=[],
            applicable_for_users=[Row(user="cashier@example.com")],
        )
        profile_a.check_permission = MagicMock(return_value=True)
        secret_customer = Row(
            name="VIP Secret B Customer",
            customer_group="Secret B Group",
            tax_category="Secret B Tax",
        )
        secret_customer.check_permission = MagicMock(side_effect=NotPermitted("Foreign customer"))
        frappe_mock.has_permission.side_effect = lambda dt, perm="read", doc=None: (
            False if dt == "Customer" else True
        )
        frappe_mock.get_doc.side_effect = lambda dt, name=None: (
            profile_a if dt == "POS Profile" else secret_customer
        )
        frappe_mock.new_doc.return_value = FakeInvoice()
        frappe_mock.db.get_value.return_value = "Rest A"
        with patch.object(api, "getBranch", return_value="Main Branch A"):
            with self.assertRaises(NotPermitted):
                api.preview_order_totals(
                    pos_profile="POS Profile A",
                    order_type="Take Away",
                    customer="VIP Secret B Customer",
                    items=[{"item": "KH-BURGER-18", "qty": 1}],
                )

    def test_get_restaurant_menu_enforces_profile_permission_branch_match_and_string_profile_safety(self):
        frappe_mock, api, _, _, _, _ = self._load_api_env()
        profile_b = Row(
            name="POS Profile B",
            branch="Main Branch B",
            company="Maximus Foods UG",
            role_allowed_for_billing=[Row(role="URY Cashier")],
        )
        profile_b.check_permission = MagicMock(return_value=True)
        frappe_mock.get_doc.return_value = profile_b
        with patch.object(api, "getBranch", return_value="Main Branch A"):
            with self.assertRaises(NotPermitted):
                api.getRestaurantMenu(pos_profile="POS Profile B", order_type="Take Away")
        profile_b.check_permission.assert_called_once_with("read")

        # String pos_profile passed directly to resolve_restaurant_menu must not raise AttributeError
        profile_a = Row(
            name="POS Profile A",
            branch="Main Branch A",
            company="Maximus Foods UG",
            taxes_and_charges="UG VAT 18% - MFU",
            custom_order_type_tax_categories=[],
        )
        frappe_mock.get_doc.return_value = profile_a
        frappe_mock.get_cached_doc.return_value = profile_a
        frappe_mock.db.get_value.side_effect = lambda dt, flt=None, field=None, **kw: (
            "Rest A" if dt == "URY Restaurant" and isinstance(flt, dict)
            else ("Main Menu" if dt == "URY Restaurant" and field == "active_menu"
            else ("Standard UGX" if dt == "Price List"
            else ("Africa/Kampala" if dt == "URY Branch Operating Schedule" else None)))
        )
        frappe_mock.get_all.return_value = []
        res = api.resolve_restaurant_menu("Main Branch A", order_type="Take Away", pos_profile="POS Profile A")
        self.assertEqual(res["name"], "Main Menu")
        self.assertIsNotNone(res["tax_summary"])

    def test_preview_order_totals_applies_selling_pricing_rules_without_customer_and_reports_effective_tax_rate(self):
        frappe_mock, api, _, _, set_taxes_mock, pricing_rule_mock = self._load_api_env()
        set_taxes_mock.return_value = "UG Levy 10% - MFU"

        def fake_pricing_rule(args, doc=None, for_validate=False):
            if args.get("transaction_type") == "selling" and args.get("item_code") == "KH-BURGER-18":
                return Row(
                    pricing_rules='["PRLE-0001"]',
                    price_or_product_discount="Price",
                    discount_percentage=20.0,
                    discount_amount=0.0,
                )
            return Row()

        pricing_rule_mock.side_effect = fake_pricing_rule
        profile_a = Row(
            name="POS Profile A",
            branch="Main Branch A",
            restaurant="Rest A",
            company="Maximus Foods UG",
            currency="UGX",
            cost_center="Main - MFU",
            warehouse="Outlet A Store - MFU",
            taxes_and_charges="UG Levy 10% - MFU",
            custom_order_type_tax_categories=[Row(order_type="Delivery", tax_category="Delivery Reduced")],
        )
        profile_a.check_permission = MagicMock(return_value=True)

        class PricingAwareInvoice(FakeInvoice):
            def apply_pricing_rule_on_items(self, item, pr_args):
                if pr_args.get("price_or_product_discount") == "Price":
                    pct = _flt(pr_args.get("discount_percentage"))
                    item["discount_percentage"] = pct
                    item["rate"] = _flt(_flt(item.get("price_list_rate")) * (1.0 - pct / 100.0), self.precision("rate"))

        def new_doc(dt):
            doc = PricingAwareInvoice()
            doc._tax_templates["UG Levy 10% - MFU"] = [
                {
                    "charge_type": "On Net Total",
                    "account_head": "VAT - MFU",
                    "rate": 10.0,
                    "description": "UG Levy 10% (10.0%)",
                }
            ]
            return doc

        frappe_mock.new_doc.side_effect = new_doc
        frappe_mock.get_doc.side_effect = lambda dt, name=None: profile_a if dt == "POS Profile" else Row(name=name)
        frappe_mock.db.get_value.side_effect = lambda dt, flt=None, field=None, **kw: (
            "Rest A" if dt == "URY Restaurant" and isinstance(flt, dict)
            else ("Main Menu" if dt == "URY Restaurant" and field == "active_menu"
            else ("Standard UGX" if dt == "Price List"
            else ("Africa/Kampala" if dt == "URY Branch Operating Schedule"
            else ("Classic Kampalan Burger" if dt == "Item" else None))))
        )
        frappe_mock.get_all.side_effect = lambda dt, filters=None, **kw: (
            [Row(item_tax_template="Item VAT 18% - MFU", tax_category="")]
            if dt == "Item Tax"
            else ([Row(rate=10.0, tax_amount=0.0)] if dt == "Sales Taxes and Charges" else [])
        )

        with patch.object(api, "getBranch", return_value="Main Branch A"):
            res = api.preview_order_totals(
                pos_profile="POS Profile A",
                order_type="Delivery",
                customer=None,
                items=[
                    {"item": "KH-BURGER-18", "qty": 1, "rate": 20000, "item_tax_rate": '{"VAT - MFU": 18.0}'},
                    {"item": "KH-NO-EFRIS-CODE", "qty": 1, "rate": 15000, "item_tax_rate": '{"VAT - MFU": 18.0}'},
                ],
            )

        # Pricing Rule -20% on 20,000 -> 16,000 + 15,000 = 31,000 net, 18% VAT = 5,580, grand = 36,580
        self.assertEqual(res["net_total"], 31000)
        self.assertEqual(res["total_taxes_and_charges"], 5580)
        self.assertEqual(res["grand_total"], 36580)
        # Tax row label must NOT claim misleading "10.0%" when overridden to 18% by Item Tax Template
        self.assertTrue(res["tax_rows"][0].get("item_rate_override"))
        self.assertEqual(res["tax_rows"][0].get("effective_rate"), 18.0)
        self.assertNotIn("10.0%", res["tax_rows"][0]["description"])
        self.assertIn("18%", res["tax_rows"][0]["description"])

    def test_change_order_type_refreshes_price_list_for_order_type_menu_and_validates_dine_in_table(self):
        frappe_mock, utils = _make_frappe_env()
        set_taxes_mock = MagicMock(return_value="UG Levy 10% - MFU")
        party_mod = _stub("erpnext.accounts.party", set_taxes=set_taxes_mock)
        erpnext_mod = _stub(
            "erpnext",
            accounts=_stub("erpnext.accounts", party=party_mod),
            controllers=_stub("erpnext.controllers", queries=_stub("erpnext.controllers.queries", item_query=MagicMock())),
        )
        override_mod = _stub(
            "ury.ury_pos.override",
            may_perform=MagicMock(return_value=True),
            consume=MagicMock(),
            mark_verified_approval=MagicMock(),
            _validate_reason=MagicMock(),
        )
        reservation_state = {"active": None}
        table_res_mod = _stub(
            "ury.ury.api.table_reservation",
            check_table_reservation=MagicMock(side_effect=lambda tbl: reservation_state["active"]),
            parse_to_datetime=MagicMock(return_value=datetime(2026, 11, 24, 19, 0, 0)),
        )
        ip_mod = _stub(
            "ury.ury.api.item_price",
            item_price_release_states=MagicMock(return_value=(None, None)),
            price_valid_on=MagicMock(
                side_effect=lambda code, pl, dt, rf, rel, customer=None: Row(
                    price_list_rate=22000 if pl == "Delivery UGX" else 20000
                )
            ),
        )
        modules = {
            "frappe": frappe_mock,
            "frappe.utils": utils,
            "frappe.model.document": _stub("frappe.model.document", Document=object),
            "erpnext": erpnext_mod,
            "erpnext.accounts": erpnext_mod.accounts,
            "erpnext.accounts.party": party_mod,
            "erpnext.controllers.queries": erpnext_mod.controllers.queries,
            "ury.brand": _stub("ury.brand", coded_message=lambda k, m="": f"[POS-{k}] {m}"),
            "ury.ury_pos.api": _stub(
                "ury.ury_pos.api",
                getBranch=MagicMock(return_value="Main Branch A"),
                getBranchRoom=MagicMock(),
                getRoom=MagicMock(return_value=[Row(name="Main Hall", branch="Main Branch A")]),
                posOpening=MagicMock(),
            ),
            "ury.ury_pos.default_customer": _stub(
                "ury.ury_pos.default_customer",
                get_default_customer_config=MagicMock(),
                resolve_default_customer=MagicMock(),
            ),
            "ury.ury_pos.override": override_mod,
            "ury.ury.api.ury_kot_generate": _stub(
                "ury.ury.api.ury_kot_generate", _execute_kot=MagicMock(), process_items_for_cancel_kot=MagicMock()
            ),
            "ury.ury.api.ury_order_reservation_service": _stub(
                "ury.ury.api.ury_order_reservation_service",
                _line_key=MagicMock(),
                _line_ref=MagicMock(),
                reconcile_order_reservations=MagicMock(),
                release_order_reservations=MagicMock(),
                resolve_production_context=MagicMock(return_value=None),
                _warehouse_for_context=MagicMock(),
            ),
            "ury.ury.api.ury_stock_policy": _stub("ury.ury.api.ury_stock_policy", get_branch_stock_policy=MagicMock()),
            "ury.ury.doctype.alert_settings.alert_settings": _stub(
                "ury.ury.doctype.alert_settings.alert_settings", get_alert_rule=MagicMock()
            ),
            "ury.ury.api.ury_kot_notification": _stub(
                "ury.ury.api.ury_kot_notification",
                create_system_notification=MagicMock(),
                get_users_with_role=MagicMock(),
            ),
            "ury.ury.api.item_price": ip_mod,
            "ury.ury.api.table_reservation": table_res_mod,
        }
        with patch.dict(sys.modules, modules):
            _load_module("ury.ury_pos.discount", URY_PKG / "ury_pos/discount.py")
            _load_module("ury.ury_pos.tax_determination", URY_PKG / "ury_pos/tax_determination.py")
            _load_module("ury.ury_pos.dayparts", URY_PKG / "ury_pos/dayparts.py")
            order_mod = _load_module(
                "ury.ury.doctype.ury_order.ury_order",
                URY_PKG / "ury/doctype/ury_order/ury_order.py",
            )

            profile = Row(
                name="POS Profile A",
                company="Maximus Foods UG",
                branch="Main Branch A",
                restaurant="Rest A",
                custom_edit_order_type=1,
                transfer_role_permissions=[Row(role="URY Manager")],
                role_allowed_for_billing=[Row(role="URY Cashier")],
                role_restricted_for_table_order=[],
                custom_order_type_tax_categories=[
                    Row(order_type="Take Away", tax_category="Take Away VAT"),
                    Row(order_type="Delivery", tax_category="Delivery Reduced"),
                    Row(order_type="Dine In", tax_category="Dine In VAT"),
                ],
                taxes_and_charges="UG VAT 18% - MFU",
            )
            tables = {
                "T-B1": Row(name="T-B1", branch="Main Branch B", restaurant_room="Room B", occupied=0),
                "T-A-OCC": Row(name="T-A-OCC", branch="Main Branch A", restaurant_room="Main Hall", occupied=1),
                "T-A-RES": Row(name="T-A-RES", branch="Main Branch A", restaurant_room="Main Hall", occupied=0),
            }

            def db_get_value(dt, flt=None, field=None, **kw):
                if dt == "URY Restaurant":
                    if isinstance(flt, dict):
                        return "Rest A"
                    if field == "order_type_wise_menu":
                        return 1
                    if field == "active_menu":
                        return "Standard Menu"
                    return 0
                if dt == "Order Type Menu":
                    return "Delivery Menu" if (flt or {}).get("order_type") == "Delivery" else "Standard Menu"
                if dt == "Price List":
                    return "Delivery UGX" if (flt or {}).get("restaurant_menu") == "Delivery Menu" else "Standard UGX"
                if dt == "URY Branch Operating Schedule":
                    return "Africa/Kampala"
                if dt == "URY Table":
                    return tables.get(flt)
                return None

            frappe_mock.db.get_value.side_effect = db_get_value
            frappe_mock.db.exists.return_value = None
            frappe_mock.get_all.return_value = []

            # 1. Take Away -> Delivery must refresh selling_price_list to Delivery UGX & re-price item
            inv = FakeInvoice(
                name="MFUA00030",
                company="Maximus Foods UG",
                branch="Main Branch A",
                restaurant="Rest A",
                pos_profile="POS Profile A",
                order_type="Take Away",
                selling_price_list="Standard UGX",
                customer="Walk In Guest",
                waiter="cashier@example.com",
                tax_category="Take Away VAT",
                taxes_and_charges="UG VAT 18% - MFU",
                items=[Row(item_code="KH-BURGER-18", rate=20000, price_list_rate=20000, qty=1, custom_tax_exempt=0)],
            )
            inv._tax_templates["UG Levy 10% - MFU"] = [
                {"charge_type": "On Net Total", "account_head": "VAT - MFU", "rate": 10.0, "description": "UG Levy 10%"}
            ]
            frappe_mock.get_doc.side_effect = lambda dt, name=None, **kw: (
                inv if dt == "POS Invoice" else (profile if dt == "POS Profile" else Row(name=name, tax_category=None))
            )
            order_mod.change_order_type("MFUA00030", "Delivery")
            self.assertEqual(inv.selling_price_list, "Delivery UGX")
            self.assertEqual(inv["items"][0].rate, 22000)

            # 2. Dine In with foreign-branch table -> PermissionError
            with self.assertRaises(NotPermitted):
                order_mod.change_order_type("MFUA00030", "Dine In", table="T-B1")

            # 3. Dine In with occupied table -> ValidationError
            with self.assertRaisesRegex(Refused, "occupied"):
                order_mod.change_order_type("MFUA00030", "Dine In", table="T-A-OCC")

            # 4. Dine In with reserved table in lock window -> ValidationError
            reservation_state["active"] = {
                "is_lock_window_active": True,
                "status": "Confirmed",
                "reserved_at": "2026-11-24 19:00:00",
            }
            with self.assertRaisesRegex(Refused, "reserved"):
                order_mod.change_order_type("MFUA00030", "Dine In", table="T-A-RES")


class TestDirectRestPosInvoiceOrderTypeTaxHardening(unittest.TestCase):
    """R3 Blocker 1 / REPORT.md:26: Direct REST POST /api/resource/POS Invoice must resolve order-type tax."""

    def test_direct_rest_post_pos_invoice_before_validate_applies_order_type_tax_without_profile_default_template(self):
        frappe_mock, utils = _make_frappe_env()
        utils.get_time = MagicMock()
        utils.get_first_day = MagicMock()
        utils.get_last_day = MagicMock()
        set_taxes_mock = MagicMock(
            side_effect=lambda customer, party_type, posting_date, company, customer_group=None, tax_category=None: (
                "UG VAT 18% - MFU" if tax_category == "Take Away VAT" else None
            )
        )
        party_mod = _stub("erpnext.accounts.party", set_taxes=set_taxes_mock)
        erpnext_mod = _stub("erpnext", accounts=_stub("erpnext.accounts", party=party_mod))
        modules = {
            "frappe": frappe_mock,
            "frappe.utils": utils,
            "erpnext": erpnext_mod,
            "erpnext.accounts": erpnext_mod.accounts,
            "erpnext.accounts.party": party_mod,
            "ury.brand": _stub("ury.brand", coded_message=lambda k, m="": f"[POS-{k}] {m}"),
            "ury.ury.doctype.ury_order.ury_order": _stub(
                "ury.ury.doctype.ury_order.ury_order", release_merge_cluster_tables=MagicMock()
            ),
            "ury.ury.doctype.staff_discount_policy.staff_discount_policy": _stub(
                "ury.ury.doctype.staff_discount_policy.staff_discount_policy",
                get_applicable_policy=MagicMock(),
            ),
            "ury.ury_pos.discount": _stub(
                "ury.ury_pos.discount",
                prorate_return_discount=MagicMock(),
                validate_pos_discount=MagicMock(),
            ),
            "ury.ury_pos.pricing_actions": _stub(
                "ury.ury_pos.pricing_actions",
                prorate_return_service_charge=MagicMock(),
                sync_service_charge_on_before_validate=MagicMock(),
            ),
        }
        with patch.dict(sys.modules, modules):
            _load_module("ury.ury_pos.tax_determination", URY_PKG / "ury_pos/tax_determination.py")
            dayparts = _load_module("ury.ury_pos.dayparts", URY_PKG / "ury_pos/dayparts.py")
            hooks = _load_module("ury.ury.hooks.ury_pos_invoice", URY_PKG / "ury/hooks/ury_pos_invoice.py")

            profile = Row(
                name="POS Profile A",
                company="Maximus Foods UG",
                branch="Main Branch A",
                restaurant="Rest A",
                tax_category="",
                taxes_and_charges="",  # Intentionally blank: relies solely on custom_order_type_tax_categories!
                custom_order_type_tax_categories=[
                    Row(order_type="Take Away", tax_category="Take Away VAT"),
                    Row(order_type="Delivery", tax_category="Delivery Reduced"),
                ],
            )
            frappe_mock.get_cached_doc.side_effect = lambda dt, name=None: profile if dt == "POS Profile" else None
            frappe_mock.get_doc.side_effect = lambda dt, name=None: profile if dt == "POS Profile" else None

            def db_get_value(dt, flt=None, field=None, **kw):
                if dt == "POS Profile":
                    if isinstance(field, (list, tuple)):
                        return Row(branch="Main Branch A", restaurant="Rest A", ury_offline_capture=0)
                    return "Main Branch A" if field == "branch" else ("Rest A" if field == "restaurant" else 0)
                if dt == "URY Branch Operating Schedule":
                    return "Africa/Kampala"
                if dt == "URY Restaurant":
                    if isinstance(flt, dict):
                        return "Rest A"
                    return "Standard Menu" if field == "active_menu" else 0
                if dt == "Price List":
                    return 1 if field == "enabled" else "Standard UGX"
                if dt == "Item Price":
                    if isinstance(flt, dict) and flt.get("price_list") == "Standard UGX" and flt.get("item_code") == "KH-BURGER-18":
                        return 20000.0
                    return None
                if dt == "Customer":
                    return Row(tax_category=None, customer_group="Individual")
                return None

            frappe_mock.db.get_value.side_effect = db_get_value
            frappe_mock.get_all.return_value = []

            doc = FakeInvoice(
                name=None,
                pos_profile="POS Profile A",
                branch="Main Branch A",
                restaurant="Rest A",
                order_type="Take Away",
                customer="Walk In Guest",
                company=None,
                tax_category="",
                taxes_and_charges="",
                selling_price_list="Standard UGX",
                items=[Row(item_code="KH-BURGER-18", qty=1, price_list_rate=1.0, rate=1.0)],
            )
            doc._tax_templates["UG VAT 18% - MFU"] = [
                {"charge_type": "On Net Total", "account_head": "VAT - MFU", "rate": 18.0, "description": "VAT"}
            ]
            doc.get_doc_before_save = lambda: None

            with patch.object(
                dayparts,
                "resolve_outlet_local_datetime",
                return_value=(datetime(2026, 11, 24, 18, 16, 0), "18:16:00", "Tue"),
            ):
                hooks.before_validate(doc, "before_validate")

            self.assertEqual(doc["items"][0].price_list_rate, 20000.0)
            self.assertEqual(doc["items"][0].rate, 20000.0)
            self.assertEqual(doc.tax_category, "Take Away VAT")
            self.assertEqual(doc.taxes_and_charges, "UG VAT 18% - MFU")
            self.assertEqual(len(doc.taxes), 1)
            self.assertEqual(doc.taxes[0]["rate"], 18.0)

    def _load_direct_rest_env(self, *, profile=None, item_taxes_by_code=None):
        frappe_mock, utils = _make_frappe_env()
        utils.get_time = MagicMock()
        utils.get_first_day = MagicMock()
        utils.get_last_day = MagicMock()
        frappe_mock.get_roles = MagicMock(return_value=["URY Cashier"])
        set_taxes_mock = MagicMock(
            side_effect=lambda customer, party_type, posting_date, company, customer_group=None, tax_category=None: (
                "UG VAT 18% - MFU" if tax_category == "Take Away VAT" else None
            )
        )
        party_mod = _stub("erpnext.accounts.party", set_taxes=set_taxes_mock)
        erpnext_mod = _stub("erpnext", accounts=_stub("erpnext.accounts", party=party_mod))
        modules = {
            "frappe": frappe_mock,
            "frappe.utils": utils,
            "erpnext": erpnext_mod,
            "erpnext.accounts": erpnext_mod.accounts,
            "erpnext.accounts.party": party_mod,
            "ury.brand": _stub("ury.brand", coded_message=lambda k, m="": f"[POS-{k}] {m}"),
            "ury.ury.doctype.ury_order.ury_order": _stub(
                "ury.ury.doctype.ury_order.ury_order", release_merge_cluster_tables=MagicMock()
            ),
            "ury.ury.doctype.staff_discount_policy.staff_discount_policy": _stub(
                "ury.ury.doctype.staff_discount_policy.staff_discount_policy",
                get_applicable_policy=MagicMock(),
            ),
            "ury.ury_pos.discount": _stub(
                "ury.ury_pos.discount",
                prorate_return_discount=MagicMock(),
                validate_pos_discount=MagicMock(),
            ),
            "ury.ury_pos.pricing_actions": _stub(
                "ury.ury_pos.pricing_actions",
                prorate_return_service_charge=MagicMock(),
                sync_service_charge_on_before_validate=MagicMock(),
            ),
        }
        profile_obj = profile or Row(
            name="POS Profile A",
            company="Maximus Foods UG",
            branch="Main Branch A",
            restaurant="Rest A",
            tax_category="",
            taxes_and_charges="",
            custom_order_type_tax_categories=[
                Row(order_type="Take Away", tax_category="Take Away VAT"),
                Row(order_type="Delivery", tax_category="Delivery Reduced"),
            ],
        )
        frappe_mock.get_cached_doc.side_effect = lambda dt, name=None: profile_obj if dt == "POS Profile" else None
        frappe_mock.get_doc.side_effect = lambda dt, name=None: profile_obj if dt == "POS Profile" else None

        def db_get_value(dt, flt=None, field=None, **kw):
            if dt == "POS Profile":
                if isinstance(field, (list, tuple)):
                    return Row(branch="Main Branch A", restaurant="Rest A", ury_offline_capture=0)
                return "Main Branch A" if field == "branch" else ("Rest A" if field == "restaurant" else 0)
            if dt == "URY Branch Operating Schedule":
                return "Africa/Kampala"
            if dt == "URY Restaurant":
                if isinstance(flt, dict):
                    return "Rest A"
                return "Standard Menu" if field == "active_menu" else 0
            if dt == "Price List":
                return 1 if field == "enabled" else "Standard UGX"
            if dt == "Item Price":
                if isinstance(flt, dict) and flt.get("price_list") == "Standard UGX" and flt.get("item_code") == "KH-BURGER-18":
                    return 20000.0
                return None
            if dt == "Customer":
                return Row(tax_category=None, customer_group="Individual")
            return None

        frappe_mock.db.get_value.side_effect = db_get_value
        item_taxes = item_taxes_by_code or {
            "KH-BURGER-18": [Row(item_tax_template="Item VAT 18% - MFU", tax_category="")]
        }

        def get_all(dt, filters=None, **kw):
            if dt == "Item Tax" and isinstance(filters, dict):
                return item_taxes.get(filters.get("parent"), [])
            if dt == "Sales Taxes and Charges" and isinstance(filters, dict):
                if filters.get("parent") == "UG VAT 18% - MFU":
                    return [Row(rate=18.0, tax_amount=0.0, charge_type="On Net Total")]
                if filters.get("parent") == "UG Exempt 0% - MFU":
                    return [Row(rate=0.0, tax_amount=0.0, charge_type="On Net Total")]
            return []

        frappe_mock.get_all.side_effect = get_all
        return frappe_mock, modules

    def test_direct_rest_post_overwrites_tampered_item_tax_template_and_default_header_template(self):
        """R3 P0 Security: Direct REST POST with authentic PL/header + tampered item_tax_template must be overwritten fail-closed."""
        frappe_mock, modules = self._load_direct_rest_env()
        with patch.dict(sys.modules, modules):
            _load_module("ury.ury_pos.tax_determination", URY_PKG / "ury_pos/tax_determination.py")
            dayparts = _load_module("ury.ury_pos.dayparts", URY_PKG / "ury_pos/dayparts.py")
            hooks = _load_module("ury.ury.hooks.ury_pos_invoice", URY_PKG / "ury/hooks/ury_pos_invoice.py")

            doc = FakeInvoice(
                name=None,
                pos_profile="POS Profile A",
                branch="Main Branch A",
                restaurant="Rest A",
                order_type="Take Away",
                customer="Walk In Guest",
                company="Maximus Foods UG",
                tax_category="Take Away VAT",
                taxes_and_charges="UG VAT 18% - MFU",
                selling_price_list="Standard UGX",
                items=[
                    Row(
                        item_code="KH-BURGER-18",
                        qty=1,
                        price_list_rate=20000.0,
                        rate=20000.0,
                        item_tax_template="Item Exempt 0% - MFU",
                        item_tax_rate='{"VAT - MFU": 0}',
                        custom_tax_exempt=0,
                    )
                ],
            )
            doc._tax_templates["UG VAT 18% - MFU"] = [
                {"charge_type": "On Net Total", "account_head": "VAT - MFU", "rate": 18.0, "description": "VAT"}
            ]
            doc.get_doc_before_save = lambda: None

            with patch.object(
                dayparts,
                "resolve_outlet_local_datetime",
                return_value=(datetime(2026, 11, 24, 18, 16, 0), "18:16:00", "Tue"),
            ):
                hooks.before_validate(doc, "before_validate")

            self.assertEqual(doc["items"][0].item_tax_template, "Item VAT 18% - MFU")
            self.assertEqual(doc["items"][0].item_tax_rate, "{}")

        # Also verify profile with NO custom_order_type_tax_categories (source == "default")
        # refuses client-tampered taxes_and_charges="UG Exempt 0% - MFU" on direct REST POST.
        default_profile = Row(
            name="POS Profile Default",
            company="Maximus Foods UG",
            branch="Main Branch A",
            restaurant="Rest A",
            tax_category="",
            taxes_and_charges="UG VAT 18% - MFU",
            custom_order_type_tax_categories=[],
        )
        frappe_mock2, modules2 = self._load_direct_rest_env(profile=default_profile)
        with patch.dict(sys.modules, modules2):
            _load_module("ury.ury_pos.tax_determination", URY_PKG / "ury_pos/tax_determination.py")
            dayparts = _load_module("ury.ury_pos.dayparts", URY_PKG / "ury_pos/dayparts.py")
            hooks = _load_module("ury.ury.hooks.ury_pos_invoice", URY_PKG / "ury/hooks/ury_pos_invoice.py")

            doc2 = FakeInvoice(
                name=None,
                pos_profile="POS Profile Default",
                branch="Main Branch A",
                restaurant="Rest A",
                order_type="Take Away",
                customer="Walk In Guest",
                company="Maximus Foods UG",
                tax_category="",
                taxes_and_charges="UG Exempt 0% - MFU",
                selling_price_list="Standard UGX",
                items=[
                    Row(
                        item_code="KH-BURGER-18",
                        qty=1,
                        price_list_rate=20000.0,
                        rate=20000.0,
                        item_tax_template="Item Exempt 0% - MFU",
                        item_tax_rate='{"VAT - MFU": 0}',
                        custom_tax_exempt=0,
                    )
                ],
            )
            doc2._tax_templates["UG VAT 18% - MFU"] = [
                {"charge_type": "On Net Total", "account_head": "VAT - MFU", "rate": 18.0, "description": "VAT"}
            ]
            doc2.get_doc_before_save = lambda: None

            with patch.object(
                dayparts,
                "resolve_outlet_local_datetime",
                return_value=(datetime(2026, 11, 24, 18, 16, 0), "18:16:00", "Tue"),
            ):
                hooks.before_validate(doc2, "before_validate")

            self.assertEqual(doc2.taxes_and_charges, "UG VAT 18% - MFU")
            self.assertEqual(doc2["items"][0].item_tax_template, "Item VAT 18% - MFU")
            self.assertEqual(doc2["items"][0].item_tax_rate, "{}")

    def test_direct_rest_post_and_put_block_forged_item_tax_exempt_and_overwrite_tampered_put_item_tax(self):
        """R3 P0 Security: Direct REST POST/PUT cannot forge item custom_tax_exempt or tamper item_tax_template."""
        frappe_mock, modules = self._load_direct_rest_env()
        with patch.dict(sys.modules, modules):
            _load_module("ury.ury_pos.tax_determination", URY_PKG / "ury_pos/tax_determination.py")
            dayparts = _load_module("ury.ury_pos.dayparts", URY_PKG / "ury_pos/dayparts.py")
            hooks = _load_module("ury.ury.hooks.ury_pos_invoice", URY_PKG / "ury/hooks/ury_pos_invoice.py")

            # 1. Direct REST POST forging item-level custom_tax_exempt=1 -> PermissionError [POS-AUTH-REQUIRED]
            forged_post = FakeInvoice(
                name=None,
                pos_profile="POS Profile A",
                branch="Main Branch A",
                restaurant="Rest A",
                order_type="Take Away",
                customer="Walk In Guest",
                company="Maximus Foods UG",
                selling_price_list="Standard UGX",
                items=[
                    Row(
                        item_code="KH-BURGER-18",
                        qty=1,
                        item_tax_template="Item Exempt 0% - MFU",
                        custom_tax_exempt=1,
                    )
                ],
            )
            forged_post.get_doc_before_save = lambda: None
            with patch.object(
                dayparts,
                "resolve_outlet_local_datetime",
                return_value=(datetime(2026, 11, 24, 18, 16, 0), "18:16:00", "Tue"),
            ):
                with self.assertRaisesRegex(frappe_mock.PermissionError, r"POS-AUTH-REQUIRED"):
                    hooks.before_validate(forged_post, "before_validate")

            # 2. Direct REST PUT tampering item_tax_template/item_tax_rate on existing check -> overwritten fail-closed
            prev_row = Row(
                name="ROW-TAX-1",
                item_code="KH-BURGER-18",
                qty=1,
                price_list_rate=20000.0,
                rate=20000.0,
                item_tax_template="Item VAT 18% - MFU",
                item_tax_rate='{"VAT - MFU": 18.0}',
                custom_tax_exempt=0,
            )
            prev_doc = Row(
                name="MFUA00099",
                order_type="Take Away",
                customer="Walk In Guest",
                tax_category="Take Away VAT",
                taxes_and_charges="UG VAT 18% - MFU",
                custom_tax_exempt=0,
                selling_price_list="Standard UGX",
                custom_serving_period="All Day Dining",
                custom_transaction_local_time="18:10:00",
                custom_transaction_weekday="Tue",
                items=[prev_row],
            )
            tampered_put = FakeInvoice(
                name="MFUA00099",
                pos_profile="POS Profile A",
                branch="Main Branch A",
                restaurant="Rest A",
                order_type="Take Away",
                customer="Walk In Guest",
                company="Maximus Foods UG",
                tax_category="Take Away VAT",
                taxes_and_charges="UG VAT 18% - MFU",
                custom_tax_exempt=0,
                selling_price_list="Standard UGX",
                items=[
                    Row(
                        name="ROW-TAX-1",
                        item_code="KH-BURGER-18",
                        qty=1,
                        price_list_rate=20000.0,
                        rate=20000.0,
                        item_tax_template="Item Exempt 0% - MFU",
                        item_tax_rate='{"VAT - MFU": 0}',
                        custom_tax_exempt=0,
                    )
                ],
            )
            tampered_put._tax_templates["UG VAT 18% - MFU"] = [
                {"charge_type": "On Net Total", "account_head": "VAT - MFU", "rate": 18.0, "description": "VAT"}
            ]
            tampered_put.get_doc_before_save = lambda: prev_doc
            with patch.object(
                dayparts,
                "resolve_outlet_local_datetime",
                return_value=(datetime(2026, 11, 24, 18, 16, 0), "18:16:00", "Tue"),
            ):
                hooks.before_validate(tampered_put, "before_validate")
            self.assertEqual(tampered_put["items"][0].item_tax_template, "Item VAT 18% - MFU")
            self.assertEqual(tampered_put["items"][0].item_tax_rate, "{}")

            # 3. Direct REST PUT flipping item custom_tax_exempt 0 -> 1 -> PermissionError [POS-AUTH-REQUIRED]
            exempt_put = FakeInvoice(
                name="MFUA00099",
                pos_profile="POS Profile A",
                branch="Main Branch A",
                restaurant="Rest A",
                order_type="Take Away",
                customer="Walk In Guest",
                company="Maximus Foods UG",
                tax_category="Take Away VAT",
                taxes_and_charges="UG VAT 18% - MFU",
                custom_tax_exempt=0,
                selling_price_list="Standard UGX",
                items=[
                    Row(
                        name="ROW-TAX-1",
                        item_code="KH-BURGER-18",
                        qty=1,
                        price_list_rate=20000.0,
                        rate=20000.0,
                        item_tax_template="Item Exempt 0% - MFU",
                        custom_tax_exempt=1,
                    )
                ],
            )
            exempt_put.get_doc_before_save = lambda: prev_doc
            with patch.object(
                dayparts,
                "resolve_outlet_local_datetime",
                return_value=(datetime(2026, 11, 24, 18, 16, 0), "18:16:00", "Tue"),
            ):
                with self.assertRaisesRegex(frappe_mock.PermissionError, r"POS-AUTH-REQUIRED"):
                    hooks.before_validate(exempt_put, "before_validate")

    def test_pos_invoice_custom_field_row_width_stays_within_mariadb_1118_budget(self):
        """R3 Schema Guard: Combined URY POS Invoice custom fields must leave >=12KB headroom for ERPNext + uganda_compliance under MariaDB's 65,535B row limit."""
        framework, utils = _make_frappe_env()
        cf_mod = _stub("frappe.custom.doctype.custom_field.custom_field", create_custom_fields=MagicMock())
        with patch.dict(
            sys.modules,
            {
                "frappe": framework,
                "frappe.utils": utils,
                "frappe.custom": _stub("frappe.custom"),
                "frappe.custom.doctype": _stub("frappe.custom.doctype"),
                "frappe.custom.doctype.custom_field": _stub("frappe.custom.doctype.custom_field"),
                "frappe.custom.doctype.custom_field.custom_field": cf_mod,
                "click": _stub("click"),
            },
        ):
            sc = _load_module("ury.setup_customizations", URY_PKG / "setup_customizations.py")
            pf = _load_module("ury.ury_pos.privileged_fields", URY_PKG / "ury_pos/privileged_fields.py")
            sc_fields = sc.get_custom_fields().get("POS Invoice", [])
            pf_fields = pf.get_privileged_action_custom_fields().get("POS Invoice", [])

        fixture_rows = [
            r
            for r in json.loads((URY_PKG / "fixtures/custom_field.json").read_text(encoding="utf-8"))
            if r.get("dt") == "POS Invoice"
        ]

        # Merge in exact Frappe install order (fixtures -> setup_customizations -> privileged_fields)
        merged = {}
        for src in (fixture_rows, sc_fields, pf_fields):
            for f in src:
                fn = f.get("fieldname")
                if not fn:
                    continue
                if fn not in merged:
                    merged[fn] = dict(f)
                else:
                    # create_custom_fields updates existing custom field properties
                    merged[fn].update({k: v for k, v in f.items() if v is not None})

        def mariadb_utf8mb4_bytes(field):
            ft = field.get("fieldtype")
            if ft in ("Section Break", "Column Break", "Tab Break", "Button", "Table", "HTML", "Heading"):
                return 0
            if ft in ("Data", "Link", "Select", "Dynamic Link", "Read Only", "Password"):
                raw_len = int(field.get("length") or 0)
                chars = max(raw_len, 64) if raw_len > 0 else 140
                octets = chars * 4
                return octets + (2 if octets > 255 else 1)
            if ft in ("Small Text", "Text", "Long Text", "Text Editor", "Code", "JSON"):
                return 12
            if ft in ("Currency", "Percent"):
                return 10
            if ft in ("Datetime", "Float"):
                return 8
            if ft in ("Int",):
                return 4
            if ft in ("Date", "Time"):
                return 3
            if ft in ("Check",):
                return 1
            return 8

        total_bytes = sum(mariadb_utf8mb4_bytes(f) for f in merged.values())
        # ERPNext base tabPOS Invoice (~33,808 B) + uganda_compliance (~10,150 B) = ~43,958 B.
        # Keep URY's combined POS Invoice custom fields <= 18,000 B so total stays <= 61,958 B (< 65,535 B MariaDB 1118 limit).
        self.assertLessEqual(
            total_bytes,
            18000,
            f"URY POS Invoice custom fields use {total_bytes} B (> 18,000 B budget); triggers MariaDB 1118 Row size too large with uganda_compliance.",
        )

    def test_change_order_type_repricing_coexists_with_152_privileged_mutation_guard_and_preserves_overrides(self):
        """R3 live-caught bug: change_order_type repricing must pass validate_privileged_pricing_mutations and preserve #152 manager-overridden rows."""
        frappe_mock, utils = _make_frappe_env()
        frappe_mock.session = SimpleNamespace(user="cashier_a@example.com")
        frappe_mock.get_roles = MagicMock(return_value=["URY Cashier"])
        frappe_mock.has_permission = MagicMock(return_value=True)
        frappe_mock.get_meta = MagicMock(return_value=SimpleNamespace(has_field=lambda f: False))

        set_taxes_mock = MagicMock(
            side_effect=lambda customer, party_type, posting_date, company, customer_group=None, tax_category=None: (
                "UG Levy 10% - MFU" if tax_category == "Delivery Reduced" else "UG VAT 18% - MFU"
            )
        )
        party_mod = _stub("erpnext.accounts.party", set_taxes=set_taxes_mock)
        erpnext_mod = _stub(
            "erpnext",
            accounts=_stub("erpnext.accounts", party=party_mod),
            controllers=_stub("erpnext.controllers", queries=_stub("erpnext.controllers.queries", item_query=MagicMock())),
        )
        ip_mod = _stub(
            "ury.ury.api.item_price",
            item_price_release_states=MagicMock(return_value=(None, None)),
            price_valid_on=MagicMock(
                side_effect=lambda code, pl, dt, rf, rel, customer=None: Row(
                    price_list_rate=22000.0 if code == "KH-BURGER-18" else 16500.0
                )
            ),
        )
        modules = {
            "frappe": frappe_mock,
            "frappe.utils": utils,
            "frappe.model.document": _stub("frappe.model.document", Document=object),
            "erpnext": erpnext_mod,
            "erpnext.accounts": erpnext_mod.accounts,
            "erpnext.accounts.party": party_mod,
            "erpnext.controllers.queries": erpnext_mod.controllers.queries,
            "ury.brand": _stub("ury.brand", coded_message=lambda k, m="": f"[POS-{k}] {m}"),
            "ury.ury_pos.api": _stub(
                "ury.ury_pos.api",
                getBranch=MagicMock(return_value="Main Branch A"),
                getBranchRoom=MagicMock(),
                getRoom=MagicMock(return_value=[Row(name="Main Hall", branch="Main Branch A")]),
                posOpening=MagicMock(),
            ),
            "ury.ury_pos.default_customer": _stub(
                "ury.ury_pos.default_customer",
                get_default_customer_config=MagicMock(),
                resolve_default_customer=MagicMock(),
            ),
            "ury.ury_pos.override": _stub(
                "ury.ury_pos.override",
                authorize_or_perform=MagicMock(),
                has_verified_approval=MagicMock(return_value=False),
                may_perform=MagicMock(return_value=True),
                consume=MagicMock(),
                mark_verified_approval=MagicMock(),
                _validate_reason=MagicMock(),
                is_company_policy_active=MagicMock(return_value=False),
            ),
            "ury.ury.api.ury_kot_generate": _stub(
                "ury.ury.api.ury_kot_generate", _execute_kot=MagicMock(), process_items_for_cancel_kot=MagicMock()
            ),
            "ury.ury.api.ury_order_reservation_service": _stub(
                "ury.ury.api.ury_order_reservation_service",
                _line_key=MagicMock(),
                _line_ref=MagicMock(),
                reconcile_order_reservations=MagicMock(),
                release_order_reservations=MagicMock(),
                resolve_production_context=MagicMock(return_value=None),
                _warehouse_for_context=MagicMock(),
            ),
            "ury.ury.api.ury_stock_policy": _stub("ury.ury.api.ury_stock_policy", get_branch_stock_policy=MagicMock()),
            "ury.ury.doctype.alert_settings.alert_settings": _stub(
                "ury.ury.doctype.alert_settings.alert_settings", get_alert_rule=MagicMock()
            ),
            "ury.ury.api.ury_kot_notification": _stub(
                "ury.ury.api.ury_kot_notification",
                create_system_notification=MagicMock(),
                get_users_with_role=MagicMock(),
            ),
            "ury.ury.api.item_price": ip_mod,
            "ury.ury.api.table_reservation": _stub(
                "ury.ury.api.table_reservation",
                check_table_reservation=MagicMock(return_value=None),
                parse_to_datetime=MagicMock(return_value=datetime(2026, 11, 24, 19, 0, 0)),
            ),
        }
        with patch.dict(sys.modules, modules):
            _load_module("ury.ury_pos.discount", URY_PKG / "ury_pos/discount.py")
            _load_module("ury.ury_pos.tax_determination", URY_PKG / "ury_pos/tax_determination.py")
            _load_module("ury.ury_pos.dayparts", URY_PKG / "ury_pos/dayparts.py")
            pa_mod = _load_module("ury.ury_pos.pricing_actions", URY_PKG / "ury_pos/pricing_actions.py")
            order_mod = _load_module("ury.ury.doctype.ury_order.ury_order", URY_PKG / "ury/doctype/ury_order/ury_order.py")

            profile = Row(
                name="POS Profile A",
                company="Maximus Foods UG",
                branch="Main Branch A",
                restaurant="Rest A",
                custom_edit_order_type=1,
                transfer_role_permissions=[Row(role="URY Manager")],
                role_allowed_for_billing=[Row(role="URY Cashier")],
                role_restricted_for_table_order=[],
                applicable_for_users=[Row(user="cashier_a@example.com")],
                tax_category="",
                taxes_and_charges="",
                custom_order_type_tax_categories=[
                    Row(order_type="Take Away", tax_category="Take Away VAT"),
                    Row(order_type="Delivery", tax_category="Delivery Reduced"),
                ],
            )
            before_snapshot = Row(
                name="MFUA00036",
                docstatus=0,
                is_return=0,
                order_type="Take Away",
                selling_price_list="Standard UGX",
                custom_is_complimentary=0,
                custom_tax_exempt=0,
                custom_service_charge_waived=0,
                discount_amount=0.0,
                additional_discount_percentage=0.0,
                taxes=[],
                items=[
                    Row(
                        name="ROW-1",
                        item_code="KH-BURGER-18",
                        qty=1,
                        price_list_rate=20000.0,
                        rate=16000.0,
                        custom_price_overridden=0,
                        custom_is_open_item=0,
                        custom_is_complimentary=0,
                    ),
                    Row(
                        name="ROW-UPCHARGE",
                        item_code="KH-COMBO-SIDE-18",
                        qty=1,
                        custom_line_role="Combo Upcharge",
                        custom_parent_line_key="ROW-1",
                        custom_is_modifier=1,
                        price_list_rate=2000.0,
                        base_price_list_rate=2000.0,
                        rate=2000.0,
                    ),
                    Row(
                        name="ROW-OV",
                        item_code="KH-CHICKEN-18",
                        qty=1,
                        price_list_rate=15000.0,
                        rate=12500.0,
                        custom_price_overridden=1,
                        custom_price_override_approver="manager_a@example.com",
                        custom_is_open_item=0,
                        custom_is_complimentary=0,
                    ),
                ],
            )
            class _HookedInvoice(FakeInvoice):
                def get_doc_before_save(self):
                    return before_snapshot

                def precision(self, _f=None):
                    return 0

                def as_dict(self):
                    return dict(self)

                def save(self):
                    pa_mod.validate_privileged_pricing_mutations(self, "validate")

            inv = _HookedInvoice(
                name="MFUA00036",
                docstatus=0,
                is_return=0,
                owner="cashier_a@example.com",
                cashier="cashier_a@example.com",
                waiter="cashier_a@example.com",
                pos_profile="POS Profile A",
                company="Maximus Foods UG",
                branch="Main Branch A",
                restaurant="Rest A",
                order_type="Take Away",
                customer="Walk In Guest",
                selling_price_list="Standard UGX",
                tax_category="Take Away VAT",
                taxes_and_charges="UG VAT 18% - MFU",
                custom_is_complimentary=0,
                custom_tax_exempt=0,
                custom_service_charge_waived=0,
                discount_amount=0.0,
                additional_discount_percentage=0.0,
                items=[
                    Row(
                        name="ROW-1",
                        item_code="KH-BURGER-18",
                        qty=1,
                        price_list_rate=20000.0,
                        rate=16000.0,
                        custom_price_overridden=0,
                        custom_is_open_item=0,
                        custom_is_complimentary=0,
                    ),
                    Row(
                        name="ROW-UPCHARGE",
                        item_code="KH-COMBO-SIDE-18",
                        qty=1,
                        custom_line_role="Combo Upcharge",
                        custom_parent_line_key="ROW-1",
                        custom_is_modifier=1,
                        price_list_rate=2000.0,
                        base_price_list_rate=2000.0,
                        rate=2000.0,
                    ),
                    Row(
                        name="ROW-OV",
                        item_code="KH-CHICKEN-18",
                        qty=1,
                        price_list_rate=15000.0,
                        rate=12500.0,
                        custom_price_overridden=1,
                        custom_price_override_approver="manager_a@example.com",
                        custom_is_open_item=0,
                        custom_is_complimentary=0,
                    ),
                ],
            )
            inv._tax_templates["UG Levy 10% - MFU"] = [
                {"charge_type": "On Net Total", "account_head": "VAT - MFU", "rate": 10.0, "description": "UG Levy 10%"}
            ]

            frappe_mock.get_doc.side_effect = lambda dt, name=None: (
                inv if dt == "POS Invoice"
                else (profile if dt == "POS Profile" else Row(name="Walk In Guest", tax_category=None, customer_group="Individual"))
            )
            frappe_mock.get_cached_doc.side_effect = lambda dt, name=None: profile if dt == "POS Profile" else None
            frappe_mock.db.get_value.side_effect = lambda dt, flt=None, field=None, **kw: (
                22000.0 if (dt == "URY Menu Item" and isinstance(flt, dict) and flt.get("item") == "KH-BURGER-18")
                else (16500.0 if (dt == "URY Menu Item" and isinstance(flt, dict) and flt.get("item") == "KH-CHICKEN-18") else None)
            )

            with (
                patch.object(
                    order_mod,
                    "_dayparts_mod",
                    return_value=SimpleNamespace(
                        resolve_outlet_local_datetime=lambda _b, at=None: (datetime(2026, 11, 24, 18, 16, 0), "18:16:00", "Tue"),
                        resolve_menu_and_price_list=lambda *a, **k: {"menu": "Delivery Menu", "price_list": "Delivery UGX", "serving_period": None},
                        serving_period_applies_to_order_type=lambda *a, **k: True,
                    ),
                ),
                patch.object(order_mod, "price_valid_on", side_effect=lambda code, pl, *a, **k: {"price_list_rate": 22000.0 if code == "KH-BURGER-18" else 16500.0}),
            ):
                order_mod.change_order_type("MFUA00036", "Delivery")

            # Standard item repriced to Delivery UGX (22,000), AND #152 manager-overridden item preserved (12,500)!
            self.assertEqual(inv.selling_price_list, "Delivery UGX")
            self.assertEqual(inv["items"][0].rate, 22000.0)
            upcharge = inv["items"][1]
            self.assertEqual((upcharge.rate, upcharge.price_list_rate, upcharge.base_price_list_rate), (2000.0,) * 3)
            self.assertEqual(inv["items"][2].rate, 12500.0)
            # Server repricing flags must be cleaned up after save() so direct REST tampering remains blocked
            self.assertFalse(getattr(inv.flags, "ury_privileged_action_verified", False))
            self.assertFalse(getattr(inv.flags, "ury_server_order_type_change", False))
            self.assertFalse(getattr(inv.flags, "ury_trusted_server_pricing", False))
            inv["items"][0].rate = 1.0
            with self.assertRaisesRegex(NotPermitted, "POS-AUTH-REQUIRED"):
                pa_mod.validate_privileged_pricing_mutations(inv, "validate")

    def test_preview_order_totals_includes_152_service_charge_rows_and_preserves_sc_descriptions(self):
        """R3 live visual catch: preview_order_totals must run #152 sync_service_charge (preserving
        orig_invoice SC flags) so PaymentDialog Order Summary shows Service Charge + VAT on Service
        Charge rows matching grand_total, and _build_preview_tax_rows must not mangle SC tax labels.
        """
        frappe_mock, utils = _make_frappe_env()
        frappe_mock.session = SimpleNamespace(user="cashier_a@example.com")
        frappe_mock.get_roles = MagicMock(return_value=["URY Cashier"])
        frappe_mock.has_permission = MagicMock(return_value=True)
        frappe_mock.db.get_value.side_effect = lambda dt, flt=None, field=None, **kw: (
            "Rest A" if dt == "URY Restaurant"
            else ("Main Branch A" if dt == "POS Profile" and field == "branch" else None)
        )
        frappe_mock.get_all.return_value = []

        sc_calls = []

        def fake_sync_service_charge(doc, profile=None):
            sc_calls.append(
                {
                    "waived": doc.get("custom_service_charge_waived"),
                    "override_rate": doc.get("custom_service_charge_override_rate"),
                    "no_of_pax": doc.get("no_of_pax"),
                }
            )
            doc.taxes.append(
                Row(
                    charge_type="Actual",
                    account_head="Service Charge Payable - MFU",
                    description="Service Charge",
                    rate=0.0,
                    tax_amount=4700.0,
                    custom_pos_charge_kind="Service Charge",
                )
            )
            doc.taxes.append(
                Row(
                    charge_type="On Previous Row Amount",
                    account_head="VAT - MFU",
                    description="VAT on Service Charge",
                    rate=18.0,
                    tax_amount=846.0,
                    custom_pos_charge_kind="Service Charge Tax",
                )
            )

        class _PreviewInvoice(FakeInvoice):
            def precision(self, _f=None):
                return 0

            def calculate_taxes_and_totals(self):
                self.net_total = 47000.0
                self.total_taxes_and_charges = sum(float(t.get("tax_amount") or 0) for t in self.taxes)
                self.grand_total = self.net_total + self.total_taxes_and_charges
                self.rounded_total = self.grand_total
                self.rounding_adjustment = 0.0

        preview_doc = _PreviewInvoice(
            taxes=[
                Row(
                    charge_type="On Net Total",
                    account_head="VAT - MFU",
                    description="VAT",
                    rate=18.0,
                    tax_amount=8460.0,
                    custom_pos_charge_kind="",
                )
            ]
        )
        orig_invoice = Row(
            name="MFUA00090",
            branch="Main Branch A",
            pos_profile="POS Profile A",
            order_type="Dine In",
            customer="Walk In Guest",
            no_of_pax=4,
            restaurant_table="Table A-Free",
            custom_serving_period="All Day Dining",
            custom_transaction_local_time="21:55:00",
            custom_transaction_weekday="Tue",
            selling_price_list="Standard UGX",
            custom_service_charge_waived=0,
            custom_service_charge_override_rate=12.5,
            custom_service_charge_override_amount=0.0,
            items=[
                Row(
                    item_code="KH-BURGER-18",
                    item_name="Classic Kampalan Burger",
                    qty=2,
                    rate=16000.0,
                    price_list_rate=20000.0,
                    item_tax_template="Item VAT 18% - MFU",
                    item_tax_rate='{"VAT - MFU": 18.0}',
                ),
                Row(
                    item_code="KH-NO-EFRIS-CODE",
                    item_name="Unmapped Local Snack",
                    qty=1,
                    rate=15000.0,
                    price_list_rate=15000.0,
                    item_tax_template="Item VAT 18% - MFU",
                    item_tax_rate='{"VAT - MFU": 18.0}',
                ),
            ],
        )
        profile = Row(
            name="POS Profile A",
            company="Maximus Foods UG",
            currency="UGX",
            branch="Main Branch A",
            restaurant="Rest A",
            selling_price_list="Standard UGX",
            cost_center="Main - MFU",
            warehouse="Stores - MFU",
            applicable_for_users=[Row(user="cashier_a@example.com")],
            customer_groups=[],
        )
        frappe_mock.new_doc = MagicMock(return_value=preview_doc)
        frappe_mock.get_doc = MagicMock(
            side_effect=lambda dt, name=None: orig_invoice if dt == "POS Invoice" else profile
        )
        modules = {
            "frappe": frappe_mock,
            "frappe.utils": utils,
            "ury.brand": _stub("ury.brand", coded_message=lambda k, m="": f"[POS-{k}] {m}"),
            "ury.ury_pos.dayparts": _stub(
                "ury.ury_pos.dayparts",
                resolve_menu_and_price_list=MagicMock(return_value={"price_list": "Standard UGX", "serving_period": None}),
                stamp_invoice_outlet_time=MagicMock(return_value={"local_dt": datetime(2026, 11, 24, 21, 55, 0), "local_time": "21:55:00"}),
            ),
            "ury.ury_pos.default_customer": _stub(
                "ury.ury_pos.default_customer",
                resolve_default_customer=MagicMock(return_value="Walk In Guest"),
                get_default_customer_config=MagicMock(return_value={}),
            ),
            "ury.ury_pos.tax_determination": _stub(
                "ury.ury_pos.tax_determination",
                apply_order_type_tax=MagicMock(),
                validate_efris_readiness=MagicMock(return_value=[]),
            ),
            "ury.ury_pos.pricing_actions": _stub(
                "ury.ury_pos.pricing_actions",
                sync_service_charge=fake_sync_service_charge,
            ),
        }
        with patch.dict(sys.modules, modules):
            api_mod = _load_module("ury.ury_pos.api", URY_PKG / "ury_pos/api.py")
            with (
                patch.object(api_mod, "_authorize_pos_profile_access", MagicMock()),
                patch.object(api_mod, "_authorize_customer_for_pos_profile", MagicMock(return_value=Row(name="Walk In Guest"))),
            ):
                res = api_mod.preview_order_totals(pos_profile="POS Profile A", invoice_name="MFUA00090")
            # Also verify _build_preview_tax_rows never rewrites Service Charge Tax even when header_rate differs from item_tax_rate
            preview_doc.taxes[-1]["rate"] = 10.0  # different from item's 18.0 VAT - MFU
            rows_checked = api_mod._build_preview_tax_rows(preview_doc, 0)

        self.assertEqual(len(sc_calls), 1)
        self.assertEqual(sc_calls[0]["no_of_pax"], 4)
        self.assertEqual(sc_calls[0]["override_rate"], 12.5)
        descs = [r["description"] for r in res["tax_rows"]]
        self.assertIn("Service Charge", descs)
        self.assertIn("VAT on Service Charge", descs)
        self.assertEqual(res["grand_total"], 61006.0)
        sc_tax_row = next(r for r in rows_checked if r["tax_amount"] == 846.0)
        self.assertEqual(sc_tax_row["description"], "VAT on Service Charge")
        self.assertFalse(sc_tax_row["item_rate_override"])

    def test_preview_order_totals_preserves_152_overrides_open_items_and_item_complimentary_without_double_discount(self):
        """R4 BLOCKING-1 RED: preview_order_totals(invoice_name=...) must preserve #152 privileged
        row flags (override, open item, item complimentary, ignore_pricing_rule) and shield them
        from set_missing_values(for_validate=True) re-applying PRLE pricing rules, while keeping
        normal PRLE sibling rows discounted and emitting custom_pos_charge_kind on tax_rows.
        """
        frappe_mock, utils = _make_frappe_env()
        frappe_mock.session = SimpleNamespace(user="cashier_a@example.com")
        frappe_mock.get_roles = MagicMock(return_value=["URY Cashier"])
        frappe_mock.has_permission = MagicMock(return_value=True)
        frappe_mock.db.get_value.side_effect = lambda dt, flt=None, field=None, **kw: (
            "Rest A"
            if dt == "URY Restaurant"
            else ("Main Branch A" if dt == "POS Profile" and field == "branch" else None)
        )
        frappe_mock.get_all.return_value = []

        class _PrlePreviewInvoice(FakeInvoice):
            """Simulates real ERPNext v16 POSInvoice.set_missing_values(for_validate=True),
            where POS Invoice Item has no ignore_pricing_rule column and apply_pricing_rule()
            applies PRLE-0008 (-20%) to EVERY row unless shielded."""

            def precision(self, _f=None):
                return 0

            def set_missing_values(self, for_validate=False):
                super().set_missing_values(for_validate=for_validate)
                if _cint(self.get("ignore_pricing_rule")) == 1:
                    return
                self["pricing_rules"] = []
                for idx, r in enumerate(self.get("items") or [], start=1):
                    r.setdefault("name", f"PREVIEW-ROW-{idx}")
                    plr = float(r.get("price_list_rate") or 0)
                    r["discount_percentage"] = 20.0
                    r["discount_amount"] = round(plr * 0.20, 0)
                    r["rate"] = round(plr * 0.80, 0)
                    r["pricing_rules"] = '["PRLE-0008"]'
                    self["pricing_rules"].append(
                        Row(pricing_rule="PRLE-0008", item_code=r["item_code"], child_docname=r["name"])
                    )

            def calculate_taxes_and_totals(self):
                prec = self.precision("grand_total")
                net = 0.0
                for r in self.get("items") or []:
                    plr = float(r.get("price_list_rate") or 0)
                    disc_pct = float(r.get("discount_percentage") or 0)
                    if disc_pct > 0:
                        eff_rate = round(plr * (1.0 - disc_pct / 100.0), prec)
                    else:
                        eff_rate = round(float(r.get("rate") or 0), prec)
                    r["rate"] = eff_rate
                    r["amount"] = round(eff_rate * float(r.get("qty") or 0), prec)
                    r["net_amount"] = r["amount"]
                    net += r["net_amount"]
                self["total"] = round(net, prec)
                self["net_total"] = round(net, prec)
                tax_total = 0.0
                for t in self.get("taxes") or []:
                    t["tax_amount"] = round(net * float(t.get("rate") or 0) / 100.0, prec)
                    tax_total += t["tax_amount"]
                self["total_taxes_and_charges"] = round(tax_total, prec)
                self["grand_total"] = round(net + tax_total, prec)
                self["rounded_total"] = self["grand_total"]
                self["rounding_adjustment"] = 0.0

        profile = Row(
            name="POS Profile A",
            company="Kampala House Hospitality",
            currency="UGX",
            branch="Main Branch A",
            restaurant="Rest A",
            selling_price_list="Kampala Menu",
            cost_center="Main - KHH",
            warehouse="Stores - KHH",
            applicable_for_users=[Row(user="cashier_a@example.com")],
            customer_groups=[],
        )

        # Case 1: Overridden (12,500) + PRLE sibling (20,000 -> 16,000) + Open Item (14,000) = 42,500 net / 50,150 gross
        orig_override_inv = Row(
            name="KHH-00062",
            branch="Main Branch A",
            pos_profile="POS Profile A",
            order_type="Take Away",
            customer="Walk-in Guest - KHH",
            no_of_pax=1,
            custom_serving_period="Happy Hour",
            custom_transaction_local_time="23:01:33",
            custom_transaction_weekday="Tue",
            selling_price_list="Kampala Menu",
            custom_is_complimentary=0,
            items=[
                Row(
                    name="row-ov",
                    item_code="KH-BURGER-18",
                    item_name="Classic Beef Burger",
                    qty=1.0,
                    price_list_rate=12500.0,
                    base_price_list_rate=12500.0,
                    rate=12500.0,
                    base_rate=12500.0,
                    discount_percentage=0.0,
                    discount_amount=0.0,
                    pricing_rules=None,
                    ignore_pricing_rule=0,
                    custom_price_overridden=1,
                    custom_is_open_item=0,
                    custom_is_complimentary=0,
                    item_tax_template="UG VAT 18% - KHH - KHH",
                    item_tax_rate='{"VAT 18% Output - KHH": 18.0}',
                ),
                Row(
                    name="row-prle",
                    item_code="KH-BURGER-SPICY-18",
                    item_name="Spicy Beef Burger",
                    qty=1.0,
                    price_list_rate=20000.0,
                    base_price_list_rate=20000.0,
                    rate=16000.0,
                    base_rate=16000.0,
                    discount_percentage=20.0,
                    discount_amount=4000.0,
                    pricing_rules='["PRLE-0008"]',
                    ignore_pricing_rule=0,
                    custom_price_overridden=0,
                    custom_is_open_item=0,
                    custom_is_complimentary=0,
                    item_tax_template="UG VAT 18% - KHH - KHH",
                    item_tax_rate='{"VAT 18% Output - KHH": 18.0}',
                ),
                Row(
                    name="row-open",
                    item_code="KH-OPEN-ITEM",
                    item_name="Chef Special",
                    qty=1.0,
                    price_list_rate=14000.0,
                    base_price_list_rate=14000.0,
                    rate=14000.0,
                    base_rate=14000.0,
                    discount_percentage=0.0,
                    discount_amount=0.0,
                    pricing_rules=None,
                    ignore_pricing_rule=0,
                    custom_price_overridden=0,
                    custom_is_open_item=1,
                    custom_is_complimentary=0,
                    item_tax_template="UG VAT 18% - KHH - KHH",
                    item_tax_rate='{"VAT 18% Output - KHH": 18.0}',
                ),
            ],
        )

        # Case 2: Item-scope complimentary (KH-BURGER-18, discount_percentage=100, rate=0) -> net 0 / grand 0
        orig_comp_inv = Row(
            name="KHH-00068",
            branch="Main Branch A",
            pos_profile="POS Profile A",
            order_type="Take Away",
            customer="Walk-in Guest - KHH",
            no_of_pax=1,
            custom_serving_period="Happy Hour",
            custom_transaction_local_time="23:01:33",
            custom_transaction_weekday="Tue",
            selling_price_list="Kampala Menu",
            custom_is_complimentary=1,
            items=[
                Row(
                    name="row-comp",
                    item_code="KH-BURGER-18",
                    item_name="Classic Beef Burger",
                    qty=1.0,
                    price_list_rate=20000.0,
                    base_price_list_rate=20000.0,
                    rate=0.0,
                    base_rate=0.0,
                    discount_percentage=100.0,
                    discount_amount=20000.0,
                    pricing_rules=None,
                    ignore_pricing_rule=0,
                    custom_price_overridden=0,
                    custom_is_open_item=0,
                    custom_is_complimentary=1,
                    item_tax_template="UG VAT 18% - KHH - KHH",
                    item_tax_rate='{"VAT 18% Output - KHH": 18.0}',
                ),
            ],
        )

        created_docs = []

        def _new_doc(_dt):
            d = _PrlePreviewInvoice(
                taxes=[
                    Row(
                        charge_type="On Net Total",
                        account_head="VAT 18% Output - KHH",
                        description="VAT 18%",
                        rate=18.0,
                        tax_amount=0.0,
                        custom_pos_charge_kind="",
                    )
                ]
            )
            created_docs.append(d)
            return d

        frappe_mock.new_doc = MagicMock(side_effect=_new_doc)
        frappe_mock.get_doc = MagicMock(
            side_effect=lambda dt, name=None: (
                orig_override_inv
                if dt == "POS Invoice" and name == "KHH-00062"
                else (orig_comp_inv if dt == "POS Invoice" and name == "KHH-00068" else profile)
            )
        )
        modules = {
            "frappe": frappe_mock,
            "frappe.utils": utils,
            "ury.brand": _stub("ury.brand", coded_message=lambda k, m="": f"[POS-{k}] {m}"),
            "ury.ury_pos.dayparts": _stub(
                "ury.ury_pos.dayparts",
                resolve_menu_and_price_list=MagicMock(return_value={"price_list": "Kampala Menu", "serving_period": "Happy Hour"}),
                stamp_invoice_outlet_time=MagicMock(return_value={"local_dt": datetime(2026, 11, 24, 23, 1, 33), "local_time": "23:01:33"}),
            ),
            "ury.ury_pos.default_customer": _stub(
                "ury.ury_pos.default_customer",
                resolve_default_customer=MagicMock(return_value="Walk-in Guest - KHH"),
                get_default_customer_config=MagicMock(return_value={}),
            ),
            "ury.ury_pos.tax_determination": _stub(
                "ury.ury_pos.tax_determination",
                apply_order_type_tax=MagicMock(),
                validate_efris_readiness=MagicMock(return_value=[]),
            ),
            "ury.ury_pos.pricing_actions": _stub(
                "ury.ury_pos.pricing_actions",
                sync_service_charge=MagicMock(),
            ),
        }
        with patch.dict(sys.modules, modules):
            api_mod = _load_module("ury.ury_pos.api", URY_PKG / "ury_pos/api.py")
            with (
                patch.object(api_mod, "_authorize_pos_profile_access", MagicMock()),
                patch.object(api_mod, "_authorize_customer_for_pos_profile", MagicMock(return_value=Row(name="Walk-in Guest - KHH"))),
            ):
                res_ov = api_mod.preview_order_totals(pos_profile="POS Profile A", invoice_name="KHH-00062")
                res_comp = api_mod.preview_order_totals(pos_profile="POS Profile A", invoice_name="KHH-00068")

        # Case 1: Overridden (12,500) + PRLE sibling (16,000) + Open Item (14,000) = 42,500 net / 50,150 gross
        self.assertEqual(res_ov["net_total"], 42500.0)
        self.assertEqual(res_ov["grand_total"], 50150.0)
        self.assertEqual([r["rate"] for r in res_ov["item_taxes"]], [12500.0, 16000.0, 14000.0])
        # Privileged flags preserved on cloned in-memory preview doc
        ov_doc = created_docs[0]
        self.assertEqual(ov_doc["items"][0].get("custom_price_overridden"), 1)
        self.assertEqual(ov_doc["items"][0].get("ignore_pricing_rule"), 1)
        self.assertEqual(ov_doc["items"][2].get("custom_is_open_item"), 1)
        self.assertEqual(ov_doc["items"][2].get("ignore_pricing_rule"), 1)

        # Case 2: Item-scope complimentary stays 0.0 / 0.0 (never repriced back to 16,000 / 18,880!)
        self.assertEqual(res_comp["net_total"], 0.0)
        self.assertEqual(res_comp["grand_total"], 0.0)
        self.assertEqual(res_comp["item_taxes"][0]["rate"], 0.0)
        comp_doc = created_docs[1]
        self.assertEqual(comp_doc["items"][0].get("custom_is_complimentary"), 1)
        self.assertEqual(comp_doc["items"][0].get("discount_percentage"), 100.0)

    def test_preview_order_totals_with_items_and_invoice_name_preserves_152_overrides_open_comp_and_delivery(self):
        """R5 BLOCKER-R4-1 RED: preview_order_totals called with BOTH invoice_name AND non-empty items
        (OrderPanel edit / table re-open path) must preserve #152 overridden rates, open-item rates &
        names, complimentary zeroing, and Delivery order-type pricing for matched saved rows, while
        pricing newly added unsaved cart lines via daypart/menu + PRLE rules.
        """
        frappe_mock, utils = _make_frappe_env()
        frappe_mock.session = SimpleNamespace(user="cashier_a@example.com")
        frappe_mock.get_roles = MagicMock(return_value=["URY Cashier"])
        frappe_mock.has_permission = MagicMock(return_value=True)
        frappe_mock.db.get_value.side_effect = lambda dt, flt=None, field=None, **kw: (
            "Rest A"
            if dt == "URY Restaurant"
            else (
                "Main Branch A"
                if dt == "POS Profile" and field == "branch"
                else (
                    "Classic Beef Burger"
                    if dt == "Item" and flt == "KH-BURGER-18"
                    else (
                        "Spicy Beef Burger"
                        if dt == "Item" and flt == "KH-BURGER-SPICY-18"
                        else ("Chef Special Open" if dt == "Item" and flt == "KH-OPEN-ITEM" else None)
                    )
                )
            )
        )
        frappe_mock.get_all.return_value = []

        price_book = {
            ("KH-BURGER-18", "Kampala Menu"): 20000.0,
            ("KH-BURGER-SPICY-18", "Kampala Menu"): 20000.0,
            ("KH-OPEN-ITEM", "Kampala Menu"): 15000.0,
            ("KH-BURGER-18", "KHH Delivery Menu"): 22000.0,
            ("KH-BURGER-SPICY-18", "KHH Delivery Menu"): 22000.0,
            ("KH-OPEN-ITEM", "KHH Delivery Menu"): 15000.0,
        }

        def _fake_price_valid_on(code, pl, *_a, **_k):
            rate = price_book.get((code, pl))
            return Row(price_list_rate=rate) if rate is not None else None

        class _EditPrlePreviewInvoice(FakeInvoice):
            def precision(self, _f=None):
                return 0

            def set_missing_values(self, for_validate=False):
                super().set_missing_values(for_validate=for_validate)
                if _cint(self.get("ignore_pricing_rule")) == 1:
                    return
                self["pricing_rules"] = []
                for idx, r in enumerate(self.get("items") or [], start=1):
                    r.setdefault("name", f"PREVIEW-EDIT-ROW-{idx}")
                    if r.get("item_code") in ("KH-BURGER-18", "KH-BURGER-SPICY-18"):
                        plr = float(r.get("price_list_rate") or 0)
                        r["discount_percentage"] = 20.0
                        r["discount_amount"] = round(plr * 0.20, 0)
                        r["rate"] = round(plr * 0.80, 0)
                        r["pricing_rules"] = '["PRLE-0008"]'
                        self["pricing_rules"].append(
                            Row(pricing_rule="PRLE-0008", item_code=r["item_code"], child_docname=r["name"])
                        )

            def calculate_taxes_and_totals(self):
                prec = self.precision("grand_total")
                net = 0.0
                for r in self.get("items") or []:
                    plr = float(r.get("price_list_rate") or 0)
                    disc_pct = float(r.get("discount_percentage") or 0)
                    if disc_pct > 0:
                        eff_rate = round(plr * (1.0 - disc_pct / 100.0), prec)
                    else:
                        eff_rate = round(float(r.get("rate") or 0), prec)
                    r["rate"] = eff_rate
                    r["amount"] = round(eff_rate * float(r.get("qty") or 0), prec)
                    r["net_amount"] = r["amount"]
                    net += r["net_amount"]
                self["total"] = round(net, prec)
                self["net_total"] = round(net, prec)
                tax_total = 0.0
                for t in self.get("taxes") or []:
                    t["tax_amount"] = round(net * float(t.get("rate") or 0) / 100.0, prec)
                    tax_total += t["tax_amount"]
                self["total_taxes_and_charges"] = round(tax_total, prec)
                self["grand_total"] = round(net + tax_total, prec)
                self["rounded_total"] = self["grand_total"]
                self["rounding_adjustment"] = 0.0

        def _fake_apply_order_type_tax(doc, _profile, customer_doc=None, posting_date=None):
            if doc.get("order_type") == "Delivery":
                doc.tax_category = "TC Delivery - KHH"
                doc.taxes_and_charges = "STC Delivery 10% - KHH - KHH"
                doc["taxes"] = [
                    Row(
                        charge_type="On Net Total",
                        account_head="VAT 10% Output - KHH",
                        description="Delivery Levy 10%",
                        rate=10.0,
                        tax_amount=0.0,
                        custom_pos_charge_kind="",
                    )
                ]
            else:
                doc.tax_category = "TC Take Away - KHH"
                doc.taxes_and_charges = "STC Take Away 18% - KHH - KHH"
                doc["taxes"] = [
                    Row(
                        charge_type="On Net Total",
                        account_head="VAT 18% Output - KHH",
                        description="VAT 18%",
                        rate=18.0,
                        tax_amount=0.0,
                        custom_pos_charge_kind="",
                    )
                ]

        profile = Row(
            name="POS Profile A",
            company="Kampala House Hospitality",
            currency="UGX",
            branch="Main Branch A",
            restaurant="Rest A",
            selling_price_list="Kampala Menu",
            cost_center="Main - KHH",
            warehouse="Stores - KHH",
            applicable_for_users=[Row(user="cashier_a@example.com")],
            customer_groups=[],
        )

        # Draft 1: Take Away #152 override (12,500) + PRLE sibling (16,000) + Open Item (14,000) = 42,500 / 50,150
        orig_takeaway = Row(
            name="KHH00022",
            branch="Main Branch A",
            pos_profile="POS Profile A",
            order_type="Take Away",
            customer="Walk-in Guest - KHH",
            no_of_pax=1,
            custom_serving_period="Happy Hour",
            custom_transaction_local_time="08:51:00",
            custom_transaction_weekday="Wed",
            selling_price_list="Kampala Menu",
            custom_is_complimentary=0,
            items=[
                Row(
                    name="row-ov",
                    reservation_line_key="rk-ov",
                    item_code="KH-BURGER-18",
                    item_name="Classic Beef Burger",
                    qty=1.0,
                    price_list_rate=12500.0,
                    base_price_list_rate=12500.0,
                    rate=12500.0,
                    base_rate=12500.0,
                    discount_percentage=0.0,
                    discount_amount=0.0,
                    pricing_rules=None,
                    ignore_pricing_rule=0,
                    custom_price_overridden=1,
                    custom_is_open_item=0,
                    custom_is_complimentary=0,
                    item_tax_template="UG VAT 18% - KHH - KHH",
                    item_tax_rate='{"VAT 18% Output - KHH": 18.0}',
                ),
                Row(
                    name="row-prle",
                    reservation_line_key="rk-prle",
                    item_code="KH-BURGER-SPICY-18",
                    item_name="Spicy Beef Burger",
                    qty=1.0,
                    price_list_rate=20000.0,
                    base_price_list_rate=20000.0,
                    rate=16000.0,
                    base_rate=16000.0,
                    discount_percentage=20.0,
                    discount_amount=4000.0,
                    pricing_rules='["PRLE-0008"]',
                    ignore_pricing_rule=0,
                    custom_price_overridden=0,
                    custom_is_open_item=0,
                    custom_is_complimentary=0,
                    item_tax_template="UG VAT 18% - KHH - KHH",
                    item_tax_rate='{"VAT 18% Output - KHH": 18.0}',
                ),
                Row(
                    name="row-open",
                    reservation_line_key="",
                    item_code="KH-OPEN-ITEM",
                    item_name="Chef Special Open - Chef Special R4",
                    qty=1.0,
                    price_list_rate=14000.0,
                    base_price_list_rate=14000.0,
                    rate=14000.0,
                    base_rate=14000.0,
                    discount_percentage=0.0,
                    discount_amount=0.0,
                    pricing_rules=None,
                    ignore_pricing_rule=0,
                    custom_price_overridden=0,
                    custom_is_open_item=1,
                    custom_is_complimentary=0,
                    item_tax_template="UG VAT 18% - KHH - KHH",
                    item_tax_rate='{"VAT 18% Output - KHH": 18.0}',
                ),
            ],
        )

        # Draft 2: Item-scope complimentary (0 / 0)
        orig_comp = Row(
            name="KHH00019",
            branch="Main Branch A",
            pos_profile="POS Profile A",
            order_type="Take Away",
            customer="Walk-in Guest - KHH",
            no_of_pax=1,
            custom_serving_period="Happy Hour",
            custom_transaction_local_time="08:51:00",
            custom_transaction_weekday="Wed",
            selling_price_list="Kampala Menu",
            custom_is_complimentary=1,
            items=[
                Row(
                    name="row-comp",
                    reservation_line_key="rk-comp",
                    item_code="KH-BURGER-18",
                    item_name="Classic Beef Burger",
                    qty=1.0,
                    price_list_rate=20000.0,
                    base_price_list_rate=20000.0,
                    rate=0.0,
                    base_rate=0.0,
                    discount_percentage=100.0,
                    discount_amount=20000.0,
                    pricing_rules=None,
                    ignore_pricing_rule=0,
                    custom_price_overridden=0,
                    custom_is_open_item=0,
                    custom_is_complimentary=1,
                    item_tax_template="UG VAT 18% - KHH - KHH",
                    item_tax_rate='{"VAT 18% Output - KHH": 18.0}',
                ),
            ],
        )

        # Draft 3: Delivery #152 check on KHH Delivery Menu (12,500 + 17,600 + 14,000 = 44,100 / 48,510)
        orig_delivery = Row(
            name="KHH00023",
            branch="Main Branch A",
            pos_profile="POS Profile A",
            order_type="Delivery",
            customer="Walk-in Guest - KHH",
            no_of_pax=1,
            custom_serving_period="Happy Hour",
            custom_transaction_local_time="08:51:00",
            custom_transaction_weekday="Wed",
            selling_price_list="KHH Delivery Menu",
            custom_is_complimentary=0,
            items=[
                Row(
                    name="row-d-ov",
                    reservation_line_key="rk-d-ov",
                    item_code="KH-BURGER-18",
                    item_name="Classic Beef Burger",
                    qty=1.0,
                    price_list_rate=12500.0,
                    base_price_list_rate=12500.0,
                    rate=12500.0,
                    base_rate=12500.0,
                    discount_percentage=0.0,
                    discount_amount=0.0,
                    pricing_rules=None,
                    ignore_pricing_rule=0,
                    custom_price_overridden=1,
                    custom_is_open_item=0,
                    custom_is_complimentary=0,
                ),
                Row(
                    name="row-d-prle",
                    reservation_line_key="rk-d-prle",
                    item_code="KH-BURGER-SPICY-18",
                    item_name="Spicy Beef Burger",
                    qty=1.0,
                    price_list_rate=22000.0,
                    base_price_list_rate=22000.0,
                    rate=17600.0,
                    base_rate=17600.0,
                    discount_percentage=20.0,
                    discount_amount=4400.0,
                    pricing_rules='["PRLE-0008"]',
                    ignore_pricing_rule=0,
                    custom_price_overridden=0,
                    custom_is_open_item=0,
                    custom_is_complimentary=0,
                ),
                Row(
                    name="row-d-open",
                    reservation_line_key="",
                    item_code="KH-OPEN-ITEM",
                    item_name="Chef Special Delivery",
                    qty=1.0,
                    price_list_rate=14000.0,
                    base_price_list_rate=14000.0,
                    rate=14000.0,
                    base_rate=14000.0,
                    discount_percentage=0.0,
                    discount_amount=0.0,
                    pricing_rules=None,
                    ignore_pricing_rule=0,
                    custom_price_overridden=0,
                    custom_is_open_item=1,
                    custom_is_complimentary=0,
                ),
            ],
        )

        invoices = {
            "KHH00022": orig_takeaway,
            "KHH00019": orig_comp,
            "KHH00023": orig_delivery,
        }
        created_docs = []

        def _new_doc(_dt):
            d = _EditPrlePreviewInvoice()
            created_docs.append(d)
            return d

        frappe_mock.new_doc = MagicMock(side_effect=_new_doc)
        frappe_mock.get_doc = MagicMock(
            side_effect=lambda dt, name=None: invoices[name] if dt == "POS Invoice" else profile
        )

        def _fake_resolve_menu(branch, restaurant=None, table=None, room=None, order_type=None, customer=None, local_dt=None):
            return {
                "price_list": "KHH Delivery Menu" if order_type == "Delivery" else "Kampala Menu",
                "serving_period": "Happy Hour",
            }

        modules = {
            "frappe": frappe_mock,
            "frappe.utils": utils,
            "ury.brand": _stub("ury.brand", coded_message=lambda k, m="": f"[POS-{k}] {m}"),
            "ury.ury.api.item_price": _stub(
                "ury.ury.api.item_price",
                item_price_release_states=MagicMock(return_value=(None, None)),
                price_valid_on=MagicMock(side_effect=_fake_price_valid_on),
            ),
            "ury.ury_pos.dayparts": _stub(
                "ury.ury_pos.dayparts",
                resolve_menu_and_price_list=MagicMock(side_effect=_fake_resolve_menu),
                stamp_invoice_outlet_time=MagicMock(
                    return_value={"local_dt": datetime(2026, 11, 25, 8, 51, 0), "local_time": "08:51:00"}
                ),
            ),
            "ury.ury_pos.default_customer": _stub(
                "ury.ury_pos.default_customer",
                resolve_default_customer=MagicMock(return_value="Walk-in Guest - KHH"),
                get_default_customer_config=MagicMock(return_value={}),
            ),
            "ury.ury_pos.tax_determination": _stub(
                "ury.ury_pos.tax_determination",
                apply_order_type_tax=MagicMock(side_effect=_fake_apply_order_type_tax),
                validate_efris_readiness=MagicMock(return_value=[]),
            ),
            "ury.ury_pos.pricing_actions": _stub(
                "ury.ury_pos.pricing_actions",
                sync_service_charge=MagicMock(),
            ),
        }

        with patch.dict(sys.modules, modules):
            api_mod = _load_module("ury.ury_pos.api", URY_PKG / "ury_pos/api.py")
            with (
                patch.object(api_mod, "_authorize_pos_profile_access", MagicMock()),
                patch.object(
                    api_mod,
                    "_authorize_customer_for_pos_profile",
                    MagicMock(return_value=Row(name="Walk-in Guest - KHH")),
                ),
            ):
                # 1. OrderPanel edit payload WITH name + reservation_line_key on KHH00022
                res_edit_keyed = api_mod.preview_order_totals(
                    pos_profile="POS Profile A",
                    order_type="Take Away",
                    invoice_name="KHH00022",
                    items=[
                        {"name": "row-ov", "reservation_line_key": "rk-ov", "item": "KH-BURGER-18", "qty": 1, "rate": 12500},
                        {"name": "row-prle", "reservation_line_key": "rk-prle", "item": "KH-BURGER-SPICY-18", "qty": 1, "rate": 16000},
                        {"name": "row-open", "reservation_line_key": "row-open", "item": "KH-OPEN-ITEM", "qty": 1, "rate": 14000},
                    ],
                )
                # 2. Legacy/HTTP payload WITHOUT name/reservation_line_key on KHH00022
                res_edit_unkeyed = api_mod.preview_order_totals(
                    pos_profile="POS Profile A",
                    order_type="Take Away",
                    invoice_name="KHH00022",
                    items=[
                        {"item": "KH-BURGER-18", "qty": 1, "rate": 12500},
                        {"item": "KH-BURGER-SPICY-18", "qty": 1, "rate": 16000},
                        {"item": "KH-OPEN-ITEM", "qty": 1, "rate": 14000},
                    ],
                )
                # 3. Edit qty on overridden row (1 -> 2) + add a NEW unsaved KH-BURGER-18 line
                res_edit_qty_and_new = api_mod.preview_order_totals(
                    pos_profile="POS Profile A",
                    order_type="Take Away",
                    invoice_name="KHH00022",
                    items=[
                        {"name": "row-ov", "reservation_line_key": "rk-ov", "item": "KH-BURGER-18", "qty": 2, "rate": 12500},
                        {"name": "row-prle", "reservation_line_key": "rk-prle", "item": "KH-BURGER-SPICY-18", "qty": 1, "rate": 16000},
                        {"name": "row-open", "reservation_line_key": "row-open", "item": "KH-OPEN-ITEM", "qty": 1, "rate": 14000},
                        {"reservation_line_key": "new-line-1", "item": "KH-BURGER-18", "qty": 1, "rate": 20000},
                    ],
                )
                # 4. Complimentary draft with non-empty items
                res_comp_items = api_mod.preview_order_totals(
                    pos_profile="POS Profile A",
                    order_type="Take Away",
                    invoice_name="KHH00019",
                    items=[
                        {"name": "row-comp", "reservation_line_key": "rk-comp", "item": "KH-BURGER-18", "qty": 1, "rate": 0},
                    ],
                )
                # 5. Delivery draft KHH00023 with non-empty items
                res_delivery_items = api_mod.preview_order_totals(
                    pos_profile="POS Profile A",
                    order_type="Delivery",
                    invoice_name="KHH00023",
                    items=[
                        {"name": "row-d-ov", "reservation_line_key": "rk-d-ov", "item": "KH-BURGER-18", "qty": 1, "rate": 12500},
                        {"name": "row-d-prle", "reservation_line_key": "rk-d-prle", "item": "KH-BURGER-SPICY-18", "qty": 1, "rate": 17600},
                        {"name": "row-d-open", "reservation_line_key": "row-d-open", "item": "KH-OPEN-ITEM", "qty": 1, "rate": 14000},
                    ],
                )
                # 6. Order-type switch while editing KHH00022 (Take Away -> Delivery)
                res_switch_delivery = api_mod.preview_order_totals(
                    pos_profile="POS Profile A",
                    order_type="Delivery",
                    invoice_name="KHH00022",
                    items=[
                        {"name": "row-ov", "reservation_line_key": "rk-ov", "item": "KH-BURGER-18", "qty": 1, "rate": 12500},
                        {"name": "row-prle", "reservation_line_key": "rk-prle", "item": "KH-BURGER-SPICY-18", "qty": 1, "rate": 16000},
                        {"name": "row-open", "reservation_line_key": "row-open", "item": "KH-OPEN-ITEM", "qty": 1, "rate": 14000},
                    ],
                )

        # 1. Keyed edit parity (exact DB 42,500 / 50,150, NOT 47,000 / 55,460)
        self.assertEqual(res_edit_keyed["net_total"], 42500.0)
        self.assertEqual(res_edit_keyed["grand_total"], 50150.0)
        self.assertEqual([r["rate"] for r in res_edit_keyed["item_taxes"]], [12500.0, 16000.0, 14000.0])
        self.assertEqual(created_docs[0]["items"][2].get("item_name"), "Chef Special Open - Chef Special R4")

        # 2. Unkeyed edit parity
        self.assertEqual(res_edit_unkeyed["net_total"], 42500.0)
        self.assertEqual(res_edit_unkeyed["grand_total"], 50150.0)
        self.assertEqual([r["rate"] for r in res_edit_unkeyed["item_taxes"]], [12500.0, 16000.0, 14000.0])

        # 3. Qty bump on overridden line (12,500 x 2 = 25,000) + brand-new unsaved same item_code priced via PRLE (16,000)
        self.assertEqual(res_edit_qty_and_new["net_total"], 71000.0)
        self.assertEqual(res_edit_qty_and_new["grand_total"], 83780.0)
        self.assertEqual([r["rate"] for r in res_edit_qty_and_new["item_taxes"]], [12500.0, 16000.0, 14000.0, 16000.0])
        self.assertEqual(
            [r["net_amount"] for r in res_edit_qty_and_new["item_taxes"]],
            [25000.0, 16000.0, 14000.0, 16000.0],
        )

        # 4. Complimentary draft stays 0 / 0 with non-empty items (NOT 16,000 / 18,880)
        self.assertEqual(res_comp_items["net_total"], 0.0)
        self.assertEqual(res_comp_items["grand_total"], 0.0)
        self.assertEqual(res_comp_items["item_taxes"][0]["rate"], 0.0)

        # 5. Delivery draft stays 44,100 / 48,510 with non-empty items (NOT 50,200 / 55,220)
        self.assertEqual(res_delivery_items["net_total"], 44100.0)
        self.assertEqual(res_delivery_items["grand_total"], 48510.0)
        self.assertEqual([r["rate"] for r in res_delivery_items["item_taxes"]], [12500.0, 17600.0, 14000.0])

        # 6. Order-type switch Take Away -> Delivery preserves #152 override (12,500) & open (14,000), reprices standard sibling (17,600)
        self.assertEqual(res_switch_delivery["net_total"], 44100.0)
        self.assertEqual(res_switch_delivery["grand_total"], 48510.0)
        self.assertEqual([r["rate"] for r in res_switch_delivery["item_taxes"]], [12500.0, 17600.0, 14000.0])

    def test_preview_order_totals_prices_and_preserves_156_modifiers_and_combo_upcharges(self):
        """#153 x #156 interop: preview_order_totals must resolve authoritative modifier &
        Combo Upcharge rates via validate_and_normalize_order_items (never falling back to
        standalone Item Price or tampered client rate), preserve modifier/combo metadata on
        the in-memory POS Invoice, keep #152 overridden modifier rows intact on draft edits,
        and skip Combo Upcharge rows in _reprice_items_from_authoritative_price_list."""
        import json as _json

        frappe_mock, utils = _make_frappe_env()
        frappe_mock.session = SimpleNamespace(user="cashier_a@example.com")
        frappe_mock.get_roles = MagicMock(return_value=["URY Cashier"])
        frappe_mock.has_permission = MagicMock(return_value=True)
        frappe_mock.db.get_value.side_effect = lambda dt, flt=None, field=None, **kw: (
            "Rest A"
            if dt == "URY Restaurant"
            else (
                "Main Branch A"
                if dt == "POS Profile" and field == "branch"
                else (
                    1
                    if dt == "Currency" and field == "fraction_units"
                    else ("Main Menu" if dt == "URY Menu" else None)
                )
            )
        )
        frappe_mock.get_all.return_value = []

        price_map = {
            "RIBEYE": 35000.0,
            "EXTRA-CHEESE": 3000.0,
            "BURGER-COMBO": 25000.0,
            # Standalone menu price for SWEET-POTATO is 8,000, while Combo Upcharge is 2,000!
            "SWEET-POTATO": 8000.0,
        }

        def _fake_price_valid_on(item_code, price_list, posting_date, *_args, **_kwargs):
            if item_code in price_map:
                return Row(price_list_rate=price_map[item_code])
            return None

        def _fake_validate_and_normalize(
            items,
            past_item,
            menu,
            price_list,
            branch,
            company,
            *,
            posting_date=None,
            customer=None,
            currency_precision=2,
        ):
            normalized = [dict(r) for r in items]
            overrides = {}
            for r in normalized:
                lkey = r.get("reservation_line_key")
                if r.get("custom_line_role") == "Modifier" and r.get("item") == "EXTRA-CHEESE":
                    r["rate"] = 3000.0
                    overrides[lkey] = {
                        "rate": 3000.0,
                        "price_list_rate": 3000.0,
                        "item_code": "EXTRA-CHEESE",
                        "is_validated_modifier": True,
                    }
                elif r.get("custom_line_role") == "Combo Upcharge" and r.get("item") == "SWEET-POTATO":
                    r["rate"] = 2000.0
                    overrides[lkey] = {
                        "rate": 2000.0,
                        "price_list_rate": 2000.0,
                        "item_code": "SWEET-POTATO",
                        "is_validated_modifier": True,
                    }
            return normalized, overrides

        class _InteropPreviewInvoice(FakeInvoice):
            """Simulates real ERPNext v16 set_missing_values overwriting unprotected Combo Upcharge
            rates with standalone Item Price or applying a 20% PRLE if not shielded."""

            def precision(self, _f=None):
                return 0

            def set_missing_values(self, for_validate=False):
                super().set_missing_values(for_validate=for_validate)
                for idx, r in enumerate(self.get("items") or [], start=1):
                    r.setdefault("name", f"PREVIEW-MOD-{idx}")
                    if r.get("item_code") == "SWEET-POTATO":
                        # Without protection, ERPNext standalone price lookup would clobber 2,000 -> 8,000
                        r["price_list_rate"] = 8000.0
                        r["rate"] = 8000.0
                    elif _cint(r.get("custom_price_overridden")) == 1:
                        # Simulate PRLE attempting to apply -20% on overridden modifier unless shielded
                        plr = float(r.get("price_list_rate") or 0)
                        r["discount_percentage"] = 20.0
                        r["rate"] = round(plr * 0.80, 0)

            def calculate_taxes_and_totals(self):
                prec = self.precision("grand_total")
                net = 0.0
                for r in self.get("items") or []:
                    eff_rate = round(float(r.get("rate") or 0), prec)
                    r["rate"] = eff_rate
                    r["amount"] = round(eff_rate * float(r.get("qty") or 0), prec)
                    r["net_amount"] = r["amount"]
                    net += r["net_amount"]
                self["total"] = round(net, prec)
                self["net_total"] = round(net, prec)
                tax_total = 0.0
                for t in self.get("taxes") or []:
                    t["tax_amount"] = round(net * float(t.get("rate") or 0) / 100.0, prec)
                    tax_total += t["tax_amount"]
                self["total_taxes_and_charges"] = round(tax_total, prec)
                self["grand_total"] = round(net + tax_total, prec)
                self["rounded_total"] = self["grand_total"]
                self["rounding_adjustment"] = 0.0

        profile = Row(
            name="POS Profile A",
            company="Maximus Foods UG",
            currency="UGX",
            branch="Main Branch A",
            restaurant="Rest A",
            selling_price_list="Standard UGX",
            cost_center="Main - MFU",
            warehouse="Stores - MFU",
            applicable_for_users=[Row(user="cashier_a@example.com")],
            customer_groups=[],
        )

        unpriced_json = _json.dumps([{"group": "Doneness", "option_code": "MED-RARE", "label": "Medium Rare"}])
        bundle_json = _json.dumps([
            {
                "group": "Combo Side",
                "slot_item": "FRIES",
                "chosen_item": "SWEET-POTATO",
                "label": "Sweet Potato Fries",
                "qty": 1,
            }
        ])

        orig_mod_draft = Row(
            name="MFU-MOD-001",
            branch="Main Branch A",
            pos_profile="POS Profile A",
            order_type="Dine In",
            customer="Walk In Guest",
            no_of_pax=2,
            selling_price_list="Standard UGX",
            items=[
                Row(
                    name="row-ribeye",
                    reservation_line_key="rk-ribeye",
                    item_code="RIBEYE",
                    item_name="Ribeye Steak",
                    qty=1.0,
                    price_list_rate=35000.0,
                    rate=35000.0,
                    custom_line_role="Standard",
                    custom_is_modifier=0,
                    custom_modifiers_json=unpriced_json,
                ),
                Row(
                    name="row-cheese-ov",
                    reservation_line_key="rk-cheese",
                    item_code="EXTRA-CHEESE",
                    item_name="Extra Cheese",
                    qty=1.0,
                    price_list_rate=1500.0,
                    base_price_list_rate=1500.0,
                    rate=1500.0,
                    base_rate=1500.0,
                    custom_price_overridden=1,
                    custom_is_modifier=1,
                    custom_parent_line_key="rk-ribeye",
                    custom_line_role="Modifier",
                    custom_modifier_group="Add-ons",
                    custom_modifier_option="EXTRA-CHEESE",
                    custom_modifier_prefix="Extra",
                ),
                Row(
                    name="row-combo",
                    reservation_line_key="rk-combo",
                    item_code="BURGER-COMBO",
                    item_name="Burger Combo Meal",
                    qty=1.0,
                    price_list_rate=25000.0,
                    rate=25000.0,
                    custom_line_role="Combo Header",
                    custom_is_modifier=0,
                    custom_bundle_overrides_json=bundle_json,
                ),
                Row(
                    name="row-upcharge",
                    reservation_line_key="rk-upcharge",
                    item_code="SWEET-POTATO",
                    item_name="Sweet Potato Upgrade",
                    qty=1.0,
                    price_list_rate=2000.0,
                    base_price_list_rate=2000.0,
                    rate=2000.0,
                    base_rate=2000.0,
                    custom_is_modifier=1,
                    custom_parent_line_key="rk-combo",
                    custom_line_role="Combo Upcharge",
                    custom_modifier_group="Combo Side",
                    custom_modifier_option="SWEET-POTATO",
                ),
            ],
        )

        created_docs = []

        def _new_doc(_dt):
            d = _InteropPreviewInvoice(
                taxes=[
                    Row(
                        charge_type="On Net Total",
                        account_head="VAT 18% Output - MFU",
                        description="VAT 18%",
                        rate=18.0,
                        tax_amount=0.0,
                        custom_pos_charge_kind="",
                    )
                ]
            )
            created_docs.append(d)
            return d

        frappe_mock.new_doc = MagicMock(side_effect=_new_doc)
        frappe_mock.get_doc = MagicMock(
            side_effect=lambda dt, name=None: orig_mod_draft if dt == "POS Invoice" else profile
        )

        modules = {
            "frappe": frappe_mock,
            "frappe.utils": utils,
            "ury.brand": _stub("ury.brand", coded_message=lambda k, m="": f"[POS-{k}] {m}"),
            "ury.ury.api.ury_modifiers": _stub(
                "ury.ury.api.ury_modifiers",
                validate_and_normalize_order_items=MagicMock(side_effect=_fake_validate_and_normalize),
            ),
            "ury.ury_pos.dayparts": _stub(
                "ury.ury_pos.dayparts",
                resolve_menu_and_price_list=MagicMock(
                    return_value={"menu": "Main Menu", "price_list": "Standard UGX", "serving_period": None}
                ),
                stamp_invoice_outlet_time=MagicMock(
                    return_value={"local_dt": datetime(2026, 11, 25, 13, 0, 0), "local_time": "13:00:00"}
                ),
            ),
            "ury.ury_pos.default_customer": _stub(
                "ury.ury_pos.default_customer",
                resolve_default_customer=MagicMock(return_value="Walk In Guest"),
                get_default_customer_config=MagicMock(return_value={}),
            ),
            "ury.ury_pos.tax_determination": _stub(
                "ury.ury_pos.tax_determination",
                apply_order_type_tax=MagicMock(),
                validate_efris_readiness=MagicMock(return_value=[]),
            ),
            "ury.ury_pos.pricing_actions": _stub(
                "ury.ury_pos.pricing_actions",
                sync_service_charge=MagicMock(),
            ),
        }

        cart_payload = [
            {
                "item": "RIBEYE",
                "item_name": "Ribeye Steak",
                "qty": 1,
                "rate": 35000,
                "reservation_line_key": "rk-ribeye",
                "custom_line_role": "Standard",
                "custom_modifiers_json": unpriced_json,
            },
            {
                "item": "EXTRA-CHEESE",
                "item_name": "Extra Cheese",
                "qty": 1,
                "rate": 100,  # tampered client rate; server must use 3,000
                "reservation_line_key": "rk-cheese",
                "custom_is_modifier": 1,
                "custom_parent_line_key": "rk-ribeye",
                "custom_line_role": "Modifier",
                "custom_modifier_group": "Add-ons",
                "custom_modifier_option": "EXTRA-CHEESE",
                "custom_modifier_prefix": "Extra",
            },
            {
                "item": "BURGER-COMBO",
                "item_name": "Burger Combo Meal",
                "qty": 1,
                "rate": 25000,
                "reservation_line_key": "rk-combo",
                "custom_line_role": "Combo Header",
                "custom_bundle_overrides_json": bundle_json,
            },
            {
                "item": "SWEET-POTATO",
                "item_name": "Sweet Potato Upgrade",
                "qty": 1,
                "rate": 500,  # tampered client rate; server must use Combo Upcharge 2,000 (NOT standalone 8,000!)
                "reservation_line_key": "rk-upcharge",
                "custom_is_modifier": 1,
                "custom_parent_line_key": "rk-combo",
                "custom_line_role": "Combo Upcharge",
                "custom_modifier_group": "Combo Side",
                "custom_modifier_option": "SWEET-POTATO",
            },
        ]

        with patch.dict(sys.modules, modules):
            api_mod = _load_module("ury.ury_pos.api", URY_PKG / "ury_pos/api.py")
            with (
                patch.object(api_mod, "_authorize_pos_profile_access", MagicMock()),
                patch.object(api_mod, "_authorize_customer_for_pos_profile", MagicMock(return_value=Row(name="Walk In Guest"))),
                patch.object(api_mod, "item_price_release_states", MagicMock(return_value=(None, None))),
                patch.object(api_mod, "price_valid_on", MagicMock(side_effect=_fake_price_valid_on)),
            ):
                # Case A: Unsaved modified + combo cart
                res_unsaved = api_mod.preview_order_totals(
                    pos_profile="POS Profile A",
                    order_type="Dine In",
                    items=cart_payload,
                )
                # Case B: Saved draft with #152 overridden modifier (1,500) + Combo Upcharge (2,000)
                draft_edit_payload = [
                    {**cart_payload[0], "name": "row-ribeye"},
                    {**cart_payload[1], "name": "row-cheese-ov", "rate": 1500},
                    {**cart_payload[2], "name": "row-combo"},
                    {**cart_payload[3], "name": "row-upcharge", "rate": 2000},
                ]
                res_saved = api_mod.preview_order_totals(
                    pos_profile="POS Profile A",
                    order_type="Dine In",
                    invoice_name="MFU-MOD-001",
                    items=draft_edit_payload,
                )

        # Case C: Direct REST _reprice_items_from_authoritative_price_list skips Combo Upcharge rows
        _rest_frappe, rest_modules = self._load_direct_rest_env()
        rest_doc = Row(
            items=[
                Row(item_code="BURGER-COMBO", custom_line_role="Combo Header", rate=1.0, price_list_rate=1.0),
                Row(item_code="SWEET-POTATO", custom_line_role="Combo Upcharge", rate=2000.0, price_list_rate=2000.0),
            ]
        )
        with patch.dict(sys.modules, rest_modules):
            hook_mod = _load_module("ury.ury.hooks.ury_pos_invoice", URY_PKG / "ury/hooks/ury_pos_invoice.py")
            with patch.object(
                hook_mod,
                "_lookup_authoritative_item_price",
                side_effect=lambda code, _pl, uom=None: price_map.get(code),
            ):
                hook_mod._reprice_items_from_authoritative_price_list(rest_doc, "Standard UGX", force_all=True)

        # Case A assertions: 35,000 + 3,000 + 25,000 + 2,000 = 65,000 net; 11,700 VAT; 76,700 grand
        self.assertEqual([r["rate"] for r in res_unsaved["item_taxes"]], [35000.0, 3000.0, 25000.0, 2000.0])
        self.assertEqual(res_unsaved["net_total"], 65000.0)
        self.assertEqual(res_unsaved["grand_total"], 76700.0)
        self.assertEqual(created_docs[0]["items"][0].get("custom_modifiers_json"), unpriced_json)
        self.assertEqual(created_docs[0]["items"][1].get("custom_is_modifier"), 1)
        self.assertEqual(created_docs[0]["items"][1].get("custom_parent_line_key"), "rk-ribeye")
        self.assertEqual(created_docs[0]["items"][2].get("custom_bundle_overrides_json"), bundle_json)
        self.assertEqual(created_docs[0]["items"][3].get("custom_line_role"), "Combo Upcharge")

        # Case B assertions: 35,000 + 1,500 (overridden!) + 25,000 + 2,000 = 63,500 net; 11,430 VAT; 74,930 grand
        self.assertEqual([r["rate"] for r in res_saved["item_taxes"]], [35000.0, 1500.0, 25000.0, 2000.0])
        self.assertEqual(res_saved["net_total"], 63500.0)
        self.assertEqual(res_saved["grand_total"], 74930.0)
        self.assertEqual(created_docs[1]["items"][2].get("custom_bundle_overrides_json"), bundle_json)
        self.assertEqual(created_docs[1]["items"][3].get("custom_line_role"), "Combo Upcharge")

        # Case C assertions: standard Combo Header repriced 1 -> 25,000; Combo Upcharge kept at 2,000 (NOT 8,000)
        self.assertEqual(rest_doc["items"][0]["rate"], 25000.0)
        self.assertEqual(rest_doc["items"][1]["rate"], 2000.0)


if __name__ == "__main__":
    unittest.main()
