"""Custom Field fixture integrity; no site or database is required.

Ported from the Maximus fork 2fbb6e04 (one fixture row per field), which lived
in the reservation merge-safety file (theme T9); it stands alone here. The
second check is new: a row whose name is missing from the hooks.py fixtures
filter is dropped by the next ``bench export-fixtures``.
"""

import collections
import json
import unittest
from pathlib import Path

from ury import hooks

ROOT = Path(__file__).resolve().parents[2]


def _fixture_rows():
	return json.loads((ROOT / "fixtures" / "custom_field.json").read_text(encoding="utf-8"))


def _filter_names():
	fixture = next(row for row in hooks.fixtures if row.get("doctype") == "Custom Field")
	return next(values for field, operator, values in fixture["filters"] if (field, operator) == ("name", "in"))


class TestCustomFieldFixtureHasOneRowPerField(unittest.TestCase):
	def test_each_custom_field_name_appears_once(self):
		rows = _fixture_rows()
		repeated = sorted(name for name, count in collections.Counter(row["name"] for row in rows).items() if count > 1)
		self.assertEqual(repeated, [])
		names = {row["name"] for row in rows}
		for field in ("custom_reservation_settings_section", "custom_enable_reservation", "custom_buffer_time",
				"custom_grace_period", "custom_unset_grace_policy", "custom_min_reservation_notice",
				"custom_target_turn_time",
				"custom_avg_table_time_last_day", "custom_avg_table_time_last_week"):
			self.assertIn(f"Branch-{field}", names)


class TestCustomFieldFixtureIsExportable(unittest.TestCase):
	def test_every_fixture_row_is_named_in_the_hooks_filter(self):
		names = _filter_names()
		missing = sorted(row["name"] for row in _fixture_rows() if row["name"] not in names)
		self.assertEqual(missing, [])


if __name__ == "__main__":
	unittest.main()
