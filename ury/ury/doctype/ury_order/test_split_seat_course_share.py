import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from ury.ury.api import ury_kot_generate as kot
from ury.ury.api import ury_order_reservation_service as reservations
from ury.ury.doctype.ury_order import ury_order as order


def row(name, qty=1, seat=0, parent=None, uom="Serving"):
    return frappe._dict(
        name=name, item_code="Soup", item_name="Soup", qty=qty, uom=uom,
        reservation_line_key=name, custom_seat_no=seat, custom_parent_line_key=parent,
    )


def throw(message, exc=frappe.ValidationError, **kwargs):
    raise exc(message)


class SplitSeatCourseShareTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(order, "_", lambda value: value))
        self.enterContext(patch.object(frappe, "throw", throw))
        self.enterContext(patch.object(frappe, "db", Mock(get_value=self.get_value)))
        self.enterContext(patch.object(kot, "_", lambda value: value, create=True))

    def get_value(self, doctype, name, field, **kwargs):
        if doctype == "UOM":
            return int(name == "Each")
        if doctype == "Item":
            return 1
        if doctype == "URY Menu Course":
            return frappe._dict(name=name, disabled=int(name == "Archived")) if name != "Unknown" else None
        if doctype == "URY Menu Item":
            return "Starter"

    def test_explicit_course_wins_and_seat_is_preserved_by_kitchen_projection(self):
        item = {"item": "Soup", "item_name": "Soup", "qty": 1, "custom_course": "Main", "custom_seat_no": 2}
        self.assertEqual(kot.resolve_line_course(item, "Soup", "Menu"), "Main")
        [projected] = kot.create_order_items(list(kot._line_keyed_items([item]).values()))
        self.assertEqual((projected["custom_course"], projected["custom_seat_no"]), ("Main", 2))

    def test_unknown_or_disabled_course_is_refused(self):
        for course in ("Unknown", "Archived"):
            with self.subTest(course=course), self.assertRaises(frappe.ValidationError):
                kot.resolve_line_course({"custom_course": course}, "Soup", "Menu")

    def test_seat_and_course_distinguish_legacy_lines_after_reordering(self):
        def key(rows):
            return set(reservations._line_quantities(rows))
        first = {"item": "Soup", "qty": 1, "custom_course": "Main", "custom_seat_no": 1}
        second = dict(first, custom_seat_no=2)
        self.assertEqual(len(key([first, second])), 2)
        self.assertEqual(key([first]), key([second, first]) - key([second]))
        self.assertEqual(set(kot._line_keyed_items([first, second])), key([first, second]))

    def test_copy_preserves_seat_and_original_invoice_item(self):
        item = row("line", seat=2)
        copied = order._copy_invoice_item_fields(item, 0.3333)
        self.assertEqual(copied["custom_seat_no"], 2)
        self.assertEqual(copied["custom_origin_invoice_item"], "line")

    def test_split_between_checks_keeps_kitchen_ticket_with_its_anchor(self):
        source = SimpleNamespace(name="ROOT", items=[row("soup"), row("bread")])
        ticket_rows = [frappe._dict(reservation_line_key=k, item="Soup", quantity=1) for k in ("soup", "bread")]
        with patch.object(frappe, "get_all", side_effect=[["KOT"], ticket_rows]):
            self.assertEqual(order._split_kots_to_move(source, {"soup": 1}, True), [])
        with patch.object(frappe, "get_all", side_effect=[["KOT"], ticket_rows]):
            self.assertEqual(order._split_kots_to_move(source, {"soup": 1, "bread": 1}, True), ["KOT"])

    def test_even_thirds_linked_upcharge_and_exact_remainder(self):
        source = frappe._dict(items=[row("soup"), row("upcharge", qty=2, parent="soup")])
        plans = order._plan_split_checks(source, None, "even", 3)
        self.assertEqual(plans, [{"soup": 0.3333, "upcharge": 0.6666}] * 2)
        self.assertEqual(round(1 - sum(plan["soup"] for plan in plans), 4), 0.3334)

    def test_seat_split_moves_parent_and_all_children_to_the_same_check(self):
        source = frappe._dict(items=[row("soup", seat=1), row("bread", seat=2), row("upcharge", parent="bread")])
        self.assertEqual(order._plan_split_checks(source, None, "by_seat", None), [{"bread": 1, "upcharge": 1}])

    def test_whole_number_uom_refuses_fractional_split(self):
        with self.assertRaisesRegex(frappe.ValidationError, "SPLIT-WHOLE-UOM"):
            order._plan_split_checks(frappe._dict(items=[row("soup", uom="Each")]), None, "even", 3)

    def test_manual_cannot_move_orphan_child_or_overallocate(self):
        source = frappe._dict(items=[row("soup"), row("upcharge", parent="soup"), row("bread")])
        for splits in (
            [[{"name": "upcharge", "qty": 1}]],
            [[{"name": "soup", "qty": 1}], [{"name": "soup", "qty": 1}]],
        ):
            with self.subTest(splits=splits), self.assertRaises(frappe.ValidationError):
                order._plan_split_checks(source, splits, "manual", None)

    def test_multi_split_rejects_cross_outlet_check_before_mutation(self):
        source = frappe._dict(name="ROOT", branch="Other", docstatus=0)
        with (
            patch.object(frappe, "get_doc", return_value=source) as get_doc,
            patch.object(frappe, "has_permission", return_value=True),
            patch.object(order, "getBranch", return_value="Main"),
            patch.object(frappe.db, "savepoint", create=True),
            patch.object(frappe.db, "rollback", create=True),
        ):
            with self.assertRaises(frappe.PermissionError):
                order.split_check_multi("ROOT", [], "manual")
            get_doc.assert_called_once_with("POS Invoice", "ROOT", for_update=True)
