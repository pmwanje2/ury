"""Idempotent patch installing G3 modifier and G4 combo custom fields."""

import frappe

from ury.ury_pos.modifier_combo_fields import ensure_modifier_combo_custom_fields


def execute():
	ensure_modifier_combo_custom_fields()
	frappe.clear_cache()
