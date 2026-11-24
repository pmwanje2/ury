"""End-to-end tests for the `void_sent_item` consumer wired to manager PIN authorisation (B2a Slice 2).

Verifies on a real Frappe + MariaDB 11.8 bench:
- `reduce_order_item_qty` requires `void_sent_item` for sent items, consumes token once,
  refuses tampered claim and replay, allows manager alone, and skips held items.
- `sync_order` reducing/removing a sent item enforces `void_sent_item` via `POS Privilege Rule`,
  accepts a bound `approval_token`, and allows manager alone.
- Direct REST/ORM `POS Invoice.save()` without an approval token (even when unprinted or when
  a caller tries to forge `flags`) is refused at `validate_invoice` with `POS-AUTH-REQUIRED`.
"""

import json
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt, getdate, now_datetime, nowdate, random_string
from frappe.utils.password import update_password


class TestVoidSentItemOverride(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")
        cls.suffix = random_string(8).lower()
        cls.created = []

        base_company = frappe.db.get_value("Company", {}, "name")
        base = frappe.get_doc("Company", base_company)
        cls.company = frappe.get_doc({
            "doctype": "Company",
            "company_name": f"VoidCo-{cls.suffix}",
            "abbr": f"V{cls.suffix[:4].upper()}",
            "default_currency": base.default_currency or "USD",
            "country": base.country or "United States",
        }).insert(ignore_permissions=True).name
        cls.created.append(("Company", cls.company))

        year = getdate(nowdate()).year
        fy_name = f"FY-{year}-{cls.suffix}"
        fy = frappe.get_doc({
            "doctype": "Fiscal Year",
            "year": fy_name,
            "year_start_date": f"{year}-01-01",
            "year_end_date": f"{year}-12-31",
            "companies": [{"company": cls.company}],
        }).insert(ignore_permissions=True)
        cls.created.append(("Fiscal Year", fy.name))

        cls.cashier = cls._make_user(f"void-cashier-{cls.suffix}@example.test", "Void Cashier", ["URY Cashier"])
        cls.manager = cls._make_user(f"void-mgr-{cls.suffix}@example.test", "Void Manager", ["URY Manager"])

        cls.branch = frappe.get_doc({
            "doctype": "Branch",
            "branch": f"VoidBranch-{cls.suffix}",
            "company": cls.company,
            "user": [{"user": cls.cashier}, {"user": cls.manager}],
        }).insert(ignore_permissions=True).name
        cls.created.append(("Branch", cls.branch))

        co = frappe.get_doc("Company", cls.company)
        cls.warehouse = frappe.db.get_value("Warehouse", {"company": cls.company, "is_group": 0}, "name")
        cls.income_account = co.default_income_account or frappe.db.get_value(
            "Account", {"company": cls.company, "root_type": "Income", "is_group": 0}, "name"
        )
        cls.expense_account = co.default_expense_account or frappe.db.get_value(
            "Account", {"company": cls.company, "root_type": "Expense", "is_group": 0}, "name"
        )
        cls.cost_center = co.cost_center or frappe.db.get_value(
            "Cost Center", {"company": cls.company, "is_group": 0}, "name"
        )
        cash_acct = co.default_cash_account or frappe.db.get_value(
            "Account", {"company": cls.company, "account_type": "Cash", "is_group": 0}, "name"
        )

        cls.mop = f"VoidCash-{cls.suffix}"
        frappe.get_doc({
            "doctype": "Mode of Payment",
            "mode_of_payment": cls.mop,
            "type": "Cash",
            "accounts": [{"company": cls.company, "default_account": cash_acct}],
        }).insert(ignore_permissions=True)
        cls.created.append(("Mode of Payment", cls.mop))

        cls.price_list = f"VoidPL-{cls.suffix}"
        frappe.get_doc({
            "doctype": "Price List",
            "price_list_name": cls.price_list,
            "currency": co.default_currency or "USD",
            "selling": 1,
            "enabled": 1,
        }).insert(ignore_permissions=True)
        cls.created.append(("Price List", cls.price_list))

        item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups"
        cls.item_a = cls._make_item(f"VOID-ITEM-A-{cls.suffix}", "Grilled Fish", item_group, 25.0)
        cls.item_b = cls._make_item(f"VOID-ITEM-B-{cls.suffix}", "Fresh Juice", item_group, 10.0)

        cls.customer = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": f"Void Guest {cls.suffix}",
            "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups",
            "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories",
        }).insert(ignore_permissions=True).name
        cls.created.append(("Customer", cls.customer))

        cls.profile = frappe.get_doc({
            "doctype": "POS Profile",
            "name": f"VoidTill-{cls.suffix}",
            "__newname": f"VoidTill-{cls.suffix}",
            "company": cls.company,
            "branch": cls.branch,
            "currency": co.default_currency or "USD",
            "warehouse": cls.warehouse,
            "income_account": cls.income_account,
            "expense_account": cls.expense_account,
            "cost_center": cls.cost_center,
            "write_off_account": cls.expense_account,
            "write_off_cost_center": cls.cost_center,
            "selling_price_list": cls.price_list,
            "payments": [{"mode_of_payment": cls.mop, "default": 1}],
            "applicable_for_users": [{"user": cls.cashier}, {"user": cls.manager}],
            "role_allowed_for_billing": [{"role": "URY Cashier"}, {"role": "URY Manager"}],
            "custom_qty_reduction_allowed_order_types": "Dine In,Take Away",
            "custom_kot_naming_series": "KOT",
            "remove_items": 1,  # Even if legacy flag is 1, POS Privilege Rule governs!
        }).insert(ignore_permissions=True).name
        cls.created.append(("POS Profile", cls.profile))

        poe = frappe.get_doc({
            "doctype": "POS Opening Entry",
            "company": cls.company,
            "branch": cls.branch,
            "pos_profile": cls.profile,
            "user": cls.cashier,
            "period_start_date": now_datetime(),
            "posting_date": nowdate(),
            "balance_details": [{"mode_of_payment": cls.mop, "opening_amount": 0}],
        })
        poe.flags.ignore_mandatory = True
        poe.insert(ignore_permissions=True, ignore_mandatory=True)
        poe.submit()
        cls.poe = poe.name
        cls.created.append(("POS Opening Entry", cls.poe))

        from ury.patches.v2_0 import setup_pos_manager_authorisation
        setup_pos_manager_authorisation.seed_default_rules(company=cls.company)
        setup_pos_manager_authorisation.seed_default_reasons()

        from ury.ury_pos import override
        frappe.set_user(cls.manager)
        override.set_my_pin(f"Pass-{cls.suffix}!", "7412")
        frappe.set_user("Administrator")
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        frappe.set_user("Administrator")
        frappe.db.rollback()
        key = f"pos-override-pin:{cls.manager.casefold()}"
        frappe.cache.hdel("login_failed_count", key)
        frappe.cache.hdel("login_failed_time", key)
        frappe.db.delete("POS Authorisation Log", {"pos_profile": cls.profile})
        frappe.db.delete("POS Privilege Rule", {"company": cls.company})
        frappe.db.delete("POS Invoice", {"company": cls.company})
        frappe.db.delete("POS Opening Entry", {"company": cls.company})
        frappe.db.delete("Mode of Payment Account", {"company": cls.company})
        frappe.db.delete("Fiscal Year Company", {"company": cls.company})
        frappe.db.delete("Item Price", {"price_list": cls.price_list})
        for dt, name in reversed(cls.created):
            if frappe.db.exists(dt, name):
                if dt == "Mode of Payment":
                    frappe.db.delete("Mode of Payment Account", {"parent": name})
                frappe.db.delete(dt, {"name": name})
        frappe.db.commit()
        super().tearDownClass()

    @classmethod
    def _make_user(cls, email, first_name, roles):
        prev = getattr(frappe.flags, "in_import", False)
        frappe.flags.in_import = True
        try:
            doc = frappe.get_doc({
                "doctype": "User",
                "email": email,
                "first_name": first_name,
                "enabled": 1,
                "user_type": "System User",
                "send_welcome_email": 0,
                "roles": [{"role": r} for r in roles],
            }).insert(ignore_permissions=True)
        finally:
            frappe.flags.in_import = prev
        update_password(doc.name, f"Pass-{cls.suffix}!")
        cls.created.append(("User", doc.name))
        return doc.name

    @classmethod
    def _make_item(cls, code, name, group, rate):
        item = frappe.get_doc({
            "doctype": "Item",
            "item_code": code,
            "item_name": name,
            "item_group": group,
            "stock_uom": "Nos",
            "is_stock_item": 0,
        }).insert(ignore_permissions=True)
        cls.created.append(("Item", item.name))
        frappe.get_doc({
            "doctype": "Item Price",
            "item_code": code,
            "price_list": cls.price_list,
            "price_list_rate": rate,
            "selling": 1,
            "valid_from": nowdate(),
        }).insert(ignore_permissions=True)
        return item.name

    def _make_pos_invoice(self, *, invoice_printed=1, waiter=None, items=None):
        frappe.set_user("Administrator")
        rows = items or [
            {
                "item_code": self.item_a,
                "item_name": "Grilled Fish",
                "qty": 2,
                "rate": 25.0,
                "price_list_rate": 25.0,
                "base_price_list_rate": 25.0,
                "uom": "Nos",
                "conversion_factor": 1.0,
                "income_account": self.income_account,
                "expense_account": self.expense_account,
                "cost_center": self.cost_center,
                "warehouse": self.warehouse,
                "reservation_line_key": f"line-a-{random_string(6)}",
                "ury_held": 0,
            },
            {
                "item_code": self.item_b,
                "item_name": "Fresh Juice",
                "qty": 1,
                "rate": 10.0,
                "price_list_rate": 10.0,
                "base_price_list_rate": 10.0,
                "uom": "Nos",
                "conversion_factor": 1.0,
                "income_account": self.income_account,
                "expense_account": self.expense_account,
                "cost_center": self.cost_center,
                "warehouse": self.warehouse,
                "reservation_line_key": f"line-b-{random_string(6)}",
                "ury_held": 0,
            },
        ]
        doc = frappe.get_doc({
            "doctype": "POS Invoice",
            "is_pos": 1,
            "company": self.company,
            "branch": self.branch,
            "pos_profile": self.profile,
            "customer": self.customer,
            "order_type": "Dine In",
            "waiter": waiter or self.cashier,
            "cashier": self.cashier,
            "selling_price_list": self.price_list,
            "invoice_printed": invoice_printed,
            "items": rows,
            "payments": [{"mode_of_payment": self.mop, "amount": 0}],
        })
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        return doc

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()
        key = f"pos-override-pin:{self.manager.casefold()}"
        frappe.cache.hdel("login_failed_count", key)
        frappe.cache.hdel("login_failed_time", key)

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()

    def test_reduce_order_item_qty_requires_void_sent_item_and_consumes_token_once(self):
        from ury.ury.api.ury_pos_invoice_qty_reduction import reduce_order_item_qty
        from ury.ury_pos import override

        inv = self._make_pos_invoice(invoice_printed=1, waiter=self.cashier)
        row_a = inv.items[0]

        frappe.set_user(self.cashier)
        with patch("ury.ury.api.ury_pos_invoice_qty_reduction.process_items_for_cancel_kot", return_value=["CNCL-KOT-1"]) as mock_kot:
            with self.assertRaisesRegex(frappe.PermissionError, "POS-AUTH-REQUIRED"):
                reduce_order_item_qty(inv.name, row_a.name, 1, reason="Guest changed mind")
            mock_kot.assert_not_called()

        inv.reload()
        self.assertEqual(flt(inv.items[0].qty), 2.0)

        claim = {
            "action": "reduce_order_item_qty",
            "item_row_name": row_a.name,
            "item_code": self.item_a,
            "from_qty": 2.0,
            "to_qty": 1.0,
            "amount": 25.0,
            "reason": "Guest changed mind",
        }
        issued = override.authorise(
            self.profile, "void_sent_item", self.manager, "7412", "POS Invoice", inv.name, claim
        )
        token = issued["token"]

        # Tampered target qty must refuse with POS-AUTH-CHANGED.
        with patch("ury.ury.api.ury_pos_invoice_qty_reduction.process_items_for_cancel_kot", return_value=["CNCL-KOT-1"]):
            with self.assertRaisesRegex(frappe.ValidationError, "POS-AUTH-CHANGED"):
                reduce_order_item_qty(inv.name, row_a.name, 0, reason="Guest changed mind", approval_token=token)

        # Matching request succeeds and consumes the token.
        with patch("ury.ury.api.ury_pos_invoice_qty_reduction.process_items_for_cancel_kot", return_value=["CNCL-KOT-1"]) as mock_kot:
            res = reduce_order_item_qty(inv.name, row_a.name, 1, reason="Guest changed mind", approval_token=token)
            mock_kot.assert_called_once()
        self.assertEqual(res["new_qty"], 1.0)
        self.assertEqual(res["approved_by"], self.manager)

        inv.reload()
        self.assertEqual(flt(inv.items[0].qty), 1.0)

        # Replay of the consumed token must refuse with POS-AUTH-EXPIRED.
        with patch("ury.ury.api.ury_pos_invoice_qty_reduction.process_items_for_cancel_kot", return_value=["CNCL-KOT-2"]):
            with self.assertRaisesRegex(frappe.ValidationError, "POS-AUTH-EXPIRED"):
                reduce_order_item_qty(inv.name, row_a.name, 0, reason="Guest changed mind", approval_token=token)

    def test_reduce_order_item_qty_allows_manager_alone_and_held_item_without_approval(self):
        from ury.ury.api.ury_pos_invoice_qty_reduction import reduce_order_item_qty

        inv = self._make_pos_invoice(
            invoice_printed=1,
            waiter=self.cashier,
            items=[
                {
                    "item_code": self.item_a, "item_name": "Grilled Fish", "qty": 2,
                    "rate": 25.0, "price_list_rate": 25.0, "base_price_list_rate": 25.0,
                    "uom": "Nos", "conversion_factor": 1.0,
                    "income_account": self.income_account, "expense_account": self.expense_account,
                    "cost_center": self.cost_center, "warehouse": self.warehouse,
                    "reservation_line_key": f"sent-{random_string(6)}", "ury_held": 0,
                },
                {
                    "item_code": self.item_b, "item_name": "Fresh Juice", "qty": 2,
                    "rate": 10.0, "price_list_rate": 10.0, "base_price_list_rate": 10.0,
                    "uom": "Nos", "conversion_factor": 1.0,
                    "income_account": self.income_account, "expense_account": self.expense_account,
                    "cost_center": self.cost_center, "warehouse": self.warehouse,
                    "reservation_line_key": f"held-{random_string(6)}", "ury_held": 1,
                },
            ],
        )
        sent_row, held_row = inv.items[0], inv.items[1]

        # Cashier can reduce an unsent/held line without manager PIN.
        frappe.set_user(self.cashier)
        res_held = reduce_order_item_qty(inv.name, held_row.name, 1)
        self.assertEqual(res_held["new_qty"], 1.0)

        # Manager with void_sent_item may_perform=1 can reduce the sent line alone.
        frappe.set_user(self.manager)
        with patch("ury.ury.api.ury_pos_invoice_qty_reduction.process_items_for_cancel_kot", return_value=["CNCL-KOT-M"]):
            res_mgr = reduce_order_item_qty(inv.name, sent_row.name, 1, reason="Kitchen error")
        self.assertEqual(res_mgr["new_qty"], 1.0)

    def test_sync_order_reducing_sent_item_enforces_void_sent_item_and_accepts_token(self):
        from ury.ury.doctype.ury_order.ury_order import sync_order
        from ury.ury_pos import override

        inv = self._make_pos_invoice(invoice_printed=0, waiter=self.cashier)
        row_a, row_b = inv.items[0], inv.items[1]
        key_a = row_a.reservation_line_key
        key_b = row_b.reservation_line_key

        reduced_payload = [
            {
                "name": row_a.name,
                "item": self.item_a,
                "item_name": "Grilled Fish",
                "qty": 1,
                "rate": 25.0,
                "comment": "",
                "reservation_line_key": key_a,
            },
            {
                "name": row_b.name,
                "item": self.item_b,
                "item_name": "Fresh Juice",
                "qty": 1,
                "rate": 10.0,
                "comment": "",
                "reservation_line_key": key_b,
            },
        ]

        frappe.set_user(self.cashier)
        with patch("ury.ury.doctype.ury_order.ury_order._require_open_cashier_session"), \
             patch("ury.ury.doctype.ury_order.ury_order._validate_sync_items_against_menu"), \
             patch("ury.ury.doctype.ury_order.ury_order.kot_execute") as mock_kot, \
             patch("ury.ury.doctype.ury_order.ury_order.getBranch", return_value=self.branch):
            with self.assertRaisesRegex(frappe.PermissionError, "POS-AUTH-REQUIRED") as caught:
                sync_order(
                    items=json.dumps(reduced_payload),
                    cashier=self.cashier,
                    owner=self.cashier,
                    mode_of_payment=self.mop,
                    customer=self.customer,
                    no_of_pax=2,
                    last_invoice=inv.name,
                    last_modified_time=str(inv.modified),
                    waiter=self.cashier,
                    pos_profile=self.profile,
                    invoice=inv.name,
                    order_type="Dine In",
                )
            self.assertIn("not permitted to reduce or remove items", str(caught.exception))
            mock_kot.assert_not_called()

        claim = {
            "action": "sync_order_void_sent_item",
            "lines": [
                {
                    "item_code": self.item_a,
                    "reservation_line_key": key_a,
                    "from_qty": 2.0,
                    "to_qty": 1.0,
                    "delta": 1.0,
                }
            ],
            "amount": 25.0,
            "reason": "Wrong item keyed",
        }
        issued = override.authorise(
            self.profile, "void_sent_item", self.manager, "7412", "POS Invoice", inv.name, claim
        )

        with patch("ury.ury.doctype.ury_order.ury_order._require_open_cashier_session"), \
             patch("ury.ury.doctype.ury_order.ury_order._validate_sync_items_against_menu"), \
             patch("ury.ury.doctype.ury_order.ury_order.kot_execute") as mock_kot, \
             patch("ury.ury.doctype.ury_order.ury_order.getBranch", return_value=self.branch):
            updated = sync_order(
                items=json.dumps(reduced_payload),
                cashier=self.cashier,
                owner=self.cashier,
                mode_of_payment=self.mop,
                customer=self.customer,
                no_of_pax=2,
                last_invoice=inv.name,
                last_modified_time=str(inv.modified),
                waiter=self.cashier,
                pos_profile=self.profile,
                invoice=inv.name,
                order_type="Dine In",
                approval_token=issued["token"],
                void_reason="Wrong item keyed",
            )
            mock_kot.assert_called_once()
        self.assertEqual(updated["name"], inv.name)
        inv.reload()
        self.assertEqual(flt(inv.items[0].qty), 1.0)

    def test_direct_rest_save_bypass_without_token_is_refused_by_validate_invoice(self):
        """REST / Desk direct POS Invoice.save() removing/reducing a sent line without approval refuses."""
        inv = self._make_pos_invoice(invoice_printed=0, waiter=self.cashier)

        # Cashier direct save reducing sent item (even on unprinted check + remove_items=1) -> REFUSED.
        frappe.set_user(self.cashier)
        doc = frappe.get_doc("POS Invoice", inv.name)
        doc.items[0].qty = 1
        with self.assertRaisesRegex(frappe.PermissionError, "POS-AUTH-REQUIRED"):
            doc.save()

        # Forging a plain boolean/dict on doc.flags must NOT bypass the verified proof check.
        doc = frappe.get_doc("POS Invoice", inv.name)
        doc.items[0].qty = 1
        doc.flags.pos_void_sent_item_approval = True
        with self.assertRaisesRegex(frappe.PermissionError, "POS-AUTH-REQUIRED"):
            doc.save()

        # Removing a sent row outright via direct save -> REFUSED.
        doc = frappe.get_doc("POS Invoice", inv.name)
        doc.remove(doc.items[0])
        with self.assertRaisesRegex(frappe.PermissionError, "POS-AUTH-REQUIRED"):
            doc.save()

        # Increasing qty via direct save is NOT a void and remains allowed.
        doc = frappe.get_doc("POS Invoice", inv.name)
        doc.items[0].qty = 3
        doc.save()
        inv.reload()
        self.assertEqual(flt(inv.items[0].qty), 3.0)

        # Manager with void_sent_item may_perform=1 can reduce directly.
        frappe.set_user(self.manager)
        doc = frappe.get_doc("POS Invoice", inv.name)
        doc.items[0].qty = 2
        doc.save()
        inv.reload()
        self.assertEqual(flt(inv.items[0].qty), 2.0)
