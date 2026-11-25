"""Native Add Check regressions for charge dependencies and comped split siblings."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from erpnext.controllers.taxes_and_totals import process_item_wise_tax_details

from ury.ury.class_overrides.pos_invoice import CustomPOSInvoice
from ury.ury.doctype.ury_order import ury_order
from ury.ury_pos import pricing_actions
from ury.ury_pos.split_balances import allocate_split_balances, set_split_balance


class TestAddCheckProvenance(unittest.TestCase):
    currencies = (("UGX", 0), ("USD", 2), ("KWD", 3))

    def setUp(self):
        frappe.db.savepoint("r11_merge")
        self.addCleanup(lambda: frappe.db.rollback(save_point="r11_merge"))

    def checks(self, currency, *, taxed=False):
        company = f"R11 {currency}"
        frappe.get_doc(dict(doctype="Company", name=company, company_name=company, abbr=currency,
                            default_currency=currency, country="Uganda")).db_insert()
        checks = []
        for label, fee, rate in (("first", 10, 10), ("second", 30, 20)):
            key = f"{currency}-{label}"
            taxes = []
            if taxed:
                if label == "second":
                    taxes.append(dict(charge_type="Actual", account_head="Source fee", tax_amount=20))
                taxes.append(dict(charge_type="Actual", account_head="Shared fee", tax_amount=fee))
                taxes.append(dict(charge_type="On Previous Row Total", account_head="VAT",
                                  rate=rate, row_id=len(taxes)))
            for index, tax in enumerate(taxes):
                tax["name"] = f"{key}-tax-{index}"
            doc = CustomPOSInvoice(dict(
                doctype="POS Invoice", name=key, company=company, currency=currency,
                conversion_rate=1, apply_discount_on="Net Total", disable_rounded_total=1,
                ignore_pricing_rule=1, items=[dict(name=key + "-item", item_code="Soup", qty=1,
                price_list_rate=100, rate=100, conversion_factor=1,
                custom_origin_invoice_item=None if taxed else "shared-soup")], taxes=taxes,
            ))
            doc.set_paid_amount = doc.set_outstanding_amount = lambda: None
            doc.calculate_taxes_and_totals()
            process_item_wise_tax_details(doc)
            checks.append(doc)
        return checks

    def snapshot(self, check, places):
        saved, _ = allocate_split_balances(check, {}, places, places)
        set_split_balance(check, saved, [row.name for row in check.items])

    def merge(self, target, source):
        def save():
            for row in target.items:
                if not row.name:
                    row.set_new_name()
        target.save = save
        source.discard = lambda: None
        source.db_set = source.set
        with patch.multiple(ury_order, _prepare_consolidated_discount=lambda *args: None,
                            _validate_and_collect_source_tenders=lambda *args: ([], None),
                            get_branch_stock_policy=lambda **kw: SimpleNamespace(reservation_control_enabled=False)):
            ury_order.consolidate_checks(target, [source])
        target.save = target.calculate_taxes_and_totals

    def discount(self, check, percentage):
        with ury_order._split_discount_pricing(check, None):
            check.additional_discount_percentage = percentage
            check.discount_amount = 0
        check.calculate_taxes_and_totals()
        return check.grand_total, check.total_taxes_and_charges

    def comp(self, check, row):
        check.save = check.calculate_taxes_and_totals
        with patch.multiple(pricing_actions, _load_invoice_and_profile=lambda *a, **kw: (check, frappe._dict(name="POS")),
                            authorize_or_perform=lambda **kw: dict(approver="Manager", reason="Guest",
                                                                   reason_note="", via_pin=True),
                            sync_service_charge=lambda *args: None):
            pricing_actions.apply_complimentary(check.name, scope="item", item_row_name=row.name)

    def test_cumulative_vat_includes_source_only_preceding_fee(self):
        for currency, places in self.currencies:
            with self.subTest(currency=currency):
                target, source = self.checks(currency, taxed=True)
                self.snapshot(target, places)
                self.merge(target, source)
                self.assertEqual((target.grand_total, target.total_taxes_and_charges), (301, 101))
                self.assertEqual(self.discount(target, 10), (278, 98))
                self.assertEqual(self.discount(target, 0), (301, 101))
                self.comp(target, target.items[0])
                self.assertEqual((target.grand_total, target.total_taxes_and_charges), (191, 91))

    def comped_sibling(self, *, comp_destination):
        for currency, places in self.currencies:
            with self.subTest(currency=currency):
                target, source = self.checks(currency)
                for check in (target, source):
                    self.snapshot(check, places)
                comped = target if comp_destination else source
                self.comp(comped, comped.items[0])
                self.assertEqual(comped.grand_total, 0)
                self.merge(target, source)
                self.assertEqual(target.grand_total, 100)
                self.assertEqual(self.discount(target, 10)[0], 90)
                self.assertEqual(self.discount(target, 0)[0], 100)
                self.assertCountEqual([(row.qty, row.rate, bool(row.custom_is_complimentary),
                                        row.custom_comp_reason or "") for row in target.items],
                                      [(1, 0, True, "Guest"), (1, 100, False, "")])
                self.assertTrue(all(row.custom_origin_invoice_item == "shared-soup" for row in target.items))

    def test_comped_into_billable(self):
        self.comped_sibling(comp_destination=False)

    def test_billable_into_comped(self):
        self.comped_sibling(comp_destination=True)
