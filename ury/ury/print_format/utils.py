"""Currency-aware formatting helpers for URY Jinja print formats."""

import frappe
from frappe.utils import cint, fmt_money


def _currency_format_spec(currency: str | None) -> tuple[int | None, str | None]:
	"""Resolve (precision, number_format) from Currency.number_format or Currency.fraction_units."""
	if not currency or not isinstance(currency, str):
		return None, None
	try:
		info = frappe.db.get_value(
			"Currency",
			currency,
			["number_format", "fraction_units"],
			as_dict=True,
			cache=True,
		)
	except Exception:
		info = None
	if not isinstance(info, dict):
		return None, None

	from frappe.locale import get_number_format
	from frappe.utils.number_format import NumberFormat

	raw_fmt = (info.get("number_format") or "").strip() if isinstance(info.get("number_format"), str) else ""
	if raw_fmt:
		try:
			nf = NumberFormat.from_string(raw_fmt)
			return nf.precision, raw_fmt
		except (KeyError, ValueError):
			pass

	fraction_units = cint(info.get("fraction_units"))
	if fraction_units >= 1:
		precision = max(0, len(str(fraction_units)) - 1)
		try:
			site_nf = get_number_format()
		except Exception:
			site_nf = NumberFormat.from_string("#,###.##")
		if site_nf.precision == precision:
			return precision, site_nf.string
		dot_thousands = site_nf.thousands_separator == "."
		if precision == 0:
			return 0, "#.###" if dot_thousands else "#,###"
		if precision == 2:
			return 2, "#.###,##" if dot_thousands else "#,###.##"
		if precision == 3:
			return 3, "#,###.###"
		return precision, None

	return None, None


def format_bill_money(value, currency: str | None = None) -> str:
	"""Format monetary amounts on receipts and bill copies by Currency.number_format or fraction_units."""
	precision, number_format = _currency_format_spec(currency)
	return fmt_money(value or 0, precision=precision, currency=currency, format=number_format)
