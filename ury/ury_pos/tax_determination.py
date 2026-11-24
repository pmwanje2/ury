"""G42 Order-Type Tax Determination, Native ERPNext Tax Rule Lookup, and Optional EFRIS Pre-Check.

Implements SAP MWST / Oracle Simphony tax determination strictly on native ERPNext
objects (Tax Category, Tax Rule via erpnext.accounts.party.set_taxes, Item Tax Template):

1. ``POS Invoice.custom_tax_exempt == 1`` locks the exempt Tax Category and template.
2. ``Customer.tax_category`` (Diplomat / NGO / Duty-Free) wins next.
3. ``POS Profile.custom_order_type_tax_categories`` matching ``doc.order_type`` wins next.
4. Legacy ``Branch.custom_no_taxes == 1`` applies ONLY for Aggregators when no
   ``custom_order_type_tax_categories`` row is configured for Aggregators.
5. Otherwise falls back to ``POS Profile.tax_category``.
6. Native ``erpnext.accounts.party.set_taxes(...)`` resolves ``taxes_and_charges`` via
   ERPNext ``Tax Rule``, falling back to ``POS Profile.taxes_and_charges`` (then
   ``URY Restaurant.default_tax_template``) only when no Tax Rule matches.
7. Per-item ``Item Tax Template`` (mixed baskets: e.g. 18% VAT + 0% Exempt milk) and
   per-line ``custom_tax_exempt == 1`` are preserved untouched.

Optional country-gated EFRIS readiness check soft-imports ``uganda_compliance`` ONLY when
installed AND the invoice's Company is EFRIS-enabled. Non-Uganda companies remain 100% untouched.
"""

from __future__ import annotations

from datetime import date, datetime

try:
    import frappe
    from frappe import _
except ModuleNotFoundError:  # site-less test double injects frappe onto caller module
    frappe = None  # type: ignore[assignment]

    def _(msg, *args):
        return msg


def cint(value) -> int:
    fn = getattr(getattr(frappe, "utils", None), "cint", None)
    if callable(fn):
        return fn(value)
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def getdate(value=None) -> date:
    fn = getattr(getattr(frappe, "utils", None), "getdate", None)
    if callable(fn):
        return fn(value)
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def validate_order_type_tax_categories(doc, method=None) -> None:
    """Validate POS Profile.custom_order_type_tax_categories: unique order_type, enabled Tax Category."""
    rows = doc.get("custom_order_type_tax_categories") or []
    if not rows:
        return

    seen_order_types: set[str] = set()
    tax_categories: set[str] = set()
    for idx, row in enumerate(rows, start=1):
        ot = (row.get("order_type") or "").strip()
        tc = (row.get("tax_category") or "").strip()
        if not ot:
            frappe.throw(
                _("Order Type Tax Categories row #{0}: Order Type is required.").format(idx),
                frappe.ValidationError,
            )
        if not tc:
            frappe.throw(
                _("Order Type Tax Categories row #{0}: Tax Category is required.").format(idx),
                frappe.ValidationError,
            )
        if ot in seen_order_types:
            frappe.throw(
                _("Duplicate Order Type '{0}' in Order Type Tax Categories.").format(ot),
                frappe.ValidationError,
            )
        seen_order_types.add(ot)
        tax_categories.add(tc)

    existing_rows = frappe.get_all(
        "Tax Category",
        filters={"name": ["in", sorted(tax_categories)]},
        fields=["name", "disabled"],
    )
    by_name = {r.get("name"): r for r in (existing_rows or []) if r.get("name")}
    for tc in sorted(tax_categories):
        record = by_name.get(tc)
        if not record:
            frappe.throw(
                _("Tax Category '{0}' does not exist.").format(tc),
                frappe.ValidationError,
            )
        if cint(record.get("disabled")) == 1:
            frappe.throw(
                _("Tax Category '{0}' is disabled. Choose an enabled Tax Category.").format(tc),
                frappe.ValidationError,
            )


def _resolve_pos_profile_doc(profile):
    """Accept either a POS Profile doc/dict or a POS Profile name string safely."""
    if isinstance(profile, str):
        name = profile.strip()
        if not name:
            return None
        try:
            return frappe.get_cached_doc("POS Profile", name)
        except Exception:
            try:
                return frappe.get_doc("POS Profile", name)
            except Exception:
                return None
    return profile


