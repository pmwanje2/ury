"""Exercise the native change validator at both write boundaries on a 2dp site."""

from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.pos_invoice.pos_invoice import POSInvoice

from ury.ury.doctype.ury_order.test_merged_bill_integrity import Check, SettlementFixture


class TestSettlementChange(SettlementFixture):
    def test_cash_change_survives_save_and_submit(self):
        self.enterContext(patch.object(frappe, "get_system_settings", return_value="Commercial Rounding"))
        self.enterContext(patch("ury.ury_pos.till_controls.lock_open_shift_for_settlement"))
        self.enterContext(patch.object(self.order, "_tax_mod"))
        api = self._stub_modules["ury.ury_pos.api"]
        self.enterContext(patch.object(api, "_record_settlement_fiscal_intent", create=True))
        for currency, due, tender, change in (
            ("KWD", 0.997, 1, 0.003), ("BHD", 0.997, 1, 0.003), ("OMR", 0.997, 1, 0.003),
            ("USD", 0.99, 1, 0.01), ("EUR", 0.99, 1, 0.01), ("UGX", 999, 1000, 1), ("JPY", 999, 1000, 1),
        ):
            with self.subTest(currency=currency):
                self.profile.currency = currency
                check = Check("Check A", grand_total=due, rounded_total=due, conversion_rate=1,
                              write_off_amount=0, account_for_change_amount="Local cash")
                check.precision = lambda field, parentfield=None: 2
                check.set_paid_amount = lambda: setattr(check, "paid_amount", sum(row.amount for row in check.payments))
                writes = []

                def validate(stage):
                    POSInvoice.validate_change_amount(check)
                    writes.append((stage, check.change_amount, check.base_change_amount))

                check.save = lambda: validate("save")
                check.submit = lambda: validate("submit")
                result = self._settle(check, [{"mode_of_payment": "Cash", "amount": tender}], submit_invoice=1)
                self.assertEqual(result["status"], "Submitted")
                self.assertEqual(writes, [("save", change, change), ("submit", change, change)])
