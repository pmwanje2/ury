import unittest
from unittest.mock import patch

import frappe
from ury.ury.api.self_ordering import _tip_prompt
from ury.ury.doctype.ury_self_ordering_profile.ury_self_ordering_profile import URYSelfOrderingProfile


class GuestTipAccountTests(unittest.TestCase):
    def check_account(self, ledger, *, bootstrap):
        profile = frappe._dict(enable_guest_tips=1, pos_profile="Till", branch="West", guest_tip_presets="10")
        till = frappe._dict(company="Hotel", currency="USD", custom_tips_payable_account="Tips Payable")
        def lookup(doctype, name, fields, **kwargs):
            row = till if doctype == "POS Profile" else ledger if doctype == "Account" else frappe._dict()
            if isinstance(fields, str):
                return row.get(fields) if row else None
            return row
        with patch.object(frappe.db, "get_value", side_effect=lookup):
            if bootstrap:
                return _tip_prompt(profile)
            return URYSelfOrderingProfile.validate(profile)

    def test_prompt_and_configuration_require_a_usable_liability(self):
        valid = dict(company="Hotel", root_type="Liability", disabled=0, is_group=0)
        self.assertIsNotNone(self.check_account(frappe._dict(valid), bootstrap=True))
        self.check_account(frappe._dict(valid), bootstrap=False)
        for invalid in (None, {"company": "Other"}, {"root_type": "Income"}, {"disabled": 1}, {"is_group": 1}):
            ledger = None if invalid is None else frappe._dict(valid | invalid)
            with self.subTest(invalid=invalid, bootstrap=True):
                self.assertIsNone(self.check_account(ledger, bootstrap=True))
            with self.subTest(invalid=invalid, bootstrap=False), self.assertRaises(frappe.ValidationError):
                self.check_account(ledger, bootstrap=False)
