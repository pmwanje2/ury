"""Site-less regressions for Item master names on POS and QR orders."""

from unittest import TestCase

from ury.ury.api.test_ury_kot_notes import OrderingCases, Row


class TestInvoiceItemNames(OrderingCases, TestCase):
    def get_value(self, doctype, filters, field, **kwargs):
        if (doctype, filters, field) == ("Item", "CF0001", "item_name"):
            return "Americano"
        return super().get_value(doctype, filters, field, **kwargs)

    def price(self, **fields):
        return self.order.price_items_for_invoice(
            [{"item": "CF0001", "qty": 1, **fields}],
            "Standard Selling", "POS A", "Branch A", "Menu A",
        )[0]

    def customer_line(self, item_code, item_name):
        self.invoice.items = [Row(
            item_code=item_code, item_name=item_name, qty=1, rate=100, amount=100,
        )]
        return self.guest._sanitize_invoice_for_customer(self.invoice)["items"][0]

    def test_pricing_resolves_an_omitted_item_name(self):
        row = self.price()
        self.assertEqual((row["item_code"], row["item_name"]), ("CF0001", "Americano"))

    def test_pricing_resolves_empty_and_sku_item_names(self):
        for name in ("", "CF0001"):
            with self.subTest(name=name):
                self.assertEqual(self.price(item_name=name)["item_name"], "Americano")

    def test_pricing_preserves_a_non_code_item_name(self):
        self.assertEqual(self.price(item_name="Iced Americano")["item_name"], "Iced Americano")

    def test_summary_resolves_missing_empty_and_sku_item_names(self):
        for name in (None, "", "CF0001"):
            with self.subTest(name=name):
                row = self.customer_line("CF0001", name)
                self.assertEqual((row["item_code"], row["item_name"]), ("CF0001", "Americano"))
                self.assertEqual(self.invoice.items[0].item_name, name)

    def test_summary_preserves_a_non_code_item_name(self):
        row = self.customer_line("CF0001", "Iced Americano")
        self.assertEqual(row["item_name"], "Iced Americano")

    def test_summary_falls_back_to_code_without_a_master_name(self):
        self.assertEqual(self.customer_line("UNKNOWN", None)["item_name"], "UNKNOWN")
