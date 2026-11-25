"""A merged fixed fee keeps the original line basis of dependent taxes."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from erpnext.controllers.taxes_and_totals import process_item_wise_tax_details

from ury.ury.class_overrides.pos_invoice import CustomPOSInvoice
from ury.ury.doctype.ury_order import ury_order
from ury.ury_pos import pricing_actions
from ury.ury_pos.split_balances import allocate_split_balances, set_split_balance


class TestFixedFeeAllocation(unittest.TestCase):
    def setUp(self):
        frappe.db.savepoint("r10_fixed_fee")
        self.addCleanup(lambda: frappe.db.rollback(save_point="r10_fixed_fee"))

    def check(self, company, currency, fee, rate, basis, persist_details):
        key = f"{currency}-{fee}"
        doc = CustomPOSInvoice({
            "doctype": "POS Invoice", "name": key, "company": company, "currency": currency,
            "conversion_rate": 1, "apply_discount_on": "Net Total", "disable_rounded_total": 1,
            "ignore_pricing_rule": 1,
            "items": [{"name": key + "-line", "item_code": "Soup", "qty": 1, "price_list_rate": 100,
                       "rate": 100, "conversion_factor": 1}],
            "taxes": [{"name": key + "-fee", "charge_type": "Actual", "account_head": "Fixed fee",
                       "tax_amount": fee},
                      {"name": key + "-tax", "charge_type": basis, "account_head": "VAT Payable",
                       "row_id": 1, "rate": rate}],
        })
        doc.set_paid_amount = doc.set_outstanding_amount = lambda: None
        doc.calculate_taxes_and_totals()
        if persist_details:
            process_item_wise_tax_details(doc)
        return doc

    def exercise_merge(self, basis, persist_details=True):
        for currency, places in (("UGX", 0), ("USD", 2), ("KWD", 3)):
            with self.subTest(currency=currency):
                company = "R10 " + currency
                frappe.get_doc({"doctype": "Company", "name": company, "company_name": company,
                                "abbr": currency, "default_currency": currency, "country": "Uganda"}).db_insert()
                target, source = (self.check(company, currency, fee, rate, basis, persist_details)
                                  for fee, rate in ((10, 10), (30, 20)))
                saved, _ = allocate_split_balances(target, {}, places, places)
                set_split_balance(target, saved, [target.items[0].name])
                target.save = lambda: target.items[-1].set_new_name()
                source.discard = lambda: None
                source.db_set = source.set
                with patch.multiple(ury_order, _prepare_consolidated_discount=lambda *args: None,
                                    _validate_and_collect_source_tenders=lambda *args: ([], None),
                                    get_branch_stock_policy=lambda **kw: SimpleNamespace(reservation_control_enabled=False)):
                    ury_order.consolidate_checks(target, [source])
                target.save = target.calculate_taxes_and_totals
                on_amount = basis == "On Previous Row Amount"
                original = (247, 47) if on_amount else (277, 77)
                self.assertEqual((target.grand_total, target.total_taxes_and_charges), original)
                for discount, expected in ((10, (227, 47) if on_amount else (254, 74)), (0, original)):
                    with ury_order._split_discount_pricing(target, None):
                        target.additional_discount_percentage = discount
                        target.discount_amount = 0
                    target.calculate_taxes_and_totals()
                    self.assertEqual((target.grand_total, target.total_taxes_and_charges), expected)
                    self.assertCountEqual(
                        [(d.item.custom_origin_invoice_item or d.item.name, d.amount)
                         for d in target._item_wise_tax_details if d.tax.account_head == "Fixed fee" and d.amount],
                        [(currency + "-10-line", 10), (currency + "-30-line", 30)],
                    )
                line = target.items[0]
                with patch.multiple(pricing_actions, _load_invoice_and_profile=lambda *a, **kw: (target, frappe._dict(name="POS")),
                                    authorize_or_perform=lambda **kw: dict(approver="Manager", reason="Guest",
                                                                           reason_note="", via_pin=True),
                                    sync_service_charge=lambda *args: None):
                    pricing_actions.apply_complimentary(target.name, scope="item", item_row_name=line.name)
                self.assertEqual((target.grand_total, target.total_taxes_and_charges),
                                 (147, 47) if on_amount else (167, 67))
                with ury_order._split_discount_pricing(target, None):
                    line.discount_percentage = line.custom_is_complimentary = 0
                    line.rate = line.custom_original_price_list_rate
                target.calculate_taxes_and_totals()
                self.assertEqual((target.grand_total, target.total_taxes_and_charges), original)

    def test_tax_on_fixed_amount(self):
        self.exercise_merge("On Previous Row Amount")

    def test_tax_on_total_including_fixed_amount(self):
        self.exercise_merge("On Previous Row Total")

    def test_original_check_without_persisted_item_breakup(self):
        self.exercise_merge("On Previous Row Amount", persist_details=False)
