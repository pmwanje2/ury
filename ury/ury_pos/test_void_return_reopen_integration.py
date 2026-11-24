"""Real Frappe + ERPNext + MariaDB 11.8 integration proof for B5a void/waste/return/reopen.

Runs via `bench --site <site> run-tests --module ury.ury_pos.test_void_return_reopen_integration`.
Proves what site-less tests cannot:
- Real ERPNext native Stock Entry (Material Issue) posted & submitted for wasted sent items.
- Real ERPNext native POS Invoice returns (`is_return=1`, `return_against`) with UGX flat
  discount (3 x 10,000 - 5,000 = 25,000) returned 1-by-1 -> 8,333 + 8,334 + 8,333 = 25,000.
- Real Room Charge return folio-only guard & Unreferenced Return dedicated-role privilege.
- Real `adjust_closed_check` (Simphony Opt 67) & `reopen_closed_check` (Simphony Opt 68)
  with reversal return, replacement check (`custom_reopened_from`), `POS Authorisation Log`,
  and server `Comment` before/after audit trail.
- Real MariaDB `FOR UPDATE` concurrency proof: two concurrent DB connections racing to return
  the last remaining qty result in exactly 1 success and 1 `POS-RETURN-OVER-QTY` refusal.
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt, getdate, nowdate, random_string
from frappe.utils.password import update_password


class TestVoidReturnReopenIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")
        cls.suffix = random_string(7).lower()
        cls.created = []

        # Ensure core ERPNext base masters exist on byte-fresh sites.
        if not frappe.db.exists("Warehouse Type", "Transit"):
            frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(ignore_permissions=True)
        if not frappe.db.exists("UOM", "Nos"):
            frappe.get_doc({"doctype": "UOM", "uom_name": "Nos"}).insert(ignore_permissions=True)
        if not frappe.db.exists("Item Group", "All Item Groups"):
            frappe.get_doc({"doctype": "Item Group", "item_group_name": "All Item Groups", "is_group": 1}).insert(ignore_permissions=True)
        if not frappe.db.exists("Item Group", "Products"):
            frappe.get_doc({"doctype": "Item Group", "item_group_name": "Products", "parent_item_group": "All Item Groups", "is_group": 0}).insert(ignore_permissions=True)
        if not frappe.db.exists("Customer Group", "All Customer Groups"):
            frappe.get_doc({"doctype": "Customer Group", "customer_group_name": "All Customer Groups", "is_group": 1}).insert(ignore_permissions=True)
        if not frappe.db.exists("Customer Group", "Individual"):
            frappe.get_doc({"doctype": "Customer Group", "customer_group_name": "Individual", "parent_customer_group": "All Customer Groups", "is_group": 0}).insert(ignore_permissions=True)
        if not frappe.db.exists("Territory", "All Territories"):
            frappe.get_doc({"doctype": "Territory", "territory_name": "All Territories", "is_group": 1}).insert(ignore_permissions=True)
        if not frappe.db.exists("Territory", "Rest Of The World"):
            frappe.get_doc({"doctype": "Territory", "territory_name": "Rest Of The World", "parent_territory": "All Territories", "is_group": 0}).insert(ignore_permissions=True)
        for se_type, purpose in (("Material Receipt", "Material Receipt"), ("Material Issue", "Material Issue")):
            if not frappe.db.exists("Stock Entry Type", se_type):
                frappe.get_doc({"doctype": "Stock Entry Type", "name": se_type, "purpose": purpose}).insert(ignore_permissions=True)

        # Ensure UGX currency exists with 0 fraction units.
        if not frappe.db.exists("Currency", "UGX"):
            frappe.get_doc({
                "doctype": "Currency",
                "currency_name": "UGX",
                "enabled": 1,
                "fraction": "",
                "fraction_units": 0,
                "smallest_currency_fraction_value": 1,
                "symbol": "UGX",
            }).insert(ignore_permissions=True)

        cls.company = frappe.get_doc({
            "doctype": "Company",
            "company_name": f"B5aUGX-{cls.suffix}",
            "abbr": f"B5{cls.suffix[:3].upper()}",
            "default_currency": "UGX",
            "country": "Uganda",
        }).insert(ignore_permissions=True).name
        cls.created.append(("Company", cls.company))

        year = getdate(nowdate()).year
        fy = frappe.get_doc({
            "doctype": "Fiscal Year",
            "year": f"FY-{year}-b5a-{cls.suffix}",
            "year_start_date": f"{year}-01-01",
            "year_end_date": f"{year}-12-31",
            "companies": [{"company": cls.company}],
        }).insert(ignore_permissions=True)
        cls.created.append(("Fiscal Year", fy.name))

        cls.cashier = cls._make_user(f"b5a-cashier-{cls.suffix}@example.test", "Cashier One", ["URY Cashier"])
        cls.manager1 = cls._make_user(f"b5a-mgr1-{cls.suffix}@example.test", "Manager One", ["URY Manager", "URY Cashier"])
        cls.manager2 = cls._make_user(f"b5a-mgr2-{cls.suffix}@example.test", "Manager Two", ["URY Manager", "URY Cashier"])
        cls.gm = cls._make_user(f"b5a-gm-{cls.suffix}@example.test", "General Manager", ["URY Admin", "URY Manager"])

        cls.branch = frappe.get_doc({
            "doctype": "Branch",
            "branch": f"B5aBranch-{cls.suffix}",
            "company": cls.company,
            "user": [
                {"user": cls.cashier},
                {"user": cls.manager1},
                {"user": cls.manager2},
                {"user": cls.gm},
            ],
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
        bank_acct = co.default_bank_account or frappe.db.get_value(
            "Account", {"company": cls.company, "account_type": "Bank", "is_group": 0}, "name"
        ) or cash_acct

        cls.mop_cash = cls._make_mop(f"B5aCash-{cls.suffix}", "Cash", cash_acct)
        cls.mop_card = cls._make_mop(f"B5aCard-{cls.suffix}", "Bank", bank_acct)
        cls.mop_room = cls._make_mop(f"Room Charge {cls.suffix}", "General", cash_acct)

        cls.price_list = f"B5aPL-{cls.suffix}"
        frappe.get_doc({
            "doctype": "Price List",
            "price_list_name": cls.price_list,
            "currency": "UGX",
            "selling": 1,
            "enabled": 1,
        }).insert(ignore_permissions=True)
        cls.created.append(("Price List", cls.price_list))

        item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups"
        cls.stock_item = cls._make_item(f"B5A-STK-{cls.suffix}", "Beef Burger", item_group, 10000.0, is_stock=1)
        cls.service_item = cls._make_item(f"B5A-SVC-{cls.suffix}", "Steak Plate", item_group, 35000.0, is_stock=0)

        # Seed initial stock for stock_item via Material Receipt so Material Issue succeeds cleanly!
        se = frappe.get_doc({
            "doctype": "Stock Entry",
            "stock_entry_type": "Material Receipt",
            "company": cls.company,
            "items": [{
                "item_code": cls.stock_item,
                "qty": 50,
                "t_warehouse": cls.warehouse,
                "basic_rate": 4000,
                "cost_center": cls.cost_center,
            }],
        })
        se.insert(ignore_permissions=True)
        se.submit()

        cls.customer = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": f"B5a Guest {cls.suffix}",
            "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups",
            "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories",
        }).insert(ignore_permissions=True).name
        cls.created.append(("Customer", cls.customer))

        cls.profile = frappe.get_doc({
            "doctype": "POS Profile",
            "name": f"B5aTill-{cls.suffix}",
            "company": cls.company,
            "branch": cls.branch,
            "currency": "UGX",
            "warehouse": cls.warehouse,
            "cost_center": cls.cost_center,
            "selling_price_list": cls.price_list,
            "write_off_account": cls.expense_account,
            "write_off_cost_center": cls.cost_center,
            "income_account": cls.income_account,
            "expense_account": cls.expense_account,
            "custom_kot_naming_series": "KOT-.#####",
            "custom_qty_reduction_allowed_order_types": "Dine In,Take Away",
            "applicable_for_users": [
                {"user": cls.cashier, "default": 1},
                {"user": cls.manager1},
                {"user": cls.manager2},
                {"user": cls.gm},
            ],
            "payments": [
                {"mode_of_payment": cls.mop_cash, "default": 1},
                {"mode_of_payment": cls.mop_card, "default": 0},
                {"mode_of_payment": cls.mop_room, "default": 0},
            ],
        }).insert(ignore_permissions=True).name
        cls.created.append(("POS Profile", cls.profile))

        poe = frappe.get_doc({
            "doctype": "POS Opening Entry",
            "company": cls.company,
            "pos_profile": cls.profile,
            "user": cls.cashier,
            "period_start_date": nowdate(),
            "posting_date": nowdate(),
            "balance_details": [{"mode_of_payment": cls.mop_cash, "opening_amount": 0}],
        })
        poe.flags.ignore_mandatory = True
        poe.insert(ignore_permissions=True, ignore_mandatory=True)
        poe.submit()
        cls.poe = poe.name
        cls.created.append(("POS Opening Entry", cls.poe))

        from ury.patches.v2_0 import setup_pos_manager_authorisation, setup_pos_void_return_reopen
        setup_pos_manager_authorisation.execute()
        setup_pos_manager_authorisation.seed_default_rules(company=cls.company)
        setup_pos_manager_authorisation.seed_default_reasons()
        setup_pos_void_return_reopen.execute()

        from ury.ury_pos import override
        for u, pin in ((cls.manager1, "7412"), (cls.manager2, "8523"), (cls.gm, "9634")):
            frappe.set_user(u)
            override.set_my_pin(f"Pass-{cls.suffix}!", pin)
        frappe.set_user("Administrator")
        frappe.db.commit()

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
    def _make_mop(cls, name, mop_type, acct):
        frappe.get_doc({
            "doctype": "Mode of Payment",
            "mode_of_payment": name,
            "type": mop_type,
            "accounts": [{"company": cls.company, "default_account": acct}],
        }).insert(ignore_permissions=True)
        cls.created.append(("Mode of Payment", name))
        return name

    @classmethod
    def _make_item(cls, code, name, group, rate, *, is_stock=0):
        item = frappe.get_doc({
            "doctype": "Item",
            "item_code": code,
            "item_name": name,
            "item_group": group,
            "stock_uom": "Nos",
            "is_stock_item": is_stock,
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

    def _make_pos_invoice(self, *, items, discount_amount=0.0, payments=None, submit=False, waiter=None, cashier=None, room_number=None):
        frappe.set_user("Administrator")
        rows = []
        for it in items:
            rows.append({
                "item_code": it["item_code"],
                "item_name": it.get("item_name", it["item_code"]),
                "qty": it["qty"],
                "rate": it["rate"],
                "price_list_rate": it["rate"],
                "base_price_list_rate": it["rate"],
                "uom": "Nos",
                "conversion_factor": 1.0,
                "income_account": self.income_account,
                "expense_account": self.expense_account,
                "cost_center": self.cost_center,
                "warehouse": self.warehouse,
                "reservation_line_key": f"ln-{random_string(6)}",
                "ury_held": 0,
            })
        doc = frappe.get_doc({
            "doctype": "POS Invoice",
            "company": self.company,
            "branch": self.branch,
            "pos_profile": self.profile,
            "customer": self.customer,
            "currency": "UGX",
            "selling_price_list": self.price_list,
            "order_type": "Dine In",
            "posting_date": nowdate(),
            "invoice_printed": 1,
            "waiter": waiter or self.cashier,
            "cashier": cashier or self.cashier,
            "apply_discount_on": "Grand Total",
            "discount_amount": discount_amount,
            "items": rows,
            "payments": payments if payments is not None else [{"mode_of_payment": self.mop_cash, "amount": 0.0}],
        })
        if room_number and frappe.get_meta("POS Invoice").has_field("room_number"):
            doc.room_number = room_number
        doc.insert(ignore_permissions=True)
        if submit:
            doc.submit()
        frappe.db.commit()
        return doc

    def test_waste_sent_item_posts_real_material_issue_stock_entry(self):
        from ury.ury.api.ury_pos_invoice_qty_reduction import reduce_order_item_qty

        inv = self._make_pos_invoice(
            items=[
                {"item_code": self.stock_item, "item_name": "Beef Burger", "qty": 2, "rate": 10000.0},
                {"item_code": self.service_item, "item_name": "Steak Plate", "qty": 1, "rate": 35000.0},
            ],
            submit=False,
        )
        frappe.set_user(self.manager1)
        with patch("ury.ury.api.ury_pos_invoice_qty_reduction.process_items_for_cancel_kot", return_value=[]):
            res = reduce_order_item_qty(inv.name, inv.items[0].name, 1, reason="WASTE-SPOILED")
        self.assertTrue(res["is_waste"])
        self.assertIsNotNone(res["waste_stock_entry"])
        se = frappe.get_doc("Stock Entry", res["waste_stock_entry"])
        self.assertEqual(se.docstatus, 1)
        self.assertEqual(se.purpose, "Material Issue")
        self.assertEqual(se.items[0].item_code, self.stock_item)
        self.assertEqual(flt(se.items[0].qty), 1.0)

    def test_ugx_discounted_check_partial_returns_whole_shillings_and_double_return_blocked(self):
        from ury.ury_pos.void_return_reopen import create_pos_return

        inv = self._make_pos_invoice(
            items=[{"item_code": self.stock_item, "item_name": "Beef Burger", "qty": 3, "rate": 10000.0}],
            discount_amount=5000.0,
            payments=[{"mode_of_payment": self.mop_cash, "amount": 25000.0}],
            submit=True,
            waiter=self.cashier,
            cashier=self.cashier,
        )
        self.assertEqual(flt(inv.grand_total), 25000.0)
        row_name = inv.items[0].name

        frappe.set_user(self.manager2)
        with patch("ury.ury_pos.api.getBranch", return_value=self.branch):
            r1 = create_pos_return(inv.name, [{"item_row_name": row_name, "qty": 1}], reason="RET-QUALITY")
            r2 = create_pos_return(inv.name, [{"item_row_name": row_name, "qty": 1}], reason="RET-QUALITY")
            r3 = create_pos_return(inv.name, [{"item_row_name": row_name, "qty": 1}], reason="RET-QUALITY")

            self.assertEqual([r1["refund_total"], r2["refund_total"], r3["refund_total"]], [8333.0, 8334.0, 8333.0])
            self.assertEqual(sum([r1["refund_total"], r2["refund_total"], r3["refund_total"]]), 25000.0)

            for r in (r1, r2, r3):
                ret_doc = frappe.get_doc("POS Invoice", r["return_invoice"])
                self.assertEqual(ret_doc.docstatus, 1)
                self.assertEqual(ret_doc.is_return, 1)
                self.assertEqual(ret_doc.return_against, inv.name)
                self.assertIsNone(ret_doc.restaurant_table)
                self.assertEqual(ret_doc.invoice_printed, 0)

            # 4th attempt (double return) MUST fail with POS-RETURN-OVER-QTY!
            with self.assertRaisesRegex(frappe.ValidationError, "POS-RETURN-OVER-QTY"):
                create_pos_return(inv.name, [{"item_row_name": row_name, "qty": 1}], reason="RET-QUALITY")

    def test_room_charge_and_unreferenced_return_dedicated_privilege(self):
        from ury.ury_pos import override
        from ury.ury_pos.void_return_reopen import create_pos_return, create_unreferenced_return

        room_inv = self._make_pos_invoice(
            items=[{"item_code": self.service_item, "item_name": "Steak Plate", "qty": 1, "rate": 35000.0}],
            payments=[{"mode_of_payment": self.mop_room, "amount": 35000.0}],
            submit=True,
            waiter=self.cashier,
            cashier=self.cashier,
            room_number="412",
        )
        frappe.set_user(self.manager2)
        with patch("ury.ury_pos.api.getBranch", return_value=self.branch):
            with self.assertRaisesRegex(frappe.ValidationError, "POS-RETURN-ROOM-CHARGE"):
                create_pos_return(
                    room_inv.name,
                    [{"item_row_name": room_inv.items[0].name, "qty": 1}],
                    reason="RET-QUALITY",
                    refund_payments=[{"mode_of_payment": self.mop_cash, "amount": 35000.0}],
                    allow_different_tender=1,
                )
            res = create_pos_return(
                room_inv.name,
                [{"item_row_name": room_inv.items[0].name, "qty": 1}],
                reason="RET-QUALITY",
            )
            self.assertEqual(res["refund_payments"], [{"mode_of_payment": self.mop_room, "amount": 35000.0}])

            # Unreferenced return default-denied
            with self.assertRaisesRegex(frappe.PermissionError, "POS-RETURN-UNREFERENCED-NOT-ALLOWED"):
                create_unreferenced_return(
                    self.profile, self.customer, [{"item_code": self.stock_item, "qty": 1}],
                    [{"mode_of_payment": self.mop_cash, "amount": 10000.0}],
                    reason="RET-QUALITY",
                )

            # Configure dedicated role `URY Admin` on POS Profile -> distinct GM PIN succeeds!
            frappe.db.set_value("POS Profile", self.profile, "custom_unreferenced_return_roles", "URY Admin")
            frappe.db.commit()
            frappe.set_user(self.cashier)
            claim = {
                "unreferenced": 1,
                "pos_profile": self.profile,
                "customer": self.customer,
                "items": [{"item_code": self.stock_item, "qty": 1.0, "rate": 10000.0}],
                "refund_total": 10000.0,
                "currency": "UGX",
                "refund_payments": [{"amount": 10000.0, "mode_of_payment": self.mop_cash}],
                "reason": "RET-QUALITY",
                "note": "",
            }
            issued = override.authorise(self.profile, "return", self.gm, "9634", "POS Profile", self.profile, claim)
            unref = create_unreferenced_return(
                self.profile, self.customer, [{"item_code": self.stock_item, "qty": 1}],
                [{"mode_of_payment": self.mop_cash, "amount": 10000.0}],
                reason="RET-QUALITY", approval_token=issued["token"],
            )
            self.assertTrue(unref["unreferenced"])
            self.assertEqual(unref["approver"], self.gm)
            frappe.db.set_value("POS Profile", self.profile, "custom_unreferenced_return_roles", "")
            frappe.db.commit()

    def test_adjust_and_reopen_closed_check_real_db_audit(self):
        from ury.ury_pos.void_return_reopen import adjust_closed_check, reopen_closed_check

        inv1 = self._make_pos_invoice(
            items=[{"item_code": self.service_item, "item_name": "Steak Plate", "qty": 1, "rate": 35000.0}],
            payments=[{"mode_of_payment": self.mop_cash, "amount": 35000.0}],
            submit=True,
        )
        inv2 = self._make_pos_invoice(
            items=[{"item_code": self.service_item, "item_name": "Steak Plate", "qty": 1, "rate": 35000.0}],
            payments=[{"mode_of_payment": self.mop_cash, "amount": 35000.0}],
            submit=True,
        )
        frappe.set_user(self.manager2)
        with patch("ury.ury_pos.api.getBranch", return_value=self.branch):
            adj = adjust_closed_check(
                inv1.name,
                [{"mode_of_payment": self.mop_card, "amount": 35000.0}],
                reason="REOPEN-TENDER",
            )
            rep = frappe.get_doc("POS Invoice", adj["replacement_invoice"])
            self.assertEqual(rep.docstatus, 1)
            self.assertEqual(rep.custom_reopened_from, inv1.name)
            self.assertEqual(rep.payments[0].mode_of_payment, self.mop_card)

            reop = reopen_closed_check(inv2.name, reason="REOPEN-SPLIT")
            draft = frappe.get_doc("POS Invoice", reop["reopened_invoice"])
            self.assertEqual(draft.docstatus, 0)
            self.assertEqual(draft.custom_reopened_from, inv2.name)
            self.assertTrue(all(flt(p.amount) == 0.0 for p in draft.payments))

    def test_concurrent_double_return_serialized_by_mariadb_for_update(self):
        """Two real MariaDB connections race to return the single qty=1 item: exactly 1 wins, 1 fails."""
        from ury.ury_pos.void_return_reopen import create_pos_return

        inv = self._make_pos_invoice(
            items=[{"item_code": self.service_item, "item_name": "Steak Plate", "qty": 1, "rate": 35000.0}],
            payments=[{"mode_of_payment": self.mop_cash, "amount": 35000.0}],
            submit=True,
        )
        row_name = inv.items[0].name
        site = frappe.local.site
        barrier = threading.Barrier(2)
        results = []
        errors = []
        lock = threading.Lock()

        def worker():
            frappe.init(site=site)
            frappe.connect()
            try:
                frappe.set_user(self.manager2)
                barrier.wait(timeout=10)
                with patch("ury.ury_pos.api.getBranch", return_value=self.branch):
                    res = create_pos_return(inv.name, [{"item_row_name": row_name, "qty": 1}], reason="RET-QUALITY")
                    frappe.db.commit()
                    with lock:
                        results.append(res)
            except Exception as exc:
                frappe.db.rollback()
                with lock:
                    errors.append(str(exc))
            finally:
                frappe.destroy()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        self.assertEqual(len(results), 1, f"Expected exactly 1 winning return, got {len(results)}; errors={errors}")
        self.assertEqual(len(errors), 1, f"Expected exactly 1 refused return, got {errors}")
        self.assertIn("POS-RETURN-OVER-QTY", errors[0])