def get_order_type_tax_config(profile) -> dict:
    """Serialize order-type tax mappings for getPosProfile / getPosProfileFull."""
    profile = _resolve_pos_profile_doc(profile)
    if not profile or not hasattr(profile, "get"):
        return {"tax_category": None, "order_type_tax_categories": {}}
    mapping = {}
    for row in profile.get("custom_order_type_tax_categories") or []:
        ot = (row.get("order_type") or "").strip()
        tc = (row.get("tax_category") or "").strip()
        if ot and tc:
            mapping[ot] = tc
    return {
        "tax_category": profile.get("tax_category") or None,
        "order_type_tax_categories": mapping,
    }


def _resolve_tax_template_via_erpnext(
    *,
    customer: str | None,
    posting_date,
    company: str | None,
    customer_group: str | None = None,
    tax_category: str | None = None,
) -> str | None:
    if not company:
        return None
    try:
        from erpnext.accounts.party import set_taxes
    except (ImportError, ModuleNotFoundError):
        return None

    return set_taxes(
        customer,
        "Customer",
        posting_date,
        company,
        customer_group=customer_group,
        tax_category=tax_category or None,
    )


def _clean_str(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def apply_order_type_tax(
    invoice,
    pos_profile,
    customer_doc=None,
    *,
    posting_date=None,
    reset_non_exempt_item_taxes: bool = False,
) -> dict:
    """Apply SAP MWST / Oracle Simphony tax determination onto ``invoice`` in place.

    Never writes to the database; safe for both live saves and stateless ``preview_order_totals``.
    """
    # Step 1: Manager PIN tax-exempt lock is sacred and never overwritten.
    if cint(invoice.get("custom_tax_exempt")) == 1:
        return {
            "locked": True,
            "source": "tax_exempt_lock",
            "tax_category": _clean_str(invoice.get("tax_category")),
            "taxes_and_charges": _clean_str(invoice.get("taxes_and_charges")),
        }

    order_type = _clean_str(invoice.get("order_type"))
    order_type_map = {}
    raw_rows = pos_profile.get("custom_order_type_tax_categories") if hasattr(pos_profile, "get") else None
    if isinstance(raw_rows, (list, tuple)):
        for row in raw_rows:
            ot = _clean_str(row.get("order_type") if hasattr(row, "get") else getattr(row, "order_type", None))
            tc = _clean_str(row.get("tax_category") if hasattr(row, "get") else getattr(row, "tax_category", None))
            if ot and tc:
                order_type_map[ot] = tc

    customer_name = _clean_str(invoice.get("customer")) or None
    customer_tax_cat = None
    customer_group = None
    if customer_doc is not None:
        customer_tax_cat = _clean_str(customer_doc.get("tax_category") if hasattr(customer_doc, "get") else getattr(customer_doc, "tax_category", None)) or None
        raw_cg = customer_doc.get("customer_group") if hasattr(customer_doc, "get") else getattr(customer_doc, "customer_group", None)
        customer_group = _clean_str(raw_cg) or None
    elif customer_name:
        cust_vals = frappe.db.get_value(
            "Customer", customer_name, ["tax_category", "customer_group"], as_dict=True
        )
        if isinstance(cust_vals, dict):
            customer_tax_cat = _clean_str(cust_vals.get("tax_category")) or None
            customer_group = _clean_str(cust_vals.get("customer_group")) or None
        elif isinstance(cust_vals, (list, tuple)) and len(cust_vals) >= 1:
            customer_tax_cat = _clean_str(cust_vals[0]) or None
            customer_group = _clean_str(cust_vals[1]) or None if len(cust_vals) > 1 else None
        elif isinstance(cust_vals, str) and cust_vals.strip():
            customer_tax_cat = cust_vals.strip()

    mapped_order_type_cat = order_type_map.get(order_type) if order_type else None

    # Precedence Steps 2 -> 3 -> 6 (legacy Aggregators no_taxes fallback) -> 4
    if customer_tax_cat:
        effective_tax_cat = customer_tax_cat
        source = "customer"
    elif mapped_order_type_cat:
        effective_tax_cat = mapped_order_type_cat
        source = "order_type"
    else:
        if order_type == "Aggregators":
            branch = _clean_str(invoice.get("branch")) or _clean_str(pos_profile.get("branch") if hasattr(pos_profile, "get") else None)
            legacy_no_taxes = (
                cint(frappe.db.get_value("Branch", branch, "custom_no_taxes")) if branch else 0
            )
            if legacy_no_taxes == 1:
                invoice.tax_category = ""
                invoice.taxes_and_charges = ""
                if callable(getattr(invoice, "set", None)):
                    invoice.set("taxes", [])
                else:
                    invoice.taxes = []
                if getattr(invoice, "flags", None) is not None:
                    invoice.flags.ury_explicit_no_taxes = True
                return {
                    "locked": False,
                    "source": "legacy_no_taxes",
                    "tax_category": "",
                    "taxes_and_charges": "",
                }

        profile_cat = _clean_str(pos_profile.get("tax_category") if hasattr(pos_profile, "get") else None)
        if profile_cat:
            effective_tax_cat = profile_cat
            source = "pos_profile"
        else:
            effective_tax_cat = ""
            source = "default"

    company = _clean_str(invoice.get("company")) or _clean_str(pos_profile.get("company") if hasattr(pos_profile, "get") else None) or None
    eff_date = getdate(posting_date or invoice.get("posting_date"))

    previous_tax_cat = _clean_str(invoice.get("tax_category"))
    previous_template = _clean_str(invoice.get("taxes_and_charges"))

    invoice.tax_category = effective_tax_cat

    # Step 5: Native ERPNext Tax Rule resolution via party.set_taxes(...)
    resolved_template = _clean_str(
        _resolve_tax_template_via_erpnext(
            customer=customer_name,
            posting_date=eff_date,
            company=company,
            customer_group=customer_group,
            tax_category=effective_tax_cat or None,
        )
    ) or None

    # Fallback ONLY when no Tax Rule matches. When no Tax Category rule applies (source == "default"),
    # preserve an already-resolved invoice template before falling back to profile / restaurant defaults.
    if not resolved_template:
        if source == "default" and previous_template:
            resolved_template = previous_template
        else:
            resolved_template = _clean_str(pos_profile.get("taxes_and_charges") if hasattr(pos_profile, "get") else None) or None
    if not resolved_template and _clean_str(invoice.get("restaurant")):
        resolved_template = _clean_str(
            frappe.db.get_value("URY Restaurant", invoice.get("restaurant"), "default_tax_template")
        ) or None

    invoice.taxes_and_charges = resolved_template or ""

    template_changed = (resolved_template or "") != previous_template
    existing_taxes = invoice.get("taxes") or []
    if template_changed or not existing_taxes or reset_non_exempt_item_taxes:
        if callable(getattr(invoice, "set", None)):
            invoice.set("taxes", [])
        else:
            invoice.taxes = []
        if resolved_template:
            if getattr(invoice, "flags", None) is not None:
                invoice.flags.ury_explicit_no_taxes = False
            append_fn = getattr(invoice, "append_taxes_from_master", None)
            if callable(append_fn):
                append_fn()
        else:
            if getattr(invoice, "flags", None) is not None:
                invoice.flags.ury_explicit_no_taxes = True

    # Step 7: Preserve native per-item Item Tax Template on mixed baskets (e.g. 18% VAT burger +
    # 0% Exempt milk on the same check). However, when a customer-level Tax Category/Rule
    # (e.g. Diplomat / NGO / Zero Exempt Guest) or a 0%/exempt header tax template governs the
    # check, do NOT fall back to a blank-category standard-rate Item Tax row (such as 18% VAT);
    # only honor an exact tax_category match and clear stale non-exempt item tax templates.
    is_zero_or_exempt = _is_zero_rate_tax_template(resolved_template, invoice)
    allow_blank_fallback = (source != "customer") and (not is_zero_or_exempt)
    should_reevaluate_items = (
        reset_non_exempt_item_taxes
        or (not allow_blank_fallback)
        or (bool(previous_template) and template_changed)
        or (bool(previous_tax_cat) and effective_tax_cat != previous_tax_cat)
    )
    for row in invoice.get("items") or []:
        if cint(row.get("custom_tax_exempt")) == 1:
            continue
        item_code = _clean_str(row.get("item_code"))
        if not item_code:
            continue
        if should_reevaluate_items or not _clean_str(row.get("item_tax_template")):
            matched_item_tpl = _resolve_item_tax_template_for_category(
                item_code,
                effective_tax_cat,
                allow_blank_fallback=allow_blank_fallback,
            )
            if should_reevaluate_items:
                row.item_tax_template = matched_item_tpl or ""
                row.item_tax_rate = "{}"
            elif matched_item_tpl:
                row.item_tax_template = matched_item_tpl
                if not _clean_str(row.get("item_tax_rate")) or _clean_str(row.get("item_tax_rate")) == "{}":
                    row.item_tax_rate = "{}"

    return {
        "locked": False,
        "source": source,
        "tax_category": effective_tax_cat,
        "taxes_and_charges": resolved_template or "",
    }


def _is_zero_rate_tax_template(template_name: str | None, invoice=None) -> bool:
    """Return True when ``template_name`` is empty or every tax row in the template has 0 rate/amount."""
    if not template_name:
        return True
    rows = None
    try:
        fetched = frappe.get_all(
            "Sales Taxes and Charges",
            filters={"parent": template_name},
            fields=["rate", "tax_amount", "charge_type"],
        )
        if isinstance(fetched, (list, tuple)) and fetched:
            rows = fetched
    except Exception:
        rows = None
    if not rows and invoice is not None:
        inv_taxes = invoice.get("taxes") if hasattr(invoice, "get") else getattr(invoice, "taxes", None)
        if isinstance(inv_taxes, (list, tuple)) and inv_taxes:
            rows = inv_taxes
        else:
            tpl_map = getattr(invoice, "_tax_templates", None)
            if isinstance(tpl_map, dict) and tpl_map.get(template_name):
                rows = tpl_map[template_name]
    if not rows:
        return False
    for r in rows:
        rate = float(r.get("rate") or 0) if hasattr(r, "get") else float(getattr(r, "rate", 0) or 0)
        amt = float(r.get("tax_amount") or 0) if hasattr(r, "get") else float(getattr(r, "tax_amount", 0) or 0)
        if abs(rate) > 1e-9 or abs(amt) > 1e-9:
            return False
    return True


def _resolve_item_tax_template_for_category(
    item_code: str | None,
    tax_category: str | None,
    *,
    allow_blank_fallback: bool = True,
) -> str | None:
    """Resolve an item's native ERPNext Item Tax child row matching tax_category (or blank category when allowed)."""
    if not item_code:
        return None
    try:
        rows = frappe.get_all(
            "Item Tax",
            filters={"parent": item_code, "parenttype": "Item"},
            fields=["item_tax_template", "tax_category"],
            order_by="idx asc",
        )
    except Exception:
        return None
    if not isinstance(rows, (list, tuple)) or not rows:
        return None

    blank_fallback = None
    for row in rows:
        row_cat = (row.get("tax_category") or "").strip()
        tpl = (row.get("item_tax_template") or "").strip()
        if not tpl:
            continue
        if tax_category and row_cat == tax_category:
            return tpl
        if not row_cat and blank_fallback is None:
            blank_fallback = tpl
    return blank_fallback if allow_blank_fallback else None


def summarize_tax_for_order_type(
    pos_profile,
    order_type: str | None,
    customer: str | None = None,
    posting_date=None,
) -> dict:
    """Read-only summary of active Tax Category and Tax Template for POS Header status strip."""
    pos_profile = _resolve_pos_profile_doc(pos_profile)
    if not pos_profile or not hasattr(pos_profile, "get"):
        return {"tax_category": None, "taxes_and_charges": None, "label": None}

    order_type_map = get_order_type_tax_config(pos_profile).get("order_type_tax_categories", {})
    customer_tax_cat = None
    customer_group = None
    if customer:
        try:
            vals = frappe.db.get_value(
                "Customer", customer, ["tax_category", "customer_group"], as_dict=True
            )
            if isinstance(vals, dict):
                customer_tax_cat = (vals.get("tax_category") or "").strip() or None
                customer_group = vals.get("customer_group")
        except Exception:
            pass

    ot = (order_type or "").strip()
    effective_cat = customer_tax_cat or order_type_map.get(ot) or (pos_profile.get("tax_category") or None)
    company = pos_profile.get("company")
    eff_date = getdate(posting_date)

    resolved_template = None
    try:
        resolved_template = _resolve_tax_template_via_erpnext(
            customer=customer,
            posting_date=eff_date,
            company=company,
            customer_group=customer_group,
            tax_category=effective_cat,
        )
    except Exception:
        resolved_template = None

    if not resolved_template:
        resolved_template = pos_profile.get("taxes_and_charges") or None

    label = resolved_template or effective_cat or None
    return {
        "tax_category": effective_cat,
        "taxes_and_charges": resolved_template,
        "label": label,
    }


def _is_company_efris_enabled(company: str | None) -> bool:
    if not company:
        return False
    try:
        installed = frappe.get_installed_apps() or []
    except Exception:
        installed = []
    if "uganda_compliance" not in installed:
        return False
    try:
        import uganda_compliance  # noqa: F401
    except ImportError:
        return False

    try:
        from uganda_compliance.efris.doctype.e_invoicing_settings.e_invoicing_settings import (
            get_e_company_settings,
        )

        settings = get_e_company_settings(company)
        if not settings:
            return False
        if isinstance(settings, dict) and "enabled" in settings:
            if cint(settings.get("enabled")) != 1:
                return False
        elif getattr(settings, "enabled", None) is not None:
            if cint(getattr(settings, "enabled")) != 1:
                return False
        return True
    except ImportError:
        pass
    except Exception:
        clear_msg = getattr(frappe, "clear_last_message", None)
        if callable(clear_msg):
            try:
                clear_msg()
            except Exception:
                pass
        return False

    try:
        return cint(frappe.db.get_value("Company", company, "efris_company")) == 1
    except Exception:
        return False


def validate_efris_readiness(invoice, throw: bool = False) -> list[dict]:
    """Optional country-gated EFRIS readiness pre-check.

    Strictly a no-op when ``uganda_compliance`` is not installed or the invoice's
    Company is not EFRIS-enabled. When active, delegates to ``uganda_compliance``'s
    helper if exposed, otherwise checks ``Item.efris_commodity_code``,
    ``UOM.efris_uom_code``, and EFRIS tax category mapping and returns structured
    ``EFRIS-ITEM-001`` warnings (or throws when ``throw=True``).
    """
    company = invoice.get("company") if hasattr(invoice, "get") else getattr(invoice, "company", None)
    if not _is_company_efris_enabled(company):
        return []

    try:
        import uganda_compliance
    except ImportError:
        return []

    # Delegate if uganda_compliance exposes a native readiness helper
    native_helper = getattr(uganda_compliance, "validate_efris_readiness", None)
    if callable(native_helper):
        return native_helper(invoice, throw=throw) or []

    warnings: list[dict] = []
    seen_codes: set[str] = set()
    for row in invoice.get("items") or []:
        if cint(row.get("is_disposable")) == 1:
            continue
        item_code = row.get("item_code")
        if not item_code or item_code in seen_codes:
            continue
        seen_codes.add(item_code)

        missing: list[str] = []
        item_meta = frappe.db.get_value(
            "Item", item_code, ["efris_commodity_code", "stock_uom"], as_dict=True
        )
        if isinstance(item_meta, dict):
            commodity_code = (item_meta.get("efris_commodity_code") or "").strip()
            stock_uom = item_meta.get("stock_uom")
        elif isinstance(item_meta, (list, tuple)):
            commodity_code = (item_meta[0] or "").strip() if len(item_meta) > 0 else ""
            stock_uom = item_meta[1] if len(item_meta) > 1 else None
        else:
            commodity_code = str(item_meta or "").strip()
            stock_uom = None

        if not commodity_code:
            missing.append("EFRIS Commodity Code")

        uom = row.get("uom") or stock_uom
        if uom:
            uom_code = frappe.db.get_value("UOM", uom, "efris_uom_code")
            if not (str(uom_code or "").strip()):
                missing.append(f"EFRIS UOM Code ({uom})")

        # Check Item Tax Template & EFRIS tax category (uganda_compliance EInvoice.fetch_items_from_invoice
        # throws when item_doc.taxes is empty or its Item Tax Template lacks efris_e_tax_category).
        item_tax_template = (row.get("item_tax_template") or "").strip()
        if not item_tax_template:
            item_tax_template = (
                _resolve_item_tax_template_for_category(
                    item_code, invoice.get("tax_category"), allow_blank_fallback=True
                )
                or ""
            )
        if not item_tax_template:
            missing.append("Item Tax Template")
        else:
            try:
                tpl_has_efris = frappe.get_all(
                    "Item Tax Template Detail",
                    filters={"parent": item_tax_template},
                    fields=["efris_e_tax_category"],
                )
                if (
                    not isinstance(tpl_has_efris, (list, tuple))
                    or not tpl_has_efris
                    or not any((d.get("efris_e_tax_category") or "").strip() for d in tpl_has_efris)
                ):
                    missing.append(f"EFRIS Tax Category ({item_tax_template})")
            except Exception:
                pass

        if missing:
            item_name = row.get("item_name") or item_code
            warnings.append(
                {
                    "code": "EFRIS-ITEM-001",
                    "item_code": item_code,
                    "item_name": item_name,
                    "missing": missing,
                    "message": _("EFRIS-ITEM-001: Item {0} is missing {1}.").format(
                        item_code, ", ".join(missing)
                    ),
                }
            )

    if warnings and throw:
        frappe.throw("\n".join(w["message"] for w in warnings), frappe.ValidationError)

    return warnings
