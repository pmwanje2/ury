"""Discounting a consolidated check retains each original item's VAT rate."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from erpnext.controllers.taxes_and_totals import process_item_wise_tax_details

from ury.ury.class_overrides.pos_invoice import CustomPOSInvoice
from ury.ury.doctype.ury_order import ury_order
from ury.ury_pos.split_balances import allocate_split_balances, set_split_balance


class TestPOSPR9R8(unittest.TestCase):
    def setUp(self):
        frappe.db.savepoint("mixed_vat_discount")
        self.addCleanup(lambda: frappe.db.rollback(save_point="mixed_vat_discount"))

    def check(self, company, currency, rate):
        origin = f"{currency}-line-{rate}"
        doc = CustomPOSInvoice({
            "doctype": "POS Invoice", "name": f"{currency}-check-{rate}", "company": company,
            "currency": currency, "conversion_rate": 1, "apply_discount_on": "Net Total",
            "disable_rounded_total": 1, "ignore_pricing_rule": 1,
            "items": [{"name": origin, "item_code": "Soup", "qty": 1, "price_list_rate": 100,
                       "rate": 100, "conversion_factor": 1}],
            "taxes": [{"name": f"{origin}-vat", "charge_type": "On Net Total",
                       "account_head": "VAT Payable", "rate": rate}],
        })
        doc.set_paid_amount = doc.set_outstanding_amount = lambda: None
        doc.calculate_taxes_and_totals()
        process_item_wise_tax_details(doc)
        return doc

    def test_add_check_then_discount_preserves_each_lines_vat(self):
        for currency, places in (("UGX", 0), ("USD", 2), ("KWD", 3)):
            with self.subTest(currency=currency):
                company = f"R8 {currency}"
                frappe.get_doc({
                    "doctype": "Company", "name": company, "company_name": company, "abbr": currency,
                    "default_currency": currency, "country": "Uganda",
                }).db_insert()
                merged = self.check(company, currency, 10)
                source = self.check(company, currency, 20)
                origins = [merged.items[0].name, source.items[0].name]
                saved, _ = allocate_split_balances(merged, {}, places, places)
                set_split_balance(merged, saved, [origins[0]])
                merged.save = lambda: merged.items[-1].set_new_name()
                source.discard = lambda: None
                source.db_set = source.set
                with patch.multiple(
                    ury_order, _prepare_consolidated_discount=lambda *args: None,
                    _validate_and_collect_source_tenders=lambda *args: ([], None),
                    get_branch_stock_policy=lambda **kwargs: SimpleNamespace(reservation_control_enabled=False),
                ):
                    ury_order.consolidate_checks(merged, [source])
                self.assertEqual((merged.grand_total, merged.total_taxes_and_charges), (230, 30))

                for percent, total, taxes in ((10, 207, [9, 18]), (0, 230, [10, 20])):
                    with ury_order._split_discount_pricing(merged, None):
                        merged.additional_discount_percentage = percent
                        merged.discount_amount = 0
                        merged.calculate_taxes_and_totals()
                        self.assertEqual((merged.grand_total, merged.total_taxes_and_charges),
                                         (total, sum(taxes)))
                    merged.calculate_taxes_and_totals()
                    self.assertEqual([row.tax_amount_after_discount_amount for row in merged.taxes], taxes)
                    self.assertEqual(merged.grand_total, total)
                    self.assertCountEqual(
                        [(d.item.custom_origin_invoice_item or d.item.name, d.rate, d.amount)
                         for d in merged._item_wise_tax_details if d.amount],
                        list(zip(origins, [10, 20], taxes, strict=True)),
                    )
