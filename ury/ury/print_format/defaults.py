"""Configuration helpers for POS Invoice guest-check default print format and
disabling XSS-vulnerable standard print formats without modifying ERPNext.

Frappe v16's Print Format Builder Beta renders Data/Table/Repeater fields without
HTML escaping (`frappe/templates/print_format/macros/Data.html`), and ERPNext's
legacy Jinja formats (`POS Invoice`, `Return POS Invoice`) also omit `| e`.
Disabling those records via `Print Format.disabled = 1` persists across
`bench migrate` (`frappe/modules/import_file.py` `ignore_values`) and removes
them from Desk's format picker (`frappe/desk/form/meta.py`).
"""

import frappe
from frappe import _
from frappe.utils import cint

GUEST_CHECK_PRINT_FORMAT = "Merged POS Invoice Format"

UNSAFE_POS_INVOICE_PRINT_FORMATS = (
	"POS Invoice Classic",
	"POS Invoice Bordered",
	"POS Invoice Modern",
	"POS Invoice Modern with Images",
	"POS Invoice",
	"Return POS Invoice",
	"EFRIS POS Receipt 80mm",
	"POS EFRIS Invoice",
)

_UNSAFE_SET = frozenset(UNSAFE_POS_INVOICE_PRINT_FORMATS)


def is_unsafe_pos_invoice_format(name):
	"""Return True if ``name`` is a known XSS-vulnerable standard POS Invoice format."""
	if not isinstance(name, str):
		return False
	return name.strip() in _UNSAFE_SET


def _active_db():
	if hasattr(frappe, "local"):
		return getattr(frappe.local, "db", None)
	return getattr(frappe, "db", None)


def is_print_format_disabled_or_unsafe(name):
	"""Return True if ``name`` is an unsafe POS Invoice format or disabled in DB."""
	if not isinstance(name, str) or not name.strip():
		return False
	clean = name.strip()
	if clean == "Standard":
		return False
	if clean in _UNSAFE_SET:
		return True
	db = _active_db()
	if db is not None:
		val = db.get_value("Print Format", clean, "disabled")
		if isinstance(val, (int, bool)) and cint(val):
			return True
	return False


def resolve_safe_pos_print_format(configured=None, *, default_format=None):
	"""Return a safe POS Invoice print format name, defaulting to the guest-check format."""
	for candidate in (configured, default_format):
		if isinstance(candidate, str) and candidate.strip():
			clean = candidate.strip()
			if not is_print_format_disabled_or_unsafe(clean):
				return clean
	return GUEST_CHECK_PRINT_FORMAT


def configure_pos_invoice_print_formats():
	"""Disable unsafe standard POS Invoice formats, set the DocType default via
	Property Setter, and migrate blank/unsafe POS Profile print_format values.

	Safe custom formats explicitly configured by an operator (such as
	``Restaurant Receipt 80mm``) are preserved untouched.
	"""
	# 1. Disable all known XSS-vulnerable standard POS Invoice formats.
	for fmt_name in UNSAFE_POS_INVOICE_PRINT_FORMATS:
		if frappe.db.exists("Print Format", fmt_name):
			frappe.db.set_value("Print Format", fmt_name, "disabled", 1, update_modified=False)

	# 2. Ensure POS Invoice default_print_format points to a safe format.
	current_default = frappe.db.get_value(
		"Property Setter",
		{"doc_type": "POS Invoice", "property": "default_print_format"},
		"value",
	)
	should_set_default = (
		not current_default
		or is_unsafe_pos_invoice_format(current_default)
		or bool(cint(frappe.db.get_value("Print Format", current_default, "disabled")))
	)
	if should_set_default and frappe.db.exists("Print Format", GUEST_CHECK_PRINT_FORMAT):
		frappe.make_property_setter(
			{
				"doctype": "POS Invoice",
				"doctype_or_field": "DocType",
				"property": "default_print_format",
				"value": GUEST_CHECK_PRINT_FORMAT,
				"property_type": "Data",
			},
			validate_fields_for_doctype=False,
		)

	# 3. Migrate POS Profiles with blank, unsafe, or disabled print_format values.
	if frappe.db.exists("Print Format", GUEST_CHECK_PRINT_FORMAT):
		profiles = frappe.get_all("POS Profile", fields=["name", "print_format"]) or []
		for prof in profiles:
			pf = (getattr(prof, "print_format", None) or (prof.get("print_format") if isinstance(prof, dict) else None) or "").strip()
			prof_name = getattr(prof, "name", None) or (prof.get("name") if isinstance(prof, dict) else None)
			if not prof_name:
				continue
			if not pf or is_unsafe_pos_invoice_format(pf):
				frappe.db.set_value("POS Profile", prof_name, "print_format", GUEST_CHECK_PRINT_FORMAT, update_modified=False)
			elif cint(frappe.db.get_value("Print Format", pf, "disabled")):
				frappe.db.set_value("POS Profile", prof_name, "print_format", GUEST_CHECK_PRINT_FORMAT, update_modified=False)

	frappe.clear_cache(doctype="POS Invoice")
	frappe.clear_cache(doctype="Print Format")
	frappe.clear_cache(doctype="POS Profile")
