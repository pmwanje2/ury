"""Exercise the native change validator at both write boundaries on a 2dp site."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from erpnext.accounts.doctype.pos_invoice.pos_invoice import POSInvoice

from erpnext.accounts.doctype.pos_invoice_item.pos_invoice_item import POSInvoiceItem
from erpnext.controllers.taxes_and_totals import calculate_taxes_and_totals
from frappe.model.document import Document

from ury.ury.doctype.ury_order.test_merged_bill_integrity import Check, SettlementFixture


class PricedCheck(Check):
    """One priced POS Invoice Item with no taxes and whole-check rounding disabled."""
    doctype = "POS Invoice"
    round_floats_in = Document.round_floats_in

    def __init__(self, name, currency, price):
        item = POSInvoiceItem(dict(
            doctype="POS Invoice Item", parentfield="items", item_code="Meal", qty=1,
            price_list_rate=price, rate=price, discount_percentage=0, discount_amount=0,
            margin_type=None, pricing_rules=None,
        ))
        super().__init__(name, [item], currency=currency, conversion_rate=1,
                         write_off_amount=0, account_for_change_amount="Local cash")
        item.precision = self.precision
        self.meta = SimpleNamespace(get_field=lambda name: name == "rounded_total")
        self.writes = []

    def precision(self, field, parentfield=None):
        return 2

    def is_rounded_total_disabled(self):
        return True

    def calculate_taxes_and_totals(self):
        # Run ERPNext's item, net and grand-total calculations on the same mutable
        # row at every write. Leave only the unrelated database/tax lookups out.
        totals = calculate_taxes_and_totals.__new__(calculate_taxes_and_totals)
        totals.doc = self
        totals._items = self.items
        totals.discount_amount_applied = False
        totals.grand_total_diff = 0
        totals.calculate_item_values()
        totals.calculate_net_total()
        totals.calculate_totals()

    def set_paid_amount(self):
        self.paid_amount = sum(row.amount for row in self.payments)

    def validate_write(self, stage):
        self.calculate_taxes_and_totals()
        self.set_paid_amount()
        POSInvoice.validate_change_amount(self)
        self.writes.append((stage, self.change_amount, self.base_change_amount))

    def save(self, **kwargs):
        self.validate_write("save")

    def submit(self):
        self.validate_write("submit")


class TestSettlementChange(SettlementFixture):
    def setUp(self):
        super().setUp()
        # Supply field metadata locally. Frappe's actual round_floats_in and flt
        # use the 2dp site precision unless the settlement context is active.
        item_meta = Mock(_table_doctypes={}, _non_computed_table_doctypes={})
        item_meta.get.return_value = [frappe._dict(fieldname=key) for key in ("rate", "price_list_rate", "qty")]
        self.enterContext(patch.object(POSInvoiceItem, "meta", item_meta))
        self.enterContext(patch.object(POSInvoiceItem, "get_valid_columns", return_value=[]))
        self.enterContext(patch.object(frappe, "get_system_settings", return_value="Commercial Rounding"))
        self.enterContext(patch("ury.ury_pos.till_controls.lock_open_shift_for_settlement"))
        self.enterContext(patch.object(self.order, "_tax_mod"))
        api = self._stub_modules["ury.ury_pos.api"]
        self.enterContext(patch.object(api, "_record_settlement_fiscal_intent", create=True))

    def test_cash_change_survives_save_and_submit(self):
        for currency, due, tender, change in (
            ("KWD", 0.997, 1, 0.003), ("BHD", 0.997, 1, 0.003), ("OMR", 0.997, 1, 0.003),
            ("USD", 0.99, 1, 0.01), ("EUR", 0.99, 1, 0.01), ("UGX", 999, 1000, 1), ("JPY", 999, 1000, 1),
        ):
            with self.subTest(currency=currency):
                self.profile.currency = currency
                check = PricedCheck("Check A", currency, due)
                result = self._settle(check, [{"mode_of_payment": "Cash", "amount": tender}], submit_invoice=1)
                self.assertEqual(result["status"], "Submitted")
                self.assertEqual(check.writes, [("save", change, change), ("submit", change, change)])

                self.assertEqual(check.items[0].rate, due)

    def test_linked_checks_recalculate_and_allocate_cash_at_three_decimals(self):
        self.profile.currency = "KWD"
        first = PricedCheck("Check A", "KWD", 0.551)
        second = PricedCheck("Check B", "KWD", 0.446)
        first.custom_merged_pos_invoice = second.name
        self.frappe.get_doc.return_value = second
        self._settle(first, [{"mode_of_payment": "Cash", "amount": 1}], submit_invoice=0)
        self.assertEqual([first.grand_total, second.grand_total], [0.551, 0.446])
        self.assertEqual([first.paid_amount, second.paid_amount], [0.554, 0.446])
        self.assertEqual(first.writes, [("save", 0.003, 0.003)])
        self.assertEqual(second.writes, [("save", 0, 0)])
