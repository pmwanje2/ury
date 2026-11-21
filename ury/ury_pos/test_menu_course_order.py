"""Site-less: the menu DTO carries what a guest screen needs to order its categories.

Oracle Simphony orders a menu's screen look-up groups by configuration on the menu,
never by item name. URY keeps that order in two places: URY Menu Course's
custom_serving_priority and the URY Menu Item row order (idx). The shared menu
resolver must pass both through, because the guest page groups items by course.
"""

import unittest
from unittest.mock import patch

MOD = "ury.ury_pos.api"


class Row(dict):
    __getattr__ = dict.get


MENU_ROWS = [
    Row(idx=3, item="Tea", item_name="Tea", rate=10, special_dish=0, disabled=0, course="Coffee & Tea"),
    Row(idx=1, item="Soup", item_name="Soup", rate=20, special_dish=0, disabled=0, course="Soup"),
    Row(idx=2, item="Chips", item_name="Chips", rate=5, special_dish=0, disabled=0, course=None),
]
PRIORITY = {"Soup": 6, "Coffee & Tea": 0}


class TestMenuCourseOrder(unittest.TestCase):
    def setUp(self):
        from ury.ury_pos import api

        self.api = api

        def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "URY Restaurant":
                return {"name": "Restaurant A", "active_menu": "Menu A", "room_wise_menu": 0,
                        "order_type_wise_menu": 0}.get(fieldname or "name", "Restaurant A")
            if doctype == "URY Menu":
                return "2026-11-20 10:00:00"
            if doctype == "Item":
                return None
            raise AssertionError(f"unexpected get_value {doctype} {filters} {fieldname}")

        def get_all(doctype, filters=None, fields=None, **kwargs):
            if doctype == "URY Menu Item":
                self.menu_item_fields = fields
                return [Row({f: r.get(f) for f in fields}) for r in MENU_ROWS]
            if doctype == "URY Menu Course":
                names = filters["name"][1]
                return [Row(name=n, custom_serving_priority=PRIORITY[n]) for n in names if n in PRIORITY]
            raise AssertionError(f"unexpected get_all {doctype}")

        self.enterContext(patch(f"{MOD}.frappe.db.get_value", side_effect=get_value))
        self.enterContext(patch(f"{MOD}.frappe.get_all", side_effect=get_all))
        self.enterContext(patch(f"{MOD}._", side_effect=lambda text, *args: text))

    def items(self):
        return {row["item"]: row for row in self.api.resolve_restaurant_menu("Branch A")["items"]}

    def test_each_item_carries_its_menu_row_order(self):
        items = self.items()
        self.assertEqual({code: row.get("menu_order") for code, row in items.items()},
                         {"Tea": 3, "Soup": 1, "Chips": 2})

    def test_each_item_carries_its_course_serving_priority(self):
        items = self.items()
        self.assertEqual(items["Soup"].get("course_priority"), 6)

    def test_unset_priority_and_no_course_carry_none(self):
        items = self.items()
        self.assertIn("course_priority", items["Tea"])
        self.assertIsNone(items["Tea"]["course_priority"], "0 is the Int default, not a configured position")
        self.assertIn("course_priority", items["Chips"])
        self.assertIsNone(items["Chips"]["course_priority"])

    def test_staff_item_order_is_unchanged(self):
        result = self.api.resolve_restaurant_menu("Branch A")
        self.assertEqual([row["item"] for row in result["items"]], ["Tea", "Soup", "Chips"])
        self.assertEqual(result["name"], "Menu A")


if __name__ == "__main__":
    unittest.main()
