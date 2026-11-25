"""Shared cover limits for order entry and profile bootstrap."""

import math
import re

import frappe

# Legacy clients limited entry to 50. Profiles can override it, including 0 for no cap.
DEFAULT_MAX_COVERS = 50
MAX_STORED_COVERS = 2_147_483_647


def covers_config(profile):
    default = profile.get("custom_max_covers") if profile else None
    rules = (profile.get("custom_order_type_cover_limits") or []) if profile else []
    return {
        "max_covers": DEFAULT_MAX_COVERS if default in (None, "") else int(default),
        "order_type_cover_limits": {
            row.get("order_type"): int(row.get("max_covers") or 0)
            for row in rules
            if row.get("order_type")
        },
    }


def parse_covers(value):
    """Return an exact Int when the submitted or legacy value represents one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if len(text) <= 64 and re.fullmatch(r"\+?[0-9]+(?:\.0+)?", text):
            return int(text.partition(".")[0])
    return None


def normalize_covers(value, order_type, profile=None, *, _frappe=frappe):
    pax = parse_covers(value)
    if pax is None or not 1 <= pax <= MAX_STORED_COVERS:
        _frappe.throw(frappe._("Number of guests must be a positive whole number."))
    config = covers_config(profile)
    limit = config["order_type_cover_limits"].get(order_type, config["max_covers"])
    if limit > 0 and pax > limit:
        _frappe.throw(frappe._("This order type allows up to {0} guests.").format(limit))
    return pax
