"""Currency-aware formatting and safe print-asset helpers for URY Jinja print formats."""

import base64
import os

import frappe
from frappe.utils import cint, fmt_money

_MAX_INLINE_LOGO_BYTES = 512 * 1024
_LOGO_MIME_BY_EXT = {
	".png": "image/png",
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".gif": "image/gif",
	".webp": "image/webp",
	".svg": "image/svg+xml",
}


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


def resolve_bill_logo_src(path: str | None) -> str:
	"""Resolve a configured receipt logo into a wkhtmltopdf- and QZ-safe image src.

	Local site images (/files/..., /private/files/..., /assets/...) are inlined as
	``data:<mime>;base64,...`` URIs when present on disk so server PDFs (wkhtmltopdf / CUPS)
	and standalone QZ Tray HTML never depend on an unreachable ``host_name`` HTTP fetch.
	Falls back to the relative path when no local file is present; blocks path traversal
	and non-image / unsafe URL schemes.
	"""
	if not isinstance(path, str):
		return ""
	raw = path.strip()
	if not raw:
		return ""
	lower = raw.lower()
	if lower.startswith("data:image/"):
		return raw
	if lower.startswith(("https://", "http://")):
		return raw
	# Block unsafe schemes (javascript:, file:, vbscript:, etc.)
	scheme_sep = raw.find(":")
	slash_sep = raw.find("/")
	if scheme_sep != -1 and (slash_sep == -1 or scheme_sep < slash_sep):
		return ""

	normalized = raw if raw.startswith("/") else "/" + raw
	parts = [p for p in normalized.split("/") if p]
	if any(p in (".", "..") for p in parts):
		return ""

	clean_path = normalized.split("?", 1)[0].split("#", 1)[0]
	ext = os.path.splitext(clean_path)[1].lower()
	mime = _LOGO_MIME_BY_EXT.get(ext)
	if not mime:
		return ""

	get_site_path = getattr(frappe, "get_site_path", None)
	if callable(get_site_path):
		base_dir = None
		rel_path = None
		if clean_path.startswith("/files/"):
			base_dir = get_site_path("public", "files")
			rel_path = clean_path[len("/files/"):]
		elif clean_path.startswith("/private/files/"):
			base_dir = get_site_path("private", "files")
			rel_path = clean_path[len("/private/files/"):]
		elif clean_path.startswith("/assets/"):
			base_dir = os.path.join(get_site_path(), "..", "assets")
			rel_path = clean_path[len("/assets/"):]

		if isinstance(base_dir, str) and rel_path:
			try:
				real_base = os.path.realpath(base_dir)
				candidate = os.path.realpath(os.path.join(real_base, rel_path))
				if os.path.commonpath([candidate, real_base]) == real_base and os.path.isfile(candidate):
					size = os.path.getsize(candidate)
					if 0 < size <= _MAX_INLINE_LOGO_BYTES:
						with open(candidate, "rb") as fh:
							encoded = base64.b64encode(fh.read()).decode("ascii")
						return f"data:{mime};base64,{encoded}"
			except OSError:
				pass

	return normalized
