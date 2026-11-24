"""Dedicated custom-field definitions for POS-PR-8 (G3 Modifiers & G4 Combo Meals).

Kept separate from `ury/fixtures/custom_field.json` so migrations stay idempotent and
merge-conflict free with sibling PRs. All labels and descriptions are strictly white-label.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def _invoice_item_modifier_combo_fields():
	return [
		{
			"fieldname": "custom_parent_line_key",
			"fieldtype": "Data",
			"label": "Parent Line Key",
			"description": "Stable line key linking a modifier or combo upcharge row to its parent order line.",
			"insert_after": "reservation_line_key",
			"read_only": 1,
			"hidden": 1,
			"translatable": 0,
		},
		{
			"fieldname": "custom_line_role",
			"fieldtype": "Select",
			"label": "Order Line Role",
			"options": "Standard\nModifier\nCombo Header\nCombo Upcharge",
			"default": "Standard",
			"insert_after": "custom_parent_line_key",
			"read_only": 1,
			"translatable": 0,
		},
		{
			"fieldname": "custom_is_modifier",
			"fieldtype": "Check",
			"label": "Is Modifier",
			"default": "0",
			"insert_after": "custom_line_role",
			"read_only": 1,
		},
		{
			"fieldname": "custom_modifier_prefix",
			"fieldtype": "Data",
			"label": "Modifier Prefix",
			"description": "Selected modifier prefix such as No, Extra, Side, Light, or On Side.",
			"insert_after": "custom_is_modifier",
			"read_only": 1,
			"translatable": 0,
		},
		{
			"fieldname": "custom_modifier_group",
			"fieldtype": "Link",
			"label": "Modifier Group",
			"options": "URY Modifier Group",
			"insert_after": "custom_modifier_prefix",
			"read_only": 1,
		},
		{
			"fieldname": "custom_modifier_option",
			"fieldtype": "Data",
			"label": "Modifier Option",
			"insert_after": "custom_modifier_group",
			"read_only": 1,
			"translatable": 0,
		},
		{
			"fieldname": "custom_modifiers_json",
			"fieldtype": "Small Text",
			"label": "Unpriced Modifiers",
			"description": "Structured preparation modifiers stored on the parent line without adding zero-price invoice rows.",
			"insert_after": "custom_modifier_option",
			"read_only": 1,
			"hidden": 1,
			"translatable": 0,
		},
		{
			"fieldname": "custom_bundle_overrides_json",
			"fieldtype": "Small Text",
			"label": "Combo Choices",
			"description": "Selected combo meal component substitutions applied during kitchen routing and inventory deduction.",
			"insert_after": "custom_modifiers_json",
			"read_only": 1,
			"hidden": 1,
			"translatable": 0,
		},
		{
			"fieldname": "custom_inherit_parent_routing",
			"fieldtype": "Check",
			"label": "Inherit Parent Station Routing",
			"default": "1",
			"insert_after": "custom_bundle_overrides_json",
			"read_only": 1,
		},
		{
			"fieldname": "custom_hide_unpriced_on_receipt",
			"fieldtype": "Check",
			"label": "Suppress Unpriced Modifiers on Receipt",
			"default": "0",
			"insert_after": "custom_inherit_parent_routing",
			"read_only": 1,
		},
	]


def get_modifier_combo_custom_fields():
	"""Return custom field specs for Item, Item Group, POS Invoice Item, and Sales Invoice Item."""
	return {
		"Item": [
			{
				"fieldname": "custom_pos_modifiers_section",
				"fieldtype": "Section Break",
				"label": "Modifiers & Combo Choices",
				"collapsible": 1,
				"insert_after": "description",
			},
			{
				"fieldname": "custom_modifier_groups",
				"fieldtype": "Table",
				"label": "Modifier Groups",
				"options": "URY Item Modifier Group",
				"description": "Condiment and combo choice groups prompted when ordering this item.",
				"insert_after": "custom_pos_modifiers_section",
			},
		],
		"Item Group": [
			{
				"fieldname": "custom_pos_default_modifiers_section",
				"fieldtype": "Section Break",
				"label": "Default Modifiers",
				"collapsible": 1,
				"insert_after": "is_group",
			},
			{
				"fieldname": "custom_default_modifier_groups",
				"fieldtype": "Table",
				"label": "Default Modifier Groups",
				"options": "URY Item Modifier Group",
				"description": "Modifier groups inherited by all items in this item group.",
				"insert_after": "custom_pos_default_modifiers_section",
			},
		],
		"POS Invoice Item": _invoice_item_modifier_combo_fields(),
		"Sales Invoice Item": _invoice_item_modifier_combo_fields(),
	}


def ensure_modifier_combo_custom_fields():
	"""Idempotently create or update all G3/G4 modifier and combo custom fields."""
	fields = get_modifier_combo_custom_fields()
	existing_targets = {
		dt: defs
		for dt, defs in fields.items()
		if frappe.db.exists("DocType", dt)
	}
	if existing_targets:
		create_custom_fields(existing_targets, update=True)
	return existing_targets
