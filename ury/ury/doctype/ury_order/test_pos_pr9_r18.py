"""Issued rows remain immutable across local-key collisions and multi-station rounds."""
import unittest
from unittest.mock import Mock, patch

import frappe

from ury.ury.api import table_lifecycle, ury_kot_generate as kot, ury_pos_invoice_qty_reduction as reduction
from ury.ury.doctype.ury_order import test_pos_pr9_r17 as r17, test_ury_order as access, ury_order as order
from ury.ury_pos import floor_actions


class TestIssuedOwnership(unittest.TestCase):
    def setUp(self):
        self.fx = r17.TestKitchenOwnership()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.currencies = self.fx.checks.fixture.currencies

    def configure(self, doc, places, rows):
        doc.db_set("custom_split_balance", None, update_modified=False)
        doc = frappe.get_doc("POS Invoice", doc.name)
        doc.set_paid_amount = doc.set_outstanding_amount = Mock()
        doc.set("items", [])
        for index, values in enumerate(rows):
            code = values["item_code"]
            if not frappe.db.exists("Item", code):
                frappe.get_doc(dict(doctype="Item", name=code, item_code=code)).db_insert()
            doc.append("items", dict(dict(name=f"{doc.name}-row-{index}", item_name=code, qty=1,
                                          price_list_rate=100, rate=100, conversion_factor=1), **values))
        doc.calculate_taxes_and_totals()
        self.fx.checks.fixture.fixture.snapshot(doc, places)
        for row in doc.items:
            row.db_insert()
        self.fx.persist(doc)
        return doc.reload()

    def issue(self, doc, rows):
        items = list(kot._line_keyed_items(order._kitchen_snapshot(rows)).values())
        return kot.process_items_for_kot(doc.name, doc.customer, None, items, "", doc.pos_profile,
                                         "KOT-.####", "New Order", require_routing=True)

    def void(self, doc, row, remaining=0):
        with self.fx.use_invoice(doc):
            result = reduction.reduce_order_item_qty(doc.name, row.name, remaining,
                                                     reason="Guest", approval_token="token")
        doc.reload()
        return [frappe.get_doc("URY KOT", key) for key in result["cancel_kot_names"]]

    def split(self, doc, moved):
        child = frappe.get_doc(dict(doctype="POS Invoice", name=doc.currency + "-r18-child",
                                    disable_rounded_total=1, ignore_pricing_rule=1))
        for check in (doc, child):
            check.set_missing_values = check.run_method = Mock()
            check.set_paid_amount = check.set_outstanding_amount = Mock()
        child.insert = lambda: self.fx.checks.reload(child)
        doc.save = lambda: self.fx.persist(doc)
        with patch.object(frappe, "new_doc", return_value=child), patch.object(
                order, "get_branch_stock_policy", return_value=frappe._dict(reservation_control_enabled=0)):
            order._apply_check_split(doc, {row.name: row.qty for row in moved}, doc.company)
        doc.reload()
        return child.reload()

    def test_modifier_stays_with_its_dish(self):
        for currency, places in self.currencies:
            with self.subTest(currency=currency):
                soup, salad = self.fx.prepare(currency, places)
                first = self.fx.fire(soup)
                salad = self.configure(salad, places, [
                    dict(item_code="Salad", reservation_line_key="dish"),
                    dict(item_code="Dressing", reservation_line_key="side", custom_line_role="Modifier",
                         custom_is_modifier=1, custom_parent_line_key="dish", price_list_rate=5, rate=5),
                ])
                self.issue(salad, salad.items)
                doc = self.fx.merge(soup, salad)
                cancelled = self.void(doc, doc.items[0])
                self.assertEqual([row.item_code for row in doc.items], ["Salad", "Dressing"])
                self.assertEqual(doc.items[1].custom_parent_line_key, doc.items[0].reservation_line_key)
                self.assertEqual([ticket.original_kot for ticket in cancelled], [first.name])
                self.assertEqual(doc.grand_total, 105)

    def test_original_rows_are_immutable_through_resplit_and_void(self):
        for currency, places in self.currencies:
            with self.subTest(currency=currency):
                left, right = self.fx.prepare(currency, places)
                issued = [self.fx.fire(check).name for check in (left, right)]
                def submitted_rows():
                    return frappe.db.sql("SELECT * FROM `tabURY KOT Items` WHERE parent IN %s ORDER BY name",
                                         (tuple(issued),))
                before = submitted_rows()
                doc = self.fx.merge(left, right)
                self.assertEqual(submitted_rows(), before)
                doc = self.fx.merge(doc, self.fx.tea(doc))
                child = self.split(doc, [doc.items[1], doc.items[-1]])
                self.assertEqual(self.void(child, child.items[0])[0].original_kot, issued[1])
                self.assertEqual(frappe.db.get_value("URY KOT", issued[0], "invoice"), doc.name)
                self.assertEqual((doc.grand_total, child.grand_total), (100, 15))
                self.assertEqual(submitted_rows(), before)

    def test_cancel_newest_round_at_both_receiving_stations(self):
        for currency, places in self.currencies:
            with self.subTest(currency=currency):
                doc, _ = self.fx.prepare(currency, places, qty=2)
                original_profile = doc.pos_profile
                target_profile = currency + "-R18-B"
                target_branch = self.fx.outlet(doc.company, target_profile)
                for profile, branch in ((original_profile, doc.branch), (target_profile, target_branch)):
                    frappe.get_doc(dict(doctype="URY Production Unit", name=profile+"-bar", branch=branch)).db_insert()
                with patch.object(kot, "_resolve_units_for_item_code", side_effect=lambda *args, pos_profile_id, **kw:
                                  [pos_profile_id + "-kitchen", pos_profile_id + "-bar"]):
                    doc.items[0].ury_held = 0
                    self.fx.persist(doc)
                    unit = dict(doc.items[0].as_dict(), qty=1)
                    first = self.issue(doc, [unit])
                    with self.fx.use_invoice(doc), patch.object(floor_actions, "authorize_or_perform",
                                                                return_value={"approver": "Manager"}):
                        floor_actions.transfer_check_outlet(doc.name, target_profile)
                    doc.reload()
                    second = self.issue(doc, [unit])
                    self.assertEqual((len(first), len(second)), (2, 2))
                    cancelled = self.void(doc, doc.items[0], remaining=1)
                    self.assertCountEqual([(c.production, c.original_kot, c.kot_items[0].cancelled_qty)
                                           for c in cancelled], [(frappe.db.get_value("URY KOT", s, "production"), s, 1)
                                                                 for s in second])
                    self.assertEqual(doc.grand_total, 100)

    def legacy_pair(self, ambiguous):
        for currency, places in self.currencies:
            with self.subTest(currency=currency):
                doc, _ = self.fx.prepare(currency, places)
                doc = self.configure(doc, places, [dict(item_code="Soup"),
                                     dict(item_code="Soup", reservation_line_key=None if ambiguous else "other")])
                issued = [self.issue(doc, [row])[0] for row in doc.items]
                tea = self.fx.tea(doc)
                if ambiguous:
                    with self.assertRaisesRegex(frappe.ValidationError, "KOT-OWNERSHIP"):
                        self.fx.merge(tea, doc)
                    self.assertEqual([frappe.db.get_value("URY KOT", name, "invoice") for name in issued],
                                     [doc.name, doc.name])
                else:
                    merged = self.fx.merge(tea, doc)
                    for name in issued:
                        self.assertEqual(self.void(merged, merged.items[1])[0].original_kot, name)
                    self.assertEqual(merged.grand_total, 15)

    def test_identifiable_legacy_dish(self):
        self.legacy_pair(False)

    def test_ambiguous_unkeyed_dishes_are_refused(self):
        self.legacy_pair(True)

    def test_authorize_table_before_reading_seating_policy(self):
        profile = access._make_pos_profile
        for currency, _ in self.currencies:
            for denial in ("active_branch", "assigned_room"):
                with self.subTest(currency=currency, denial=denial):
                    pos = profile()
                    pos.currency = currency
                    with patch.object(access, "_make_pos_profile", return_value=pos), patch.object(
                            table_lifecycle, "assert_table_seatable", side_effect=AssertionError("unauthorized lookup")):
                        getattr(access.TestSyncOrderHardening(), "test_denies_table_outside_" + denial)()
