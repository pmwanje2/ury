"""Siteless regression test guarding MariaDB 65,535-byte row-size limit on `tabPOS Invoice`.

When `erpnext` v16 (~33,707B across 126 standard columns) and `uganda_compliance`
(~9,031B across 23 `efris_*` columns) are installed alongside `ury`, unbounded
`varchar(140)` (`562B` each in utf8mb4) custom fields on `POS Invoice` push
`tabPOS Invoice` over MariaDB's 65,535-byte inline row-size ceiling (`1118 Row
size too large`).

This test enforces Frappe v16's exact MariaDB column-width rule
(`varchar(max(64, length))` when `length > 0`, else `varchar(140)`) across every
`ury` `POS Invoice` custom-field source (`fixtures/custom_field.json`,
`setup_customizations.py`, `privileged_fields.py`, and
`patches/v3_46/add_pos_fx_ref_offline_fields.py`) so fresh `bench new-site` and
`bench migrate` installs stay safely below 65,535 bytes with headroom.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import unittest


APP_ROOT = Path(__file__).resolve().parents[1]

# Measured on live Frappe v16 / ERPNext v16 / uganda_compliance / MariaDB 11.8.9:
# - ERPNext standard `tabPOS Invoice`: 126 columns = 33,707 inline bytes
# - uganda_compliance `efris_*`:        23 columns =  9,031 inline bytes
MARIADB_ROW_LIMIT_BYTES = 65535
BASELINE_ERPNEXT_PLUS_UGANDA_BYTES = 33707 + 9031
# Leave >= 3,297 bytes for MariaDB NULL-bitmap/row overhead and future columns.
MAX_URY_POS_INVOICE_INLINE_BYTES = 19500

VARCHAR_FIELDTYPES = frozenset(
    {
        "Data",
        "Link",
        "Select",
        "Dynamic Link",
        "Read Only",
        "Phone",
        "Autocomplete",
        "Password",
        "Barcode",
    }
)
TEXT_FIELDTYPES = frozenset(
    {
        "Small Text",
        "Text",
        "Long Text",
        "Code",
        "JSON",
        "Text Editor",
        "HTML Editor",
        "Markdown Editor",
        "Signature",
        "Attach",
        "Attach Image",
    }
)
DECIMAL_FIELDTYPES = frozenset({"Currency", "Float", "Percent"})
TEMPORAL_FIELDTYPES = frozenset({"Date", "Datetime", "Time"})
LAYOUT_FIELDTYPES = frozenset(
    {"Section Break", "Column Break", "Tab Break", "Table", "Table MultiSelect", "Button", "HTML"}
)


def mariadb_inline_bytes(field: dict) -> int:
    """Compute MariaDB utf8mb4 inline row bytes matching `frappe/database/schema.py`."""
    ft = str(field.get("fieldtype") or "")
    if ft in LAYOUT_FIELDTYPES:
        return 0
    if ft in VARCHAR_FIELDTYPES:
        raw_len = int(field.get("length") or 0)
        eff_len = max(64, raw_len) if raw_len > 0 else 140
        byte_len = eff_len * 4
        return byte_len + (1 if byte_len <= 255 else 2)
    if ft in TEXT_FIELDTYPES:
        return 12
    if ft in DECIMAL_FIELDTYPES:
        return 10
    if ft == "Check":
        return 1
    if ft == "Int":
        return 4
    if ft in TEMPORAL_FIELDTYPES:
        return 8
    return 8


def _extract_dict_literal_from_function(py_path: Path, func_name: str) -> dict[str, list[dict]]:
    tree = ast.parse(py_path.read_text(), filename=str(py_path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for stmt in node.body:
                if isinstance(stmt, ast.Return):
                    if isinstance(stmt.value, ast.Dict):
                        return ast.literal_eval(stmt.value)
                    if (
                        isinstance(stmt.value, ast.Call)
                        and stmt.value.args
                        and isinstance(stmt.value.args[0], ast.Dict)
                    ):
                        return ast.literal_eval(stmt.value.args[0])
    raise AssertionError(f"Could not extract dict literal from {py_path}:{func_name}")


def _extract_module_constant(py_path: Path, var_name: str) -> dict[str, list[dict]]:
    tree = ast.parse(py_path.read_text(), filename=str(py_path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"Could not extract {var_name} from {py_path}")


def load_ury_pos_invoice_sources() -> dict[str, dict[str, dict]]:
    fixture_rows = json.loads((APP_ROOT / "fixtures" / "custom_field.json").read_text())
    fixture_pos = {
        row["fieldname"]: row for row in fixture_rows if row.get("dt") == "POS Invoice"
    }
    setup_pos = {
        row["fieldname"]: row
        for row in _extract_dict_literal_from_function(
            APP_ROOT / "setup_customizations.py", "get_custom_fields"
        ).get("POS Invoice", [])
    }
    priv_pos = {
        row["fieldname"]: row
        for row in _extract_dict_literal_from_function(
            APP_ROOT / "ury_pos" / "privileged_fields.py", "get_privileged_action_custom_fields"
        ).get("POS Invoice", [])
    }
    pr6_pos = {
        row["fieldname"]: row
        for row in _extract_module_constant(
            APP_ROOT / "patches" / "v3_46" / "add_pos_fx_ref_offline_fields.py", "CUSTOM_FIELDS"
        ).get("POS Invoice", [])
    }
    pr10_pos = {
        row["fieldname"]: row
        for row in _extract_dict_literal_from_function(
            APP_ROOT / "ury_pos" / "timeclock_tips_fields.py", "get_timeclock_tips_custom_fields"
        ).get("POS Invoice", [])
    }
    return {
        "fixtures/custom_field.json": fixture_pos,
        "setup_customizations.py": setup_pos,
        "ury_pos/privileged_fields.py": priv_pos,
        "patches/v3_46/add_pos_fx_ref_offline_fields.py": pr6_pos,
        "ury_pos/timeclock_tips_fields.py": pr10_pos,
        "patches/v3_46/add_floor_lifecycle_fields.py": {
            row["fieldname"]: row for row in _extract_module_constant(
                APP_ROOT / "patches" / "v3_46" / "add_floor_lifecycle_fields.py", "SPLIT_BALANCE_FIELDS"
            )["POS Invoice"]
        },
    }


class TestPosInvoiceRowSizeBudget(unittest.TestCase):
    def test_pos_invoice_custom_fields_fit_mariadb_row_size_budget(self):
        sources = load_ury_pos_invoice_sources()
        worst_case_bytes: dict[str, int] = {}
        for src_name, fields in sources.items():
            for fn, spec in fields.items():
                col_bytes = mariadb_inline_bytes(spec)
                if col_bytes > worst_case_bytes.get(fn, -1):
                    worst_case_bytes[fn] = col_bytes

        ury_total = sum(worst_case_bytes.values())
        combined_total = BASELINE_ERPNEXT_PLUS_UGANDA_BYTES + ury_total

        self.assertLessEqual(
            ury_total,
            MAX_URY_POS_INVOICE_INLINE_BYTES,
            f"URY POS Invoice custom fields consume {ury_total}B inline "
            f"(combined with ERPNext + uganda_compliance = {combined_total}B vs "
            f"MariaDB {MARIADB_ROW_LIMIT_BYTES}B limit). Bound short varchar custom fields "
            f"(length=64) so fresh installs never hit MariaDB 1118 Row size too large.",
        )
        self.assertLess(
            combined_total,
            MARIADB_ROW_LIMIT_BYTES - 3000,
            f"Combined tabPOS Invoice row size ({combined_total}B) leaves less than 3,000B "
            f"headroom below MariaDB 65,535B limit.",
        )

    def test_bounded_setup_fields_are_not_widened_by_fixtures(self):
        sources = load_ury_pos_invoice_sources()
        setup_pos = sources["setup_customizations.py"]
        fixture_pos = sources["fixtures/custom_field.json"]

        widened = []
        for fn, setup_spec in setup_pos.items():
            if fn not in fixture_pos:
                continue
            setup_bytes = mariadb_inline_bytes(setup_spec)
            fixture_bytes = mariadb_inline_bytes(fixture_pos[fn])
            if setup_bytes < fixture_bytes:
                widened.append(
                    f"{fn}: setup_customizations.py={setup_bytes}B < fixtures/custom_field.json={fixture_bytes}B"
                )
        self.assertEqual(
            widened,
            [],
            "fixtures/custom_field.json widens bounded POS Invoice fields back to varchar(140): "
            + "; ".join(widened),
        )

    def test_ury_last_round_id_accommodates_merge_rounds_composite_uuid(self):
        """G46 Merge Rounds writes `<client_order_id>:<round_id>` (73 chars for two UUIDv4s)
        into `ury_last_round_id`; its configured length must be >= 73 (or unbounded 0)."""
        sources = load_ury_pos_invoice_sources()
        spec = sources["fixtures/custom_field.json"].get("ury_last_round_id") or {}
        raw_len = int(spec.get("length") or 0)
        eff_len = max(64, raw_len) if raw_len > 0 else 140
        composite_uuid_len = 36 + 1 + 36  # "<uuid4>:<uuid4>" == 73
        self.assertGreaterEqual(
            eff_len,
            composite_uuid_len,
            f"POS Invoice-ury_last_round_id length={eff_len} truncates Merge Rounds "
            f"composite round_id ({composite_uuid_len} chars).",
        )


if __name__ == "__main__":
    unittest.main()
