"""Public site identity for URY pages; deliberately not an RPC endpoint."""

import frappe

DEFAULT_SOURCE_URL = "https://github.com/ury-erp/ury"


def get_brand(surface="site"):
    settings = frappe.get_cached_doc("Website Settings")
    brand = {
        "name": settings.app_name or None,
        "logo": settings.app_logo or None,
        "favicon": settings.favicon or None,
    }
    if surface == "staff":
        # A dict hook arrives as {key: [values...]}; any other shape is ignored.
        hooks = frappe.get_hooks("ury_brand")
        if not isinstance(hooks, dict):
            hooks = {}
        for key in brand:
            values = hooks.get(key)
            if isinstance(values, list) and values and values[-1]:
                brand[key] = values[-1]
    return brand if any(brand.values()) else None


def json_for_script(value):
    """Preserve JSON values without allowing an HTML script tag to close."""
    return (
        frappe.as_json(value, indent=None, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def get_brand_context(surface="site"):
    brand = get_brand(surface=surface)
    return {
        "brand": brand,
        "brand_json": json_for_script(brand),
        "brand_source_url": frappe.conf.get("ury_source_url") or DEFAULT_SOURCE_URL,
    }
