"""Run on a configured ERPNext/MariaDB test site via bench run-tests --module
ury.ury.doctype.ury_order.test_workstation_change_integration.
"""

import unittest

import frappe
from frappe.core.doctype.system_settings.system_settings import clear_system_settings_cache
from frappe.utils import get_currency_precision, getdate, now_datetime, random_string

from ury.ury.doctype.ury_order.ury_order import make_invoice
from ury.ury_pos.discount import with_currency_precision


def insert(doctype, **fields):
    return frappe.get_doc({"doctype": doctype, **fields}).insert()


class TestWorkstationChangePersistence(unittest.TestCase):
    def setUp(self):
        self.addCleanup(frappe.clear_cache)
        self.addCleanup(frappe.db.rollback)
        frappe.set_user("Administrator")
        self.assertIn("MariaDB", frappe.db.sql("select version()")[0][0])
        frappe.db.set_single_value("System Settings", "currency_precision", "2")
        # Evict the settings cached before this transaction changed the site precision.
        clear_system_settings_cache()
        frappe.local.system_settings = None
        self.assertEqual(int(frappe.get_system_settings("currency_precision")), 2)
        frappe.db.set_single_value("Global Defaults", "disable_rounded_total", 1)
        frappe.db.set_single_value("POS Settings", "invoice_type", "POS Invoice")

    def open_till(self, currency, rates, company_currency=None):
        tag = f"Change {currency} {random_string(6)}"
        units = {"UGX": 1, "USD": 100}.get(currency, 1000)
        frappe.db.set_value("Currency", currency, {"enabled": 1, "fraction_units": units,
                                                  "smallest_currency_fraction_value": 1 / units})
        company = insert("Company", company_name=tag, abbr=random_string(5),
                         default_currency=company_currency or currency, country="Kuwait")
        self.company = company
        year = getdate().year
        if not frappe.db.exists("Fiscal Year", {"year_start_date": ("<=", getdate()),
                                                 "year_end_date": (">=", getdate()), "disabled": 0}):
            insert("Fiscal Year", year=tag, year_start_date=f"{year}-01-01", year_end_date=f"{year}-12-31")
        self.branch = insert("Branch", branch=tag, company=company.name,
                             user=[{"user": "Administrator"}]).name
        menu_rows = []
        for rate in rates:
            item = insert("Item", item_code=f"{tag} {rate}", stock_uom="Nos", is_stock_item=0,
                          item_group=frappe.db.get_value("Item Group", {"is_group": 0}))
            menu_rows.append({"item": item.name, "rate": rate})
        frappe.defaults.set_user_default("currency", currency)
        frappe.defaults.set_user_default("company", company.name)
        frappe.local.new_doc_templates.pop("Price List", None)
        menu = insert("URY Menu", name=tag, branch=self.branch, items=menu_rows)
        price_list = frappe.get_doc("Price List", menu.price_list)
        self.assertEqual(price_list.currency, currency)
        self.prices = {
            price.price_list_rate: price.item_code
            for price in frappe.get_all("Item Price", filters={"price_list": price_list.name},
                                        fields=["item_code", "price_list_rate"])
        }
        room = insert("URY Room", name=tag, branch=self.branch)
        self.restaurant = insert("URY Restaurant", name=tag, company=company.name, branch=self.branch,
                                 active_menu=menu.name, default_room=room.name, invoice_series_prefix="CHG").name
        self.customer = insert("Customer", customer_name=tag, customer_type="Individual",
                               customer_group=frappe.db.get_value("Customer Group", {"is_group": 0}),
                               territory=frappe.db.get_value("Territory", {"is_group": 0})).name
        self.cash = insert("Mode of Payment", mode_of_payment=tag, type="Cash",
                           accounts=[{"company": company.name, "default_account": company.default_cash_account}]).name
        self.profile = insert(
            "POS Profile", name=tag, company=company.name, currency=currency, branch=self.branch,
            restaurant=self.restaurant, warehouse=frappe.db.get_value("Warehouse", {"company": company.name, "is_group": 0}),
            selling_price_list=price_list.name, write_off_account=company.default_expense_account,
            write_off_cost_center=company.cost_center, cost_center=company.cost_center,
            account_for_change_amount=company.default_cash_account, disable_rounded_total=1,
            income_account=company.default_income_account, expense_account=company.default_expense_account,
            applicable_for_users=[{"user": "Administrator", "default": 1}],
            payments=[{"mode_of_payment": self.cash, "default": 1}],
        )
        insert("POS Opening Entry", company=company.name, pos_profile=self.profile.name, user="Administrator",
               period_start_date=now_datetime(), posting_date=getdate(), branch=self.branch, restaurant=self.restaurant,
               balance_details=[{"mode_of_payment": self.cash, "opening_amount": 0}]).submit()

    def draft(self, rate, conversion_rate=1, qty=1):
        return insert(
            "POS Invoice", company=self.company.name, pos_profile=self.profile.name, restaurant=self.restaurant,
            branch=self.branch, customer=self.customer, currency=self.profile.currency, conversion_rate=conversion_rate, is_pos=1,
            cashier="Administrator", waiter="Administrator", invoice_printed=1, order_type="Take Away",
            selling_price_list=self.profile.selling_price_list, disable_rounded_total=1,
            account_for_change_amount=self.company.default_cash_account,
            items=[{"item_code": self.prices[rate], "qty": qty, "rate": rate, "price_list_rate": rate}],
            payments=[{"mode_of_payment": self.cash, "amount": 0}],
        ).name

    def check_settlement(self, currency, linked):
        # Each tuple is the priced item, allocated cash and change for one persisted invoice.
        expected = [(0.551, 0.554, 0.003), (0.446, 0.446, 0)] if linked else [(0.997, 1, 0.003)]
        self.open_till(currency, [rate for rate, _, _ in expected])
        names = [self.draft(rate) for rate, _, _ in expected]
        if linked:
            for name, partner in zip(names, reversed(names)):
                frappe.db.set_value("POS Invoice", name, "custom_merged_pos_invoice", partner)

        for stage, status in [(0, "Draft"), (1, "Submitted")]:
            result = make_invoice(
                customer=self.customer, payments=[{"mode_of_payment": self.cash, "amount": 1}],
                cashier="Administrator", pos_profile=self.profile.name, owner="Administrator",
                invoice=names[0], submit_invoice=stage,
            )
            self.assertEqual(result, {"name": names[0], "status": status})
            for name, (rate, cash, change) in zip(names, expected):
                with self.subTest(currency=currency, stage=status, invoice=name):
                    doc = frappe.get_doc("POS Invoice", name)
                    self.assertEqual(
                        (doc.docstatus, doc.currency, [row.rate for row in doc.items], doc.grand_total,
                         [(row.mode_of_payment, row.amount) for row in doc.payments], doc.change_amount),
                        (stage, currency, [rate], rate, [(self.cash, cash)], change),
                    )

    def test_kwd_single(self):
        self.check_settlement("KWD", False)

    def test_bhd_single(self):
        self.check_settlement("BHD", False)

    def test_omr_single(self):
        self.check_settlement("OMR", False)

    def test_kwd_linked(self):
        self.check_settlement("KWD", True)

    def test_bhd_linked(self):
        self.check_settlement("BHD", True)

    def test_omr_linked(self):
        self.check_settlement("OMR", True)

    def test_ugx_check_posts_usd_company_totals(self):
        self.open_till("UGX", [1000], company_currency="USD")
        insert("Currency Exchange", date=getdate(), from_currency="UGX", to_currency="USD",
               exchange_rate=0.00027, for_selling=1)
        name = self.draft(1000, conversion_rate=0.00027)
        for stage in (0, 1):
            if stage:
                make_invoice(customer=self.customer, payments=[{"mode_of_payment": self.cash, "amount": 1000}],
                             cashier="Administrator", pos_profile=self.profile.name, owner="Administrator",
                             invoice=name, submit_invoice=1)
            with self.subTest(docstatus=stage):
                doc = frappe.get_doc("POS Invoice", name)
                self.assertEqual((doc.docstatus, doc.currency, doc.conversion_rate), (stage, "UGX", 0.00027))
                self.assertEqual((doc.grand_total, doc.base_grand_total), (1000, 0.27))
                self.assertEqual((doc.items[0].rate, doc.items[0].base_rate), (1000, 0.27))
                if stage:
                    self.assertEqual((doc.paid_amount, doc.base_paid_amount), (1000, 0.27))

    def test_usd_check_keeps_three_decimal_company_fields(self):
        self.open_till("USD", [1], company_currency="KWD")
        insert("Currency Exchange", date=getdate(), from_currency="USD", to_currency="KWD",
               exchange_rate=0.3074, for_selling=1)
        name = self.draft(1, conversion_rate=0.3074)
        doc = frappe.get_doc("POS Invoice", name)
        with with_currency_precision(doc):
            self.assertEqual(doc.precision("grand_total"), 2)
            self.assertEqual(doc.precision("loyalty_amount"), 3)
            self.assertEqual(doc.items[0].precision("incoming_rate"), 3)
            self.assertEqual(doc.precision("incoming_rate", "items"), 3)
        self.assertEqual((doc.grand_total, doc.base_grand_total), (1, 0.307))

    def test_usd_unit_rate_uses_native_three_decimal_site_precision(self):
        settings = frappe.get_doc("System Settings")
        settings.language = settings.language or "en"
        settings.time_zone = settings.time_zone or "UTC"
        settings.currency_precision = "3"
        settings.save()
        frappe.local.system_settings = None
        self.assertEqual(get_currency_precision(), 3)
        self.open_till("USD", [0.125])
        name = self.draft(0.125, qty=1000)

        for stage in (0, 1):
            if stage:
                make_invoice(customer=self.customer, payments=[{"mode_of_payment": self.cash, "amount": 125}],
                             cashier="Administrator", pos_profile=self.profile.name, owner="Administrator",
                             invoice=name, submit_invoice=1)
            doc = frappe.get_doc("POS Invoice", name)
            self.assertEqual((doc.docstatus, doc.grand_total, doc.base_grand_total), (stage, 125, 125))
            self.assertEqual((doc.items[0].rate, doc.items[0].base_rate), (0.125, 0.125))
