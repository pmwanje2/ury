"""MariaDB 11.x InnoDB 65,535-byte row-size budget for `tabPOS Invoice`.

When ERPNext v16 (~33,683B) and `uganda_compliance` EFRIS (~9,031B) are both
installed alongside `ury`, every unbounded `Data`/`Select`/`Link` custom field
on `POS Invoice` consumes `varchar(140)` utf8mb4 = 562 bytes of inline row
width (and `length=200` consumes 802 bytes). Without explicit `length=64`
bounds on short/fixed-width fields, `bench install-app ury` exceeds MariaDB's
65,535-byte limit with OperationalError 1118 ("Row size too large").

This test runs siteless (no Frappe or MariaDB required) by inspecting every
`POS Invoice` custom field source in `ury` (`fixtures/custom_field.json`,
`setup_customizations.get_custom_fields()`, `privileged_fields`,
`kds_device_fields`, and `modifier_combo_fields`).
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Measured on live MariaDB 11.8.9 (`information_schema.COLUMNS`, utf8mb4) for
# Frappe v16 + ERPNext v16 (126 cols = 33,683B) + uganda_compliance (23 cols = 9,031B).
ERPNEXT_PLUS_EFRIS_BASELINE_BYTES = 42_714
MARIADB_MAX_ROW_BYTES = 65_535
# Require >= 4,000B safety margin below MariaDB's 65,535B hard cap so future
# ERPNext/EFRIS minor upgrades or additional numeric/text fields cannot trip 1118.
MAX_ALLOWED_TOTAL_ROW_BYTES = 61_500

VARCHAR_FIELDTYPES = frozenset(
	{"Data", "Select", "Link", "Dynamic Link", "Read Only", "Password", "Phone", "Autocomplete"}
)
TEXT_FIELDTYPES = frozenset(
	{"Small Text", "Text", "Long Text", "Text Editor", "Code", "HTML Editor", "Markdown Editor", "JSON"}
)
NO_COLUMN_FIELDTYPES = frozenset(
	{"Section Break", "Column Break", "Tab Break", "Table", "Table MultiSelect", "Button", "HTML", "Heading"}
)


def _frappe_mariadb_column_bytes(fieldtype: str, length: int | None) -> int:
	"""Mirror Frappe `database/schema.py::get_definition` utf8mb4 inline byte width."""
	if fieldtype in NO_COLUMN_FIELDTYPES:
		return 0
	if fieldtype in VARCHAR_FIELDTYPES:
		# Frappe clamps any explicit varchar length < 64 up to 64 and defaults 0/None to 140.
		if length and int(length) > 0:
			chars = max(64, int(length))
		else:
			chars = 140
		raw = chars * 4
		return raw + (2 if raw > 255 else 1)
	if fieldtype in TEXT_FIELDTYPES:
		return 12
	if fieldtype in ("Currency", "Float", "Percent"):
		return 10
	if fieldtype in ("Datetime", "Timestamp"):
		return 8
	if fieldtype in ("Int", "Date", "Time"):
		return 4
	if fieldtype == "Check":
		return 1
	return 8


def _extract_dict_function(path: Path, func_name: str) -> dict[str, list[dict]]:
	"""Parse a module AST and safely evaluate the literal dict returned by `func_name`."""
	tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
	for node in tree.body:
		if isinstance(node, ast.FunctionDef) and node.name == func_name:
			for stmt in node.body:
				if isinstance(stmt, ast.Return) and stmt.value is not None:
					return ast.literal_eval(stmt.value)
	raise AssertionError(f"Could not locate literal return dict in {path}:{func_name}")


def _load_all_ury_pos_invoice_fields() -> dict[str, list[tuple[str, dict]]]:
	"""Return fieldname -> list of (source_name, field_spec) across all URY sources."""
	by_field: dict[str, list[tuple[str, dict]]] = {}

	def _record(source: str, rows: list[dict]) -> None:
		for row in rows:
			fn = row.get("fieldname")
			if not fn:
				continue
			by_field.setdefault(fn, []).append((source, row))

	# 1. Fixture export
	fixtures = json.loads((ROOT / "fixtures" / "custom_field.json").read_text(encoding="utf-8"))
	_record("fixtures/custom_field.json", [r for r in fixtures if r.get("dt") == "POS Invoice"])

	# 2. setup_customizations.get_custom_fields()
	setup_fields = _extract_dict_function(ROOT / "setup_customizations.py", "get_custom_fields")
	_record("setup_customizations.py", setup_fields.get("POS Invoice", []))

	# 3. privileged_fields.get_privileged_action_custom_fields()
	priv_fields = _extract_dict_function(
		ROOT / "ury_pos" / "privileged_fields.py", "get_privileged_action_custom_fields"
	)
	_record("ury_pos/privileged_fields.py", priv_fields.get("POS Invoice", []))

	# 4. kds_device_fields.get_kds_device_custom_fields() (if it defines POS Invoice fields)
	kds_path = ROOT / "ury_pos" / "kds_device_fields.py"
	if kds_path.exists():
		try:
			kds_fields = _extract_dict_function(kds_path, "get_kds_device_custom_fields")
			_record("ury_pos/kds_device_fields.py", kds_fields.get("POS Invoice", []))
		except AssertionError:
			pass

	return by_field


class TestPosInvoiceRowSizeBudget(unittest.TestCase):
	def test_pos_invoice_total_row_bytes_within_mariadb_65535_budget(self):
		by_field = _load_all_ury_pos_invoice_fields()
		self.assertGreater(len(by_field), 30, "Expected URY POS Invoice custom fields to be loaded")

		# Use the worst-case (widest) column width across all sources that declare each field,
		# because `after_install()` and `sync_fixtures()` both execute during fresh install.
		ury_bytes = 0
		per_field_bytes: dict[str, int] = {}
		for fn, entries in sorted(by_field.items()):
			worst = max(
				_frappe_mariadb_column_bytes(spec.get("fieldtype", ""), spec.get("length"))
				for _src, spec in entries
			)
			per_field_bytes[fn] = worst
			ury_bytes += worst

		projected_total = ERPNEXT_PLUS_EFRIS_BASELINE_BYTES + ury_bytes
		self.assertLessEqual(
			projected_total,
			MAX_ALLOWED_TOTAL_ROW_BYTES,
			f"tabPOS Invoice projected row size {projected_total}B "
			f"(baseline {ERPNEXT_PLUS_EFRIS_BASELINE_BYTES}B + URY {ury_bytes}B) "
			f"exceeds safety budget {MAX_ALLOWED_TOTAL_ROW_BYTES}B (MariaDB limit {MARIADB_MAX_ROW_BYTES}B). "
			f"Bound short varchar fields to length=64 across setup_customizations.py, "
			f"privileged_fields.py, and fixtures/custom_field.json.",
		)

	def test_duplicate_declared_pos_invoice_varchar_fields_agree_on_length(self):
		"""Fields defined in both setup_customizations.py and fixtures/custom_field.json must match length."""
		by_field = _load_all_ury_pos_invoice_fields()
		mismatches = []
		for fn, entries in sorted(by_field.items()):
			varchar_entries = [
				(src, max(64, int(spec["length"])) if spec.get("length") else 140)
				for src, spec in entries
				if spec.get("fieldtype") in VARCHAR_FIELDTYPES
			]
			if len(varchar_entries) > 1:
				widths = {w for _src, w in varchar_entries}
				if len(widths) > 1:
					mismatches.append((fn, varchar_entries))
		self.assertEqual(
			mismatches,
			[],
			f"Conflicting varchar widths across URY POS Invoice custom field sources: {mismatches}",
		)


if __name__ == "__main__":
	unittest.main()
