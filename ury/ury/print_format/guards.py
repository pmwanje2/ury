"""Explicit whitelisted print-endpoint guards for POS Invoice (zero stack-frame walking).

Registered via `override_whitelisted_methods` in `ury/hooks.py`:
- Enforces `Print Format.disabled = 1` across Frappe v16 Print Format Builder Beta formats
- Blocks XSS-vulnerable standard formats on hospitality POS Invoices without globally
  disabling retail POS formats
- Strips untrusted client-supplied `copy_number` / `copy_timestamp` from serialized `doc`
  payloads sent to `get_html_and_style`.
"""

import json

import frappe

from ury.ury.print_format.defaults import validate_pos_invoice_print_format


def _parse_pos_invoice_payload(doc, name=None):
	"""Return (target_doc, sanitized_doc_arg) when the request targets a POS Invoice."""
	if isinstance(doc, str) and isinstance(name, str) and doc == "POS Invoice":
		return frappe.get_lazy_doc(doc, name, check_permission=True), doc

	parsed = None
	was_json_str = False
	if isinstance(doc, str):
		stripped = doc.strip()
		if stripped.startswith("{"):
			try:
				parsed = frappe.parse_json(stripped)
				was_json_str = True
			except Exception:
				return None, doc
	elif isinstance(doc, dict):
		parsed = dict(doc)

	if not isinstance(parsed, dict) or parsed.get("doctype") != "POS Invoice":
		return None, doc

	# Never trust client-supplied copy_number or copy_timestamp in a serialized printview payload.
	parsed.pop("copy_number", None)
	parsed.pop("copy_timestamp", None)
	sanitized = json.dumps(parsed, default=str) if was_json_str else parsed
	return parsed, sanitized


@frappe.whitelist()
def get_html_and_style(
	doc,
	name=None,
	print_format=None,
	no_letterhead=None,
	letterhead=None,
	trigger_print=False,
	style=None,
	settings=None,
):
	from frappe.www.printview import get_html_and_style as _frappe_get_html_and_style

	target_doc, safe_doc = _parse_pos_invoice_payload(doc, name)
	if target_doc is not None:
		form_dict = getattr(frappe, "form_dict", None)
		effective_fmt = print_format or (
			(form_dict.get("format") or form_dict.get("print_format")) if isinstance(form_dict, dict) else None
		)
		validate_pos_invoice_print_format(target_doc, effective_fmt)

	return _frappe_get_html_and_style(
		doc=safe_doc,
		name=name,
		print_format=print_format,
		no_letterhead=no_letterhead,
		letterhead=letterhead,
		trigger_print=trigger_print,
		style=style,
		settings=settings,
	)


@frappe.whitelist(allow_guest=True)
def download_pdf(
	doctype,
	name,
	format=None,
	doc=None,
	no_letterhead=0,
	language=None,
	letterhead=None,
	pdf_generator=None,
	style=None,
	settings=None,
):
	from frappe.utils.print_format import download_pdf as _frappe_download_pdf

	if doctype == "POS Invoice":
		target_doc = doc if doc is not None else frappe.get_doc(doctype, name)
		validate_pos_invoice_print_format(target_doc, format)

	return _frappe_download_pdf(
		doctype=doctype,
		name=name,
		format=format,
		doc=doc,
		no_letterhead=no_letterhead,
		language=language,
		letterhead=letterhead,
		pdf_generator=pdf_generator,
		style=style,
		settings=settings,
	)
