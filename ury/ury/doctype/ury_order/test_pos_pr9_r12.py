"""Native Add Check price, rounding, comp and kitchen scheduling contracts."""

import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import frappe
from erpnext.controllers.taxes_and_totals import process_item_wise_tax_details

from ury.ury.doctype.ury_order import test_pos_pr9_r11 as r11
from ury.ury_pos import pricing_actions
from ury.ury_pos.discount import with_currency_precision
from ury.ury_pos.split_balances import allocate_split_balances


class TestAddCheckRoundTrip(unittest.TestCase):
    currencies = r11.TestAddCheckProvenance.currencies

    def setUp(self):
        self.fixture = r11.TestAddCheckProvenance()
        frappe.db.savepoint("r12_merge")
        self.addCleanup(lambda: frappe.db.rollback(save_point="r12_merge"))
        self.enterContext(patch("frappe.model.meta.get_currency_precision", return_value=2))

    def priced_checks(self, currency, places, prices=(100, 100), *, fees=None, vat=False, siblings=False):
        checks = self.fixture.checks(currency)
        for index, (doc, price) in enumerate(zip(checks, prices, strict=True)):
            item = doc.items[0]
            item.rate = item.price_list_rate = float(price)
            if not siblings:
                item.custom_origin_invoice_item = None
            if fees or vat:
                doc.append("taxes", dict(
                    name=doc.name + "-charge", account_head="Same account",
                    charge_type="Actual" if fees else "On Net Total",
                    tax_amount=fees[index] if fees else 0, rate=0 if fees else 20,
                ))
            with with_currency_precision(doc):
                doc.calculate_taxes_and_totals()
            process_item_wise_tax_details(doc)
            self.fixture.snapshot(doc, places)
        return checks

    def test_offsetting_signed_fees_survive_discount_and_resplit(self):
        for currency, places in self.currencies:
            with self.subTest(currency=currency):
                target, source = self.priced_checks(currency, places, fees=(20, -20))
                self.assertEqual((target.grand_total, source.grand_total), (120, 80))
                self.fixture.merge(target, source)
                self.assertEqual(self.fixture.discount(target, 10), (180, 0))
                self.assertEqual(self.fixture.discount(target, 0), (200, 0))
                parts = allocate_split_balances(target, {target.items[1].name: 1}, places, places)
                for prefix in ("", "base_"):
                    self.assertEqual([part["header"][prefix + "grand_total"] for part in parts], [120, 80])
                    self.assertEqual([part["header"][prefix + "total_taxes_and_charges"] for part in parts],
                                     [20, -20])

    def test_three_decimal_unit_price_on_two_decimal_site(self):
        target, source = self.priced_checks("KWD", 3, (100.125, 100.125), siblings=True)
        self.assertEqual(target.items[0].precision("rate"), 2)
        self.fixture.merge(target, source)
        self.assertEqual(target.grand_total, 200.250)
        self.assertEqual((target.items[0].rate, target.items[0].price_list_rate), (100.125, 100.125))
        for discount, expected in ((10, 180.225), (0, 200.250)):
            self.assertEqual(self.fixture.discount(target, discount)[0], expected)
            self.assertEqual((target.items[0].rate, target.items[0].price_list_rate), (100.125, 100.125))

    def room_charge_after_merge(self, comp_destination):
        for currency, places in self.currencies:
            with self.subTest(currency=currency):
                target, source = self.priced_checks(currency, places, siblings=True)
                comped = target if comp_destination else source
                self.fixture.comp(comped, comped.items[0])
                self.assertTrue(comped.custom_is_complimentary)
                self.fixture.merge(target, source)
                target.db_insert()
                for row in target.items:
                    row.db_insert()
                reloaded = frappe.get_doc("POS Invoice", target.name)
                self.assertEqual(reloaded.grand_total, 100)
                reloaded.room_number = "Room 12"
                reloaded.flags.ury_privileged_action_verified = True
                pricing_actions.validate_privileged_pricing_mutations(reloaded)
                self.assertFalse(reloaded.custom_is_complimentary)
                free = next(row for row in reloaded.items if row.custom_is_complimentary)
                parts = allocate_split_balances(reloaded, {free.name: 1}, places, places)
                self.assertEqual([part["header"]["grand_total"] for part in parts], [100, 0])

    def test_room_charge_after_billable_joins_comped(self):
        self.room_charge_after_merge(True)

    def test_room_charge_after_comped_joins_billable(self):
        self.room_charge_after_merge(False)

    def test_all_comped_stays_ineligible_for_room_charge(self):
        target, source = self.priced_checks("USD", 2, siblings=True)
        for doc in (target, source):
            self.fixture.comp(doc, doc.items[0])
        self.fixture.merge(target, source)
        self.assertEqual(target.grand_total, 0)
        target.room_number = "Room 12"
        with self.assertRaisesRegex(frappe.ValidationError, "COMP-ROOM-CHARGE-FORBIDDEN"):
            pricing_actions.validate_privileged_pricing_mutations(target)

    def held_siblings(self, left, right):
        fields = ("ury_held", "fire_at", "fire_confirmation_pending")
        for currency, places in self.currencies:
            with self.subTest(currency=currency):
                target, source = self.priced_checks(currency, places, siblings=True)
                for doc, state in ((target, left), (source, right)):
                    doc.items[0].update(dict(zip(fields, state, strict=True)))
                self.fixture.merge(target, source)
                self.assertCountEqual(
                    [(row.qty, *(row.get(field) for field in fields)) for row in target.items],
                    [(1, *left), (1, *right)],
                )

    def test_different_fire_times(self):
        self.held_siblings((1, datetime(2026, 11, 25, 19), 0), (1, datetime(2026, 11, 25, 19, 30), 0))

    def test_different_hold_state(self):
        self.held_siblings((1, None, 0), (0, None, 0))

    def test_different_fire_confirmation(self):
        self.held_siblings((1, None, 0), (1, None, 1))

    def test_vat_rounds_per_original_check(self):
        for currency, places in self.currencies:
            with self.subTest(currency=currency):
                unit = Decimal(1).scaleb(-places)
                prices = (100 + unit, 100 + 2 * unit)
                target, source = self.priced_checks(currency, places, prices, vat=True)
                expected = float(sum(prices) + 40)
                self.assertEqual([doc.total_taxes_and_charges for doc in (target, source)], [20, 20])
                self.fixture.merge(target, source)
                self.assertEqual((target.grand_total, target.total_taxes_and_charges), (expected, 40))
                self.fixture.discount(target, 10)
                self.assertEqual(self.fixture.discount(target, 0), (expected, 40))
                parts = allocate_split_balances(target, {target.items[1].name: 1}, places, places)
                self.assertEqual([p["header"]["grand_total"] for p in parts],
                                 [float(price + 20) for price in prices])
