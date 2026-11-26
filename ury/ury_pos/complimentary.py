"""Complimentary line attribution and room-charge eligibility on split checks."""

from frappe.utils import cint, flt


def fully_comped_lines(items):
    """Return the approved lines only when no positive, billable item remains."""
    comps = []
    for row in items:
        if flt(row.get("qty")) <= 0 or cint(row.get("is_disposable")):
            continue
        if cint(row.get("custom_is_complimentary")):
            comps.append(row)
        elif flt(row.get("rate")) > 0:
            return []
    return comps


def check_is_comped_for_room_charge(doc):
    if cint(doc.get("custom_is_complimentary")):
        return True
    # Older split children can have the approval exclusively on their lines.
    return bool((doc.get("custom_split_group") or doc.get("custom_split_from"))
                and fully_comped_lines(doc.get("items") or ()))


def set_split_comp_headers(source, child):
    """A header comp covers both halves. Item comps follow only their own lines."""
    fields = ("custom_comp_type", "custom_comp_reason", "custom_comp_approver")
    whole_check = cint(source.get("custom_is_complimentary")) and flt(source.get("additional_discount_percentage")) == 100
    inherited = {field: source.get(field) for field in fields}
    for check in (source, child):
        comps = fully_comped_lines(check.items)
        check.custom_is_complimentary = int(bool(whole_check or comps))
        for field in fields:
            # When approvals differ the complete audit remains on the lines.
            value = None
            if whole_check:
                value = inherited[field]
            elif check.custom_is_complimentary:
                values = {row.get(field) for row in comps}
                if len(values) == 1:
                    value = values.pop()
            check.set(field, value)
