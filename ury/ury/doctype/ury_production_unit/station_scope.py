"""Enforce production station ownership through the site's Branch.company link."""

import frappe
from frappe.core.doctype.user_permission import user_permission


def _allowed(permissions, doctype):
    return {row["doc"] for row in permissions.get(doctype, [])
            if row.get("applicable_for") in (None, "", "URY Production Unit")}


def station_company(doc, user=None):
    branch = doc.get("branch")
    if not branch:
        return None
    company = frappe.db.get_value("Branch", branch, "company", cache=True)
    if not company or doc.get("company") not in (None, "", company):
        return None
    permissions = user_permission.get_user_permissions(user)
    branches = _allowed(permissions, "Branch")
    companies = _allowed(permissions, "Company")
    if (branches and branch not in branches) or (companies and company not in companies):
        return None
    return company


def has_station_permission(doc, ptype=None, user=None):
    if ptype == "create" and doc.is_new() and not doc.get("branch"):
        # New stations may still be in the process of receiving a branch.
        return True
    return bool(station_company(doc, user))


def station_query_conditions(user=None, doctype="URY Production Unit"):
    permissions = user_permission.get_user_permissions(user)
    station = "`tabURY Production Unit`"
    clauses = [f"b.name = {station}.branch", "COALESCE(b.company, '') != ''",
               f"(COALESCE({station}.company, '') = '' OR {station}.company = b.company)"]
    for scope, column in (("Branch", "b.name"), ("Company", "b.company")):
        values = _allowed(permissions, scope)
        if values:
            escaped = ", ".join(frappe.db.escape(v) for v in sorted(values))
            clauses.append(f"{column} IN ({escaped})")
    branch_scope = "EXISTS (SELECT 1 FROM `tabBranch` b WHERE " + " AND ".join(clauses) + ")"
    if doctype == "URY KOT":
        # Unassigned tickets keep their normal KOT permissions on unrestricted boards.
        if not (_allowed(permissions, "Branch") or _allowed(permissions, "Company")):
            return ""
        return (f"EXISTS (SELECT 1 FROM {station} WHERE {station}.name = `tabURY KOT`.production"
                f" AND {station}.branch = `tabURY KOT`.branch AND {branch_scope})")
    return branch_scope
