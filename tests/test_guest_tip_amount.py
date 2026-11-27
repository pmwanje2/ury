import unittest
from decimal import Decimal

from ury.ury.api.guest_tip import guest_tip_amount


class GuestTipAmountTests(unittest.TestCase):
    def test_minor_unit_rounding(self):
        for precision, expected in [(0, "2"), (2, "1.56"), (3, "1.555")]:
            self.assertEqual(guest_tip_amount("1.555", 10, precision), Decimal(expected))

    def test_invalid_or_excessive_amount(self):
        for value in ("NaN", "Infinity", "-0.01", "10.001", {}, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                guest_tip_amount(value, 10, 2)
