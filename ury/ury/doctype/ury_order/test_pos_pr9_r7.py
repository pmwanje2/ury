"""Native regressions for split tax rules, waitlist overlap, and outlet fixtures."""

import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import frappe
from frappe.translate import get_translations_from_csv

from ury.ury.api import table_reservation
from ury.ury.class_overrides.pos_invoice import CustomPOSInvoice
from ury.ury.doctype.ury_order import ury_order
from ury.ury_pos.split_balances import allocate_split_balances, set_split_balance

OVERLAP_MESSAGE = (
    "Table {0} already has a reservation during the selected time. Please select a different reservation time."
)


def _insert(doctype, name, **fields):
    doc = frappe.get_doc({"doctype": doctype, "name": name, **fields})
    doc.set_user_and_timestamp()
    doc.db_insert()
    for child in doc.get_all_children():
        child.db_insert()
    return doc


class TestPOSPR9R7(unittest.TestCase):
    def setUp(self):
        savepoint = "pos_pr9_r7"
        frappe.db.savepoint(savepoint)
        self.addCleanup(lambda: frappe.db.rollback(save_point=savepoint))

    def test_split_discounts_reprice_percentage_tax_like_native_check(self):
        company = _insert("Company", "R7 Company", company_name="R7 Company", abbr="R7",
                          default_currency="USD", country="United States")
        vat = {"name": "R7 VAT Row", "account_head": "R7 VAT", "charge_type": "On Net Total", "rate": 10}
        template = _insert("Sales Taxes and Charges Template", "R7 Tax Template",
                           title="R7 Tax Template", company=company.name, taxes=[vat])

        def invoice(name, basis, lines, tax_template=None):
            doc = CustomPOSInvoice({
                "doctype": "POS Invoice", "name": name, "company": company.name, "currency": "USD",
                "conversion_rate": 1, "apply_discount_on": basis, "disable_rounded_total": 1,
                "ignore_pricing_rule": 1, "taxes_and_charges": tax_template,
                "items": [
                    {"name": line, "item_code": "Soup", "qty": 1, "rate": 100, "price_list_rate": 100,
                     "conversion_factor": 0.5}
                    for line in lines
                ],
                "taxes": [dict(vat)],
            })
            doc.set_paid_amount = lambda: None
            doc.set_outstanding_amount = lambda: None
            doc.calculate_taxes_and_totals()
            return doc

        for basis in ("Net Total", "Grand Total"):
            for legacy in (False, True):
                with self.subTest(basis=basis, legacy_template=legacy):
                    control = invoice("R7 Control", basis, ["r7-control"])
                    split = invoice("R7 Split", basis, ["r7-keep", "r7-move"], template.name if legacy else None)
                    remain, _ = allocate_split_balances(split, {"r7-move": 1}, 2, 2)
                    split.remove(split.items[1])
                    remain["items"]["r7-keep"]["stock_qty"] = 0.499
                    if legacy:
                        keep = {"account_head", "tax_amount", "base_tax_amount", "tax_amount_after_discount_amount",
                                "base_tax_amount_after_discount_amount", "net_amount", "base_net_amount"}
                        remain["taxes"] = [{key: value for key, value in row.items() if key in keep}
                                           for row in remain["taxes"]]
                    set_split_balance(split, remain, ["r7-keep"])
                    split.calculate_taxes_and_totals()

                    for percent in (10, 0):
                        control.additional_discount_percentage = percent
                        control.discount_amount = 0
                        control.calculate_taxes_and_totals()
                        with ury_order._split_discount_pricing(split, None):
                            split.additional_discount_percentage = percent
                            split.discount_amount = 0
                            split.calculate_taxes_and_totals()
                            self.assertEqual((split.grand_total, split.total_taxes_and_charges),
                                             (control.grand_total, control.total_taxes_and_charges))
                            if basis == "Net Total" and percent:
                                self.assertEqual((split.grand_total, split.total_taxes_and_charges), (99, 9))
                        split.calculate_taxes_and_totals()
                        self.assertEqual(split.grand_total, control.grand_total)
                        self.assertEqual(split.items[0].stock_qty, 0.499)

    def test_chained_taxes_share_fixed_transaction_amount_before_fx(self):
        for charge_type, rate, tax_amount, expected in (
            ("On Previous Row Amount", 100, 0.02, 0.01),
            ("On Previous Row Total", 100, 0.06, 0.03),
        ):
            with self.subTest(charge_type=charge_type):
                items = [frappe._dict(name=name, qty=1, net_amount=0.02, base_net_amount=0.01)
                         for name in ("a", "b")]
                check = frappe._dict(
                    items=items, currency="USD", conversion_rate=0.5,
                    taxes=[
                        frappe._dict(name="fixed", charge_type="Actual", tax_amount_after_discount_amount=0.02),
                        frappe._dict(name="chain", charge_type=charge_type, row_id=1,
                                     tax_amount_after_discount_amount=tax_amount),
                    ],
                    item_wise_tax_details=[
                        frappe._dict(item_row="a", tax_row="fixed", amount=0.01, rate=0),
                        frappe._dict(item_row="b", tax_row="fixed", amount=0, rate=0),
                        frappe._dict(item_row="a", tax_row="chain", amount=0.01 if expected == 0.01 else 0.02, rate=rate),
                        frappe._dict(item_row="b", tax_row="chain", amount=0 if expected == 0.01 else 0.01, rate=rate),
                    ],
                )
                parts = allocate_split_balances(check, {"b": 1}, 2, 2)
                self.assertEqual([part["taxes"][1]["tax_amount_after_discount_amount"] for part in parts],
                                 [expected, expected])

    def test_waitlist_upcoming_overlap_has_a_localized_error_code(self):
        now = datetime(2026, 11, 26, 12)
        branch = _insert("Branch", "R7 Branch", branch="R7 Branch", custom_enable_reservation=1,
                         custom_buffer_time=10, custom_target_turn_time=60)
        table = _insert("URY Table", "R7 Table", branch=branch.name, occupied=0,
                        no_of_seats=4, custom_table_state="Available")
        _insert("URY Table Reservation", "R7 Booking", branch=branch.name, reserved_table=table.name,
                customer_name="Booked guest", reserved_at=now + timedelta(minutes=35), status="Confirmed")
        party = _insert("URY Table Reservation", "R7 Walk-In", branch=branch.name,
                        customer_name="Walk-in guest", reserved_at=now, status="Waiting", is_waitlist=1, no_of_pax=2)
        with (
            patch.object(table_reservation, "now_datetime", return_value=now),
            patch.object(table_reservation, "check_table_reservation",
                         return_value=frappe._dict(is_lock_window_active=False)),
            self.assertRaisesRegex(frappe.ValidationError, "RESERVATION-TABLE-OVERLAP"),
        ):
            table_reservation.seat_waitlist_party(party.name, table.name)
        self.assertEqual(frappe.db.get_value("URY Table Reservation", party.name, "status"), "Waiting")
        for locale in ("fr", "es", "ar"):
            self.assertIn("{0}", get_translations_from_csv(locale, "ury")[OVERLAP_MESSAGE])

    def test_legacy_split_proration_and_approver_fixtures_reach_their_assertions(self):
        prefix = "ury.ury.doctype.ury_order.test_ury_order_discount"
        cases = [
            f"{prefix}.TestSplitMergeAndOptions.test_split_bill_preserves_line_discount_and_prorates_flat_header_discount",
            f"{prefix}.TestHydrogenR1Defects.test_p0_7_split_bill_by_approving_owner_and_meta_only_approver_pass_siteops_guards",
        ]
        code = """
import sys
import unittest
import frappe
frappe.init(sys.argv[1], sites_path=sys.argv[2])
frappe.connect()
frappe.set_user("Administrator")
try:
    tests = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[3:])
    result = unittest.TextTestRunner().run(tests)
finally:
    frappe.destroy()
raise SystemExit(not result.wasSuccessful())
"""
        result = subprocess.run(
            [sys.executable, "-c", code, frappe.local.site, frappe.local.sites_path, *cases],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
