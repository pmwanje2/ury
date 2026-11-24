"""Add Check and till tender configuration; site-less (frappe double, no bench).

merge_bills is Simphony's Add/Transfer Check: it validates the selected open checks (same branch,
same order type, drafts the caller may write) and consolidates their lines onto the primary check
(ury_order.consolidate_checks). Nothing links two invoices, so a check can absorb any number of
others (G12).

getPosProfile gives the till its tender configuration instead of literals (G17, G21): the default
tender is the POS Profile payment row marked Default, and the room-charge tenders are the payment
methods whose company account is the Company's Guest Ledger Clearing account, the same rule the
hotel bridge enforces when it posts a room charge.
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]  # ury/


class Row(dict):
    __getattr__ = dict.get


def _stub(name, **attrs):
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class Refused(Exception):
    pass


class NotPermitted(Exception):
    pass


class Loaded(unittest.TestCase):
    def setUp(self):
        framework = MagicMock()
        framework.PermissionError = NotPermitted
        framework.ValidationError = Refused
        framework._ = lambda message, *args: message
        framework.whitelist = lambda *args, **kwargs: (lambda function: function)
        framework.session = SimpleNamespace(user="cashier@example.com")

        def throw(message, exception=None, **kwargs):
            raise (exception or Refused)(message)

        framework.throw = throw
        framework.get_traceback = lambda *args, **kwargs: "traceback"
        self.frappe = framework
        utils = _stub("frappe.utils", cint=lambda value: int(value or 0), flt=lambda value: float(value or 0),
                      nestedset=MagicMock(), validate_phone_number=MagicMock())
        framework.utils = utils
        self.consolidate = MagicMock()
        self.enterContext(patch.dict(sys.modules, {
            "frappe": framework, "frappe.utils": utils,
            "ury.ury_pos.default_customer": _stub("ury.ury_pos.default_customer",
                                                  get_default_customer_config=lambda profile: {}),
            "ury.ury.api.item_price": _stub("ury.ury.api.item_price", item_price_release_states=MagicMock(),
                                            price_valid_on=MagicMock()),
            "ury.ury.doctype.ury_order.ury_order": _stub("ury.ury.doctype.ury_order.ury_order",
                                                         consolidate_checks=self.consolidate),
        }))
        spec = importlib.util.spec_from_file_location("ury.ury_pos.api", ROOT / "ury_pos/api.py")
        self.api = importlib.util.module_from_spec(spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury_pos.api": self.api}))
        spec.loader.exec_module(self.api)


def open_check(name, **fields):
    check = SimpleNamespace(name=name, branch="Lakeside", order_type="Dine In", docstatus=0,
                            custom_merged_pos_invoice=None, items=[Row(item_code="soda", qty=1)])
    check.__dict__.update(fields)
    return check


class TestAddCheck(Loaded):
    def setUp(self):
        super().setUp()
        self.checks = {name: open_check(name) for name in ("CHK-1", "CHK-2", "CHK-3")}
        self.frappe.get_doc.side_effect = lambda doctype, name, *a, **k: self.checks[name]
        self.frappe.has_permission.return_value = True

    def merge(self, secondary):
        return self.api.merge_bills("CHK-1", secondary)

    def test_the_selected_check_is_consolidated_onto_the_primary(self):
        result = self.merge("CHK-2")
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["name"], "CHK-1")
        self.consolidate.assert_called_once_with(self.checks["CHK-1"], [self.checks["CHK-2"]])
        self.frappe.db.commit.assert_called_once()

    def test_no_merge_link_is_written(self):
        self.merge("CHK-2")
        links = [c for c in self.frappe.db.set_value.call_args_list if "custom_merged_pos_invoice" in str(c)]
        self.assertEqual(links, [])

    def test_several_checks_are_added_in_one_step(self):
        result = self.merge(json.dumps(["CHK-2", "CHK-3"]))
        self.assertEqual(result["status"], "success", result)
        self.consolidate.assert_called_once_with(self.checks["CHK-1"], [self.checks["CHK-2"], self.checks["CHK-3"]])

    def test_a_check_of_another_order_type_is_refused(self):
        self.checks["CHK-2"].order_type = "Take Away"
        result = self.merge("CHK-2")
        self.assertEqual(result["status"], "error")
        self.assertIn("order type", result["message"])
        self.consolidate.assert_not_called()
        self.frappe.db.rollback.assert_called()

    def test_the_primary_cannot_be_added_to_itself_among_others(self):
        result = self.merge(json.dumps(["CHK-2", "CHK-1"]))
        self.assertEqual(result["status"], "error")
        self.consolidate.assert_not_called()

    def test_a_check_of_another_branch_is_refused(self):
        self.checks["CHK-3"].branch = "Hilltop"
        with self.assertRaises(NotPermitted):
            self.merge(json.dumps(["CHK-2", "CHK-3"]))
        self.consolidate.assert_not_called()


class Profile(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


class TestTillTenderConfiguration(Loaded):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(self.api, "getBranch", return_value="Lakeside"))
        self.profile = Profile(
            name="Lakeside Till", branch="Lakeside", warehouse="Stores", company="Lakeside Hotel",
            currency="UGX",
            table_attention_time=10, print_format="Receipt", paid_limit=50, custom_enable_discount=1,
            custom_enable_multiple_cashier=0, custom_edit_order_type=0, custom_enable_kot_reprint=0,
            custom_invoice_warning_time=0, qz_print=0, qz_host=None, printer_settings=[],
            applicable_for_users=[Row(user="cashier@example.com")],
            payments=[Row(mode_of_payment="Cash", default=0), Row(mode_of_payment="Mobile Money", default=1),
                      Row(mode_of_payment="Guest Folio", default=0)],
        )
        self.frappe.db.exists.return_value = "Lakeside Till"
        self.frappe.get_doc.side_effect = lambda doctype, name, *a, **k: self.profile
        self.frappe.get_single.return_value = SimpleNamespace(disable_rounded_total=0)
        self.accounts = {"Cash": "Cash - LH", "Mobile Money": "MoMo - LH", "Guest Folio": "Guest Ledger Clearing - LH"}
        self.clearing = "Guest Ledger Clearing - LH"
        self.company_currency = "USD"
        self.has_clearing_field = True
        self.frappe.get_meta.side_effect = lambda doctype: SimpleNamespace(
            has_field=lambda field: doctype == "Company" and field == "custom_guest_ledger_clearing"
            and self.has_clearing_field)
        self.frappe.db.get_value.side_effect = self.value

    def value(self, doctype, filters=None, fieldname=None, *args, **kwargs):
        if doctype == "Mode of Payment Account":
            assert filters["company"] == "Lakeside Hotel", filters
            return self.accounts.get(filters["parent"])
        if doctype == "Company" and fieldname == "custom_guest_ledger_clearing":
            assert self.has_clearing_field, "read a Company field this site does not have"
            return self.clearing
        if doctype == "Company" and fieldname == "country":
            return "Uganda"
        if doctype == "Company" and fieldname == "default_currency":
            return self.company_currency
        return None

    def test_the_default_tender_is_the_profiles_default_payment_row(self):
        self.assertEqual(self.api.getPosProfile()["default_payment_mode"], "Mobile Money")

    def test_get_pos_profile_exposes_profile_currency_and_falls_back_to_company_default(self):
        self.assertEqual(self.api.getPosProfile()["currency"], "UGX")
        self.profile.currency = None
        self.assertEqual(self.api.getPosProfile()["currency"], "USD")

    def test_room_charge_tenders_are_the_methods_posting_to_guest_ledger_clearing(self):
        self.assertEqual(self.api.getPosProfile()["room_charge_modes"], ["Guest Folio"])

    def test_a_site_without_guest_ledger_clearing_has_no_room_charge_tender(self):
        self.has_clearing_field = False
        self.assertEqual(self.api.getPosProfile()["room_charge_modes"], [])

    def test_an_unset_clearing_account_never_matches_a_method_without_an_account(self):
        self.clearing = None
        self.accounts["Cash"] = None
        self.assertEqual(self.api.getPosProfile()["room_charge_modes"], [])

    def test_an_unset_clearing_account_reports_room_charge_unconfigured(self):
        self.clearing = None
        profile = self.api.getPosProfile()
        self.assertEqual(profile["room_charge_modes"], [])
        self.assertTrue(profile["room_charge_unconfigured"])

    def test_a_room_lookup_profile_without_clearing_field_reports_room_charge_unconfigured(self):
        self.has_clearing_field = False
        self.profile.custom_room_lookup_method = "pms_bridge.room_charge_lookup"
        profile = self.api.getPosProfile()
        self.assertEqual(profile["room_charge_modes"], [])
        self.assertTrue(profile["room_charge_unconfigured"])

    def test_a_standalone_site_without_clearing_field_or_lookup_does_not_flag_room_charge_unconfigured(self):
        self.has_clearing_field = False
        profile = self.api.getPosProfile()
        self.assertEqual(profile["room_charge_modes"], [])
        self.assertFalse(profile["room_charge_unconfigured"])

    def test_a_mapped_clearing_account_reports_room_charge_configured(self):
        profile = self.api.getPosProfile()
        self.assertEqual(profile["room_charge_modes"], ["Guest Folio"])
        self.assertFalse(profile["room_charge_unconfigured"])


class TestGetPosInvoiceItemsPayments(Loaded):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(self.api, "getBranch", return_value="Lakeside"))
        self.frappe.has_permission.return_value = True
        self.check_access = MagicMock()
        self.enterContext(
            patch.dict(
                sys.modules,
                {
                    "ury.ury.doctype.ury_order.ury_order": _stub(
                        "ury.ury.doctype.ury_order.ury_order",
                        consolidate_checks=self.consolidate,
                        check_split_target_access=self.check_access,
                    )
                },
            )
        )
        self.frappe.get_all.return_value = []

    def test_returns_only_explicitly_received_tenders_and_skips_unreceived_draft_rows(self):
        invoice = SimpleNamespace(
            name="LKG-00061",
            branch="Lakeside",
            company="Lakeside Hotel",
            items=[Row(name="i1", item_name="Chicken Curry", qty=1, rate=15.0, amount=15.0, is_disposable=0)],
            taxes=[Row(description="VAT 18%", tax_amount=2.7)],
            payments=[
                Row(mode_of_payment="Cash", amount=0.0, custom_tender_received=0),
                # Legacy non-zero placeholder, unconfirmed self-order Phone row, and failed-print row:
                # all have custom_tender_received=0 and must NOT appear as Already received.
                Row(mode_of_payment="Cash", amount=5.90, custom_tender_received=0),
                Row(mode_of_payment="Online Mobile Money", type="Phone", amount=4.72, custom_tender_received=0),
                Row(mode_of_payment="Mobile Money", amount=5.90),
                # Server-marked received tenders:
                Row(mode_of_payment="Cash", amount=5.31, custom_tender_received=1),
                Row(
                    mode_of_payment="Card",
                    amount=10.0,
                    reference_no="AUTH-991",
                    clearance_date="2026-11-24",
                    custom_tender_received=1,
                ),
            ],
            get=lambda k, default=None: getattr(invoice, k, default),
        )
        self.frappe.get_doc.return_value = invoice

        result = self.api.getPosInvoiceItems("LKG-00061")
        self.assertEqual(len(result), 3)
        items, taxes, payments = result
        self.assertEqual(len(items), 1)
        self.assertEqual(len(taxes), 1)
        self.assertEqual(
            payments,
            [
                {"mode_of_payment": "Cash", "amount": 5.31},
                {
                    "mode_of_payment": "Card",
                    "amount": 10.0,
                    "reference_no": "AUTH-991",
                    "clearance_date": "2026-11-24",
                },
            ],
        )

    def test_returns_confirmed_paid_phone_payment_request_but_never_unconfirmed_phone_row(self):
        invoice = SimpleNamespace(
            name="LKG-00122",
            branch="Lakeside",
            company="Lakeside Hotel",
            items=[Row(name="i1", item_name="Iced Tea", qty=1, rate=5.0, amount=5.0, is_disposable=0)],
            taxes=[],
            payments=[
                Row(mode_of_payment="Online Mobile Money", type="Phone", amount=4.72, custom_tender_received=0),
            ],
            get=lambda k, default=None: getattr(invoice, k, default),
        )
        self.frappe.get_doc.return_value = invoice

        # No Paid Payment Request -> empty paymentDetails (no false "Already received").
        self.frappe.get_all.return_value = []
        _, _, payments = self.api.getPosInvoiceItems("LKG-00122")
        self.assertEqual(payments, [])

        # Paid Phone Payment Request -> surfaced as received with its Payment Request reference.
        self.frappe.get_all.return_value = [
            Row(
                name="PR-00122",
                mode_of_payment="Online Mobile Money",
                payment_account="MoMo - LH",
                grand_total=4.72,
                outstanding_amount=0.0,
                status="Paid",
            )
        ]
        _, _, payments = self.api.getPosInvoiceItems("LKG-00122")
        self.assertEqual(
            payments,
            [{"mode_of_payment": "Online Mobile Money", "amount": 4.72, "reference_no": "PR-00122"}],
        )

    def test_rechecks_live_phone_payment_request_status_when_row_has_stale_received_flag(self):
        """P1 #1: a Phone row with stale custom_tender_received=1 whose Payment Request is
        no longer Paid/Partially Paid must NOT be returned as Already received."""
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None, *a, **k: (
            "Phone" if dt == "Mode of Payment" and name == "Online Mobile Money" and field == "type" else None
        )
        invoice = SimpleNamespace(
            name="LKG-00089",
            branch="Lakeside",
            company="Lakeside Hotel",
            items=[Row(name="i1", item_name="Iced Tea", qty=1, rate=5.0, amount=5.0, is_disposable=0)],
            taxes=[],
            payments=[
                Row(
                    mode_of_payment="Online Mobile Money",
                    type="Phone",
                    amount=5.90,
                    reference_no="ACC-PRQ-2026-00052",
                    custom_tender_received=1,
                ),
            ],
            get=lambda k, default=None: getattr(invoice, k, default),
        )
        self.frappe.get_doc.return_value = invoice

        # PR was cancelled or failed -> no live Paid/Partially Paid PR -> must return [].
        self.frappe.get_all.return_value = []
        _, _, payments = self.api.getPosInvoiceItems("LKG-00089")
        self.assertEqual(payments, [])

        # PR is still live Paid -> must return the received Phone tender.
        self.frappe.get_all.return_value = [
            Row(
                name="ACC-PRQ-2026-00052",
                mode_of_payment="Online Mobile Money",
                payment_account="MoMo - LH",
                grand_total=5.90,
                outstanding_amount=0.0,
                status="Paid",
            )
        ]
        _, _, payments = self.api.getPosInvoiceItems("LKG-00089")
        self.assertEqual(
            payments,
            [{"mode_of_payment": "Online Mobile Money", "amount": 5.90, "reference_no": "ACC-PRQ-2026-00052"}],
        )

    def test_invoice_list_endpoints_select_currency_for_multi_currency_orders(self):
        """P1 UI: getPosInvoice, searchPosInvoice, and get_split_group must expose currency."""
        import inspect

        for fn_name in ("getPosInvoice", "searchPosInvoice", "get_split_group"):
            src = inspect.getsource(getattr(self.api, fn_name))
            self.assertIn("currency", src, f"{fn_name} must select POS Invoice.currency")

    def test_partially_refunded_phone_request_returns_live_paid_amount_not_stale_row_amount(self):
        """R6 P1 #2: getPosInvoiceItems must report live paid amount (grand_total - outstanding_amount)
        for Partially Paid and Partially Refunded Phone Payment Requests, and omit fully refunded ones."""
        self.frappe.db.get_value.side_effect = lambda dt, name, field=None, *a, **k: (
            "Phone" if dt == "Mode of Payment" and name == "Online Mobile Money" and field == "type" else None
        )
        invoice = SimpleNamespace(
            name="LKG-00075",
            branch="Lakeside",
            company="Lakeside Hotel",
            items=[Row(name="i1", item_name="Iced Tea", qty=1, rate=5.0, amount=5.0, is_disposable=0)],
            taxes=[],
            payments=[
                Row(
                    mode_of_payment="Online Mobile Money",
                    type="Phone",
                    amount=5.90,
                    reference_no="ACC-PRQ-PART",
                    custom_tender_received=1,
                ),
            ],
            get=lambda k, default=None: getattr(invoice, k, default),
        )
        self.frappe.get_doc.return_value = invoice

        for pr_status in ("Partially Paid", "Partially Refunded"):
            with self.subTest(pr_status=pr_status):
                live_pr = Row(
                    name="ACC-PRQ-PART",
                    mode_of_payment="Online Mobile Money",
                    payment_account="MoMo - LH",
                    grand_total=5.90,
                    outstanding_amount=2.00,
                    status=pr_status,
                )

                def filtered_get_all(doctype, filters=None, **kw):
                    f = filters or {}
                    allowed = set(f["status"][1]) if isinstance(f.get("status"), list) else None
                    if allowed is not None and live_pr["status"] not in allowed:
                        return []
                    return [live_pr]

                self.frappe.get_all.side_effect = filtered_get_all
                _, _, payments = self.api.getPosInvoiceItems("LKG-00075")
                self.assertEqual(
                    payments,
                    [
                        {
                            "mode_of_payment": "Online Mobile Money",
                            "amount": 3.90,
                            "reference_no": "ACC-PRQ-PART",
                        }
                    ],
                )

        # Fully refunded (outstanding == grand_total -> live paid 0.0) must return [].
        zero_pr = Row(
            name="ACC-PRQ-PART",
            mode_of_payment="Online Mobile Money",
            payment_account="MoMo - LH",
            grand_total=5.90,
            outstanding_amount=5.90,
            status="Partially Refunded",
        )
        self.frappe.get_all.side_effect = lambda doctype, filters=None, **kw: [zero_pr]
        _, _, payments = self.api.getPosInvoiceItems("LKG-00075")
        self.assertEqual(payments, [])


if __name__ == "__main__":
    unittest.main()
