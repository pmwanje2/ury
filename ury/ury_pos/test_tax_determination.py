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


if __name__ == "__main__":
    unittest.main()
