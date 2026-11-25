"""Native regression tests for the POS configuration sweep review fixes."""

from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.patches.v2_0 import setup_pos_config_sweep as migration
from ury.ury_pos import floor_pin
from ury.ury_pos.covers import parse_covers


class TestLegacyCoverMigration(FrappeTestCase):
    def test_mariadb_parser_matches_runtime_and_retains_original_cover_text(self):
        values = ("\t8\t", " \t+17.00\r\n", "\v\f22 \t", "  42  ", "7.5", "abc", "\t", None,
                  " 2147483648 ", "\u00a09\u00a0")
        expected = (8, 17, 22, 42, 0, 0, 0, 0, 0, 0)
        # This connection-local table shadows the invoice table only for this test.
        frappe.db.sql("""
            CREATE TEMPORARY TABLE `tabPOS Invoice` (
                test_order INT PRIMARY KEY, no_of_pax VARCHAR(64), custom_legacy_covers TEXT
            )
        """)
        try:
            for index, value in enumerate(values):
                frappe.db.sql("INSERT INTO `tabPOS Invoice` (test_order, no_of_pax) VALUES (%s, %s)",
                              (index, value))
            with (patch.object(migration, "create_custom_fields"),
                  patch.object(frappe.db, "get_value", return_value="Data")):
                migration._convert_legacy_covers()
            migrated = frappe.db.sql("""
                SELECT no_of_pax, custom_legacy_covers FROM `tabPOS Invoice` ORDER BY test_order
            """)
            self.assertEqual(tuple(int(value) for value, _ in migrated), expected)
            self.assertEqual(tuple(raw for _, raw in migrated), values)
            runtime = (parse_covers(value) for value in values)
            self.assertEqual(tuple(value if value is not None and 0 <= value <= 2_147_483_647 else 0
                                   for value in runtime), expected)
        finally:
            # MariaDB temporary DDL does not commit. The framework's generic DROP
            # guard cannot distinguish temporary tables from persistent tables.
            frappe.db._cursor.execute("DROP TEMPORARY TABLE `tabPOS Invoice`")


class TestStaffPinIssuance(FrappeTestCase):
    def issue(self, policies):
        profiles = [frappe._dict(name=f"Profile {index}", custom_floor_pin_login=1,
                                 custom_floor_pin_min_length=minimum, custom_floor_pin_max_length=maximum)
                    for index, (minimum, maximum) in enumerate(policies)]
        get_profile = {profile.name: profile for profile in profiles}.__getitem__
        self.write_pin = Mock()
        with (patch.object(frappe, "get_all", return_value=profiles),
              patch.object(frappe, "get_doc", side_effect=lambda _doctype, name: get_profile(name)),
              patch.object(frappe, "has_permission", side_effect=lambda _dt, _mode, doc=None:
                           doc is None or doc is profiles[0]),
              patch.object(floor_pin, "_eligible", side_effect=lambda _user, profile: profile is not profiles[-1]),
              patch.object(frappe.db, "get_value", return_value=None),
              patch.object(frappe.db, "set_value", self.write_pin),
              patch.object(floor_pin, "_digest", return_value="synthetic-hash"),
              patch.object(floor_pin, "add_authentication_log"),
              patch.object(floor_pin.secrets, "randbelow", return_value=42)):
            return floor_pin.issue_pin("employee@example.test")["pin"]

    def test_pin_length_fits_all_eligible_profiles_including_unmanaged_profiles(self):
        pin = self.issue([(4, 8), (6, 6), (9, 9)])
        self.assertEqual(pin, "000042")
        self.write_pin.assert_called_once()

    def test_disjoint_bounds_refuse_issuance_without_overwriting_the_pin(self):
        with self.assertRaisesRegex(frappe.ValidationError, "URY_FLOOR_PIN_POLICY_CONFLICT"):
            self.issue([(4, 4), (6, 6), (9, 9)])
        self.write_pin.assert_not_called()
