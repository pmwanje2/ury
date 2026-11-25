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


def _mariadb_utf8mb4_inline_bytes(fieldtype: str, length=0) -> int:
	"""Approximate MariaDB 11.8 utf8mb4 inline row-size bytes matching Frappe v16 schema.py."""
	ft = (fieldtype or "").strip()
	if ft in ("Section Break", "Column Break", "Tab Break", "Table", "Table MultiSelect", "Button", "HTML", "Fold", "Heading"):
		return 0
	if ft in ("Data", "Link", "Select", "Dynamic Link", "Read Only", "Password", "Phone", "Autocomplete", "Barcode", "Color", "Duration", "Icon", "Rating"):
		n = int(length or 0)
		if n <= 0:
			n = 140
		elif n < 64:
			n = 64
		return n * 4 + 2
	if ft in ("Small Text", "Text", "Long Text", "Text Editor", "Code", "HTML Editor", "Markdown Editor", "JSON", "Attach", "Attach Image", "Signature", "Geolocation"):
		return 12
	if ft in ("Currency", "Float", "Percent"):
		return 10
	if ft in ("Datetime", "Time"):
		return 8
	if ft in ("Date", "Int"):
		return 4
	if ft == "Check":
		return 1
	return 16


class TestPosInvoiceMariaDbRowSizeBudget(unittest.TestCase):
	"""Prevent MariaDB 1118 'Row size too large (>65535)' when erpnext + uganda_compliance + ury install together."""

	MAX_URY_POS_INVOICE_INLINE_BYTES = 14000

	def test_pos_invoice_custom_fields_stay_within_mariadb_row_size_budget(self):
		pi_rows = [row for row in _fixture_rows() if row.get("dt") == "POS Invoice"]
		by_name = {row["name"]: row for row in pi_rows}
		for text_field in ("POS Invoice-custom_comments", "POS Invoice-custom_merged_tables"):
			self.assertEqual(
				by_name[text_field].get("fieldtype"),
				"Small Text",
				f"{text_field} must use off-page Small Text instead of inline varchar(140)",
			)
		inline_bytes = sum(
			_mariadb_utf8mb4_inline_bytes(row.get("fieldtype"), row.get("length", 0))
			for row in pi_rows
		)
		self.assertLessEqual(
			inline_bytes,
			self.MAX_URY_POS_INVOICE_INLINE_BYTES,
			f"URY POS Invoice custom fields consume {inline_bytes} inline bytes "
			f"(budget <= {self.MAX_URY_POS_INVOICE_INLINE_BYTES}); risks MariaDB 1118 with uganda_compliance.",
		)


if __name__ == "__main__":
	unittest.main()
