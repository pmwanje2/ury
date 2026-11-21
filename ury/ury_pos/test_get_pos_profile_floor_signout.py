"""Site-less getPosProfile contracts.

Ported from the Maximus fork: only the payment-mode account map (#72,
2c925938 and ac63706d). The floor sign-out settings in the fork file belong to
theme T4, which adds its tests to this module.
"""

import unittest
from unittest.mock import patch

import frappe

from ury.ury_pos import api


class TestFloorSignoutProfile(unittest.TestCase):
    def setUp(self):
        self.framework = self.enterContext(patch.object(api, "frappe"))
        self.enterContext(patch.object(api, "getBranch", return_value="Branch A"))
        self.framework.session.user = "waiter@example.com"
        self.framework.db.exists.return_value = "Branch A POS"
        self.framework.get_single.return_value = frappe._dict(disable_rounded_total=0)
        self.profile = frappe._dict(
            name="Branch A POS", branch="Branch A", warehouse="Stores", company="Test Co",
            table_attention_time=10, print_format="Receipt", paid_limit=0,
            custom_enable_discount=0, custom_enable_multiple_cashier=0,
            custom_edit_order_type=0, custom_enable_kot_reprint=0, qz_print=0,
            applicable_for_users=[frappe._dict(user="cashier@example.com")],
            printer_settings=[],
        )
        self.framework.get_doc.return_value = self.profile

    def test_profile_without_payment_rows_returns_empty_account_map(self):
        # A site-less double (and a profile read without child rows) has no
        # payments list; the till's mode-to-account map must then be empty
        # instead of raising.
        result = api.getPosProfile()
        self.assertEqual(result.get("payment_mode_accounts"), {})

    def test_payment_mode_accounts_use_the_profile_company(self):
        self.profile.payments = [frappe._dict(mode_of_payment="Cash"), frappe._dict(mode_of_payment="Card")]
        accounts = {"Cash": "Cash - TC", "Card": "Card Clearing - TC"}
        self.framework.db.get_value.side_effect = lambda doctype, filters, field: (
            accounts[filters["parent"]]
            if doctype == "Mode of Payment Account" and filters.get("company") == "Test Co"
            and field == "default_account" else None
        )
        self.assertEqual(api.getPosProfile().get("payment_mode_accounts"), accounts)


if __name__ == "__main__":
    unittest.main()
