"""Native Add Check repricing for per-quantity and dependent tax rows."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from erpnext.controllers.taxes_and_totals import process_item_wise_tax_details

from ury.ury.class_overrides.pos_invoice import CustomPOSInvoice
from ury.ury.doctype.ury_order import ury_order
from ury.ury_pos import pricing_actions
from ury.ury_pos.split_balances import allocate_split_balances, set_split_balance


class TestMergedChargeScope(unittest.TestCase):
    def setUp(self):
        frappe.db.savepoint("r9_tax_scope")
        self.addCleanup(lambda: frappe.db.rollback(save_point="r9_tax_scope"))

    def merged_check(self, charge, currency, places):
        company = f"R9 {currency}"
        frappe.get_doc({"doctype": "Company", "name": company, "company_name": company,
                        "abbr": currency, "default_currency": currency, "country": "Uganda"}).db_insert()
        checks = []
        for rate in (10, 20):
            key = f"{currency}-{rate}"
            taxes = []
            if charge.startswith("On Previous"):
                taxes.append({"name": key + "-base", "charge_type": "On Net Total",
                              "account_head": "Tax Basis", "rate": 100 if charge == "On Previous Row Amount" else 0})
            taxes.append({"name": key + "-vat", "charge_type": charge, "account_head": "VAT Payable",
                          "rate": rate, "row_id": 1 if taxes else None})
            doc = CustomPOSInvoice({
                "doctype": "POS Invoice", "name": key, "company": company, "currency": currency,
                "conversion_rate": 1, "apply_discount_on": "Net Total", "disable_rounded_total": 1,
                "ignore_pricing_rule": 1, "taxes": taxes,
                "items": [{"name": key + "-line", "item_code": "Soup", "qty": 1,
                           "price_list_rate": 100, "rate": 100, "conversion_factor": 1}],
            })
            doc.set_paid_amount = doc.set_outstanding_amount = lambda: None
            doc.calculate_taxes_and_totals()
            process_item_wise_tax_details(doc)
            checks.append(doc)

        target, source = checks
        saved, _ = allocate_split_balances(target, {}, places, places)
        set_split_balance(target, saved, [target.items[0].name])
        target.save = lambda: target.items[-1].set_new_name()
        source.discard = lambda: None
        source.db_set = source.set
        with patch.multiple(ury_order, _prepare_consolidated_discount=lambda *args: None,
                            _validate_and_collect_source_tenders=lambda *args: ([], None),
                            get_branch_stock_policy=lambda **kwargs: SimpleNamespace(reservation_control_enabled=False)):
            ury_order.consolidate_checks(target, [source])
        target.save = target.calculate_taxes_and_totals
        return target

    def verify_discounts(self, charge, discounted_total, discounted_tax, original, vat):
        for currency, places in (("UGX", 0), ("USD", 2), ("KWD", 3)):
            with self.subTest(currency=currency):
                doc = self.merged_check(charge, currency, places)
                self.assertEqual(doc.grand_total, original)
                for percent, total, tax, amounts in (
                    (10, discounted_total, discounted_tax, vat),
                    (0, original, original - 200, [10, 20]),
                ):
                    with ury_order._split_discount_pricing(doc, None):
                        doc.additional_discount_percentage = percent
                        doc.discount_amount = 0
                        doc.calculate_taxes_and_totals()
                        self.assertEqual((doc.grand_total, doc.total_taxes_and_charges), (total, tax))
                    doc.calculate_taxes_and_totals()
                    self.assertEqual(doc.grand_total, total)
                    self.assertCountEqual(
                        [(d.item.custom_origin_invoice_item or d.item.name, d.rate, d.amount)
                         for d in doc._item_wise_tax_details if d.tax.account_head == "VAT Payable" and d.amount],
                        [(f"{currency}-{rate}-line", rate, amount)
                         for rate, amount in zip((10, 20), amounts, strict=True)],
                    )

    def test_previous_row_total_after_zero_charge(self):
        self.verify_discounts("On Previous Row Total", 207, 27, 230, [9, 18])

    def test_per_item_quantity(self):
        self.verify_discounts("On Item Quantity", 210, 30, 230, [10, 20])

    def test_previous_row_amount_after_percentage_charge(self):
        self.verify_discounts("On Previous Row Amount", 387, 207, 430, [9, 18])

    def test_authorized_item_comp_and_discount_removal(self):
        doc = self.merged_check("On Previous Row Total", "USD", 2)
        line = doc.items[0]
        with patch.multiple(pricing_actions, assert_not_offline_replay=lambda: None,
                            _load_invoice_and_profile=lambda *a, **kw: (doc, frappe._dict(name="POS")),
                            authorize_or_perform=lambda **kw: dict(approver="Manager", reason="Guest",
                                                                   reason_note="", via_pin=True),
                            sync_service_charge=lambda *args: None):
            result = pricing_actions.apply_complimentary(doc.name, scope="item", item_row_name=line.name)
        self.assertEqual((result["grand_total"], doc.total_taxes_and_charges), (120, 20))
        with ury_order._split_discount_pricing(doc, None):
            line.discount_percentage = 0
            line.rate = line.custom_original_price_list_rate
            line.custom_is_complimentary = 0
        doc.calculate_taxes_and_totals()
        self.assertEqual((doc.grand_total, doc.total_taxes_and_charges), (230, 30))
