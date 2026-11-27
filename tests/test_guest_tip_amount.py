import unittest
from decimal import Decimal

from ury.ury.api.guest_tip import guest_tip_amount


class GuestTipAmountTests(unittest.TestCase):
    def test_minor_unit_rounding(self):
        for precision, expected in [(0, "2"), (2, "1.56"), (3, "1.555")]:
            self.assertEqual(guest_tip_amount("1.555", 10, precision), Decimal(expected))

    def test_sub_minor_excess_rounds_to_the_cap(self):
        self.assertEqual(guest_tip_amount("10.001", 10, 2), Decimal("10.00"))

    def test_guest_tip_follows_assigned_waiter_or_unassigned_outlet_bucket(self):
        from ury.ury.api.guest_tip import self_order_tip_server
        for waiter, expected in [(None, "Guest"), ("Administrator", "Guest"), ("waiter@example.com", "waiter@example.com")]:
            self.assertEqual(self_order_tip_server("Administrator", waiter), expected)
        self.assertEqual(self_order_tip_server("captain@example.com", None), "captain@example.com")

    def test_rounded_amount_cannot_exceed_fractional_subtotal(self):
        for precision, subtotal in [(0, "1.5"), (2, "1.555"), (3, "1.5555")]:
            with self.subTest(precision=precision), self.assertRaises(ValueError):
                guest_tip_amount(subtotal, subtotal, precision)

    def test_invalid_or_excessive_amount(self):
        for value in ("NaN", "Infinity", "-0.01", "10.006", {}, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                guest_tip_amount(value, 10, 2)
