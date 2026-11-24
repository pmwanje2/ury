"""Configuration and hospitality-scoped safety helpers for POS Invoice print formats.

Frappe v16's Print Format Builder Beta renders Data/Table/Repeater fields without
HTML escaping (`frappe/templates/print_format/macros/Data.html`), and ERPNext's
legacy Jinja formats (`POS Invoice`, `Return POS Invoice`) also omit `| e`.

We set `Merged POS Invoice Format` as the default `POS Invoice` print format via
Property Setter and migrate hospitality POS Profiles (those linked to a restaurant
or branch), WITHOUT globally disabling standard ERPNext / EFRIS formats for
non-hospitality retail POS Profiles or overwriting operator changes on migrate.
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


def _field_str(obj, fieldname):
	if obj is None:
		return ""
	val = obj.get(fieldname) if isinstance(obj, dict) else getattr(obj, fieldname, None)
	return val.strip() if isinstance(val, str) else ""


def is_print_format_disabled(name):
	"""Return True if ``name`` is explicitly marked disabled=1 in Print Format."""
	if not isinstance(name, str) or not name.strip():
		return False
	clean = name.strip()
	if clean == "Standard":
		return False
	db = _active_db()
	if db is not None:
		val = db.get_value("Print Format", clean, "disabled")
		if isinstance(val, (int, bool)) and cint(val):
			return True
	return False


def is_print_format_disabled_or_unsafe(name):
	"""Return True if ``name`` is an unsafe POS Invoice format or disabled in DB."""
	if not isinstance(name, str) or not name.strip():
		return False
	clean = name.strip()
	if clean == "Standard":
		return False
	if clean in _UNSAFE_SET:
		return True
	return is_print_format_disabled(clean)


def is_hospitality_pos_profile(profile_or_name):
	"""Return True when a POS Profile belongs to a hospitality outlet (has restaurant or branch)."""
	if profile_or_name is None:
		return False
	if isinstance(profile_or_name, str):
		name = profile_or_name.strip()
		if not name:
			return False
		db = _active_db()
		if db is None:
			return False
		row = db.get_value("POS Profile", name, ["restaurant", "branch"], as_dict=True)
		if not isinstance(row, dict):
			return False
		return bool(_field_str(row, "restaurant") or _field_str(row, "branch"))
	return bool(_field_str(profile_or_name, "restaurant") or _field_str(profile_or_name, "branch"))


def is_hospitality_pos_invoice(doc):
	"""Return True when a POS Invoice belongs to a hospitality outlet (restaurant/branch or hospitality POS Profile)."""
	if doc is None:
		return False
	if _field_str(doc, "restaurant") or _field_str(doc, "branch"):
		return True
	pos_profile = _field_str(doc, "pos_profile")
	if pos_profile:
		return is_hospitality_pos_profile(pos_profile)
	return False


def validate_pos_invoice_print_format(doc, print_format_name):
	"""Enforce disabled=1 on all POS Invoices and refuse unsafe formats on hospitality POS Invoices."""
	if not isinstance(print_format_name, str) or not print_format_name.strip():
		return
	clean = print_format_name.strip()
	if clean == "Standard":
		return
	if is_print_format_disabled(clean):
		frappe.throw(
			_("Print Format {0} is disabled").format(clean),
			frappe.DoesNotExistError,
		)
	if is_hospitality_pos_invoice(doc) and is_unsafe_pos_invoice_format(clean):
		frappe.throw(
			_("Print Format {0} is not allowed for hospitality POS invoices. Use {1}.").format(
				clean, GUEST_CHECK_PRINT_FORMAT
			),
			frappe.DoesNotExistError,
		)


def resolve_safe_pos_print_format(configured=None, *, default_format=None):
	"""Return a safe POS Invoice print format name, defaulting to the guest-check format."""
	for candidate in (configured, default_format):
		if isinstance(candidate, str) and candidate.strip():
			clean = candidate.strip()
			if not is_print_format_disabled_or_unsafe(clean):
				return clean
	return GUEST_CHECK_PRINT_FORMAT


def configure_pos_invoice_print_formats():
	"""Set POS Invoice default_print_format via Property Setter and migrate blank/unsafe
	hospitality POS Profile print_format values.

	Does NOT globally disable standard ERPNext / EFRIS Print Formats and does NOT
	rewrite non-hospitality retail POS Profiles or safe operator-configured formats.
	"""
	# 1. Ensure POS Invoice default_print_format points to a safe format when unset, unsafe, or disabled.
	current_default = frappe.db.get_value(
		"Property Setter",
		{"doc_type": "POS Invoice", "property": "default_print_format"},
		"value",
	)
	should_set_default = (
		not current_default
		or is_unsafe_pos_invoice_format(current_default)
		or is_print_format_disabled(current_default)
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

	# 2. Migrate ONLY hospitality POS Profiles (restaurant or branch set) with blank/unsafe/disabled format.
	if frappe.db.exists("Print Format", GUEST_CHECK_PRINT_FORMAT):
		profiles = frappe.get_all("POS Profile", fields=["name", "print_format", "restaurant", "branch"]) or []
		for prof in profiles:
			if not is_hospitality_pos_profile(prof):
				continue
			pf = _field_str(prof, "print_format")
			prof_name = _field_str(prof, "name")
			if not prof_name:
				continue
			if not pf or is_unsafe_pos_invoice_format(pf) or is_print_format_disabled(pf):
				frappe.db.set_value(
					"POS Profile", prof_name, "print_format", GUEST_CHECK_PRINT_FORMAT, update_modified=False
				)

	frappe.clear_cache(doctype="POS Invoice")
	frappe.clear_cache(doctype="Print Format")
	frappe.clear_cache(doctype="POS Profile")
