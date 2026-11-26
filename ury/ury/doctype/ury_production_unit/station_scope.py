"""Enforce production station ownership through the site's Branch.company link."""

from functools import partial

import frappe
from frappe.core.doctype.user_permission import user_permission


def _allowed(permissions, doctype):
    return {row["doc"] for row in permissions.get(doctype, [])
            if row.get("applicable_for") in (None, "", "URY Production Unit")}


def _transaction_scope(user):
    """Read station restrictions without publishing pending User Permission changes to Redis."""
    scope = {}
    if user in ("Administrator", "Guest"):
        return scope
    rules = frappe.get_all("User Permission", filters={"user": user, "allow": ["in", ["Company", "Branch"]]},
                           fields=["allow", "for_value", "applicable_for", "hide_descendants"])
    for rule in rules:
        values = [rule.for_value]
        if not rule.hide_descendants and frappe.get_meta(rule.allow).is_nested_set():
            values.extend(frappe.db.get_descendants(rule.allow, rule.for_value))
        scope.setdefault(rule.allow, []).extend({"doc": value, "applicable_for": rule.applicable_for} for value in values)
    return scope


def station_company(doc, user=None, *, for_update=False, permissions=None):
    branch = doc.get("branch")
    if not branch:
        return None
    company = frappe.db.get_value("Branch", branch, "company", for_update=for_update)
    if not company or doc.get("company") not in (None, "", company):
        return None
    if permissions is None:
        permissions = user_permission.get_user_permissions(user)
    branches = _allowed(permissions, "Branch")
    companies = _allowed(permissions, "Company")
    if (branches and branch not in branches) or (companies and company not in companies):
        return None
    return company


def has_station_permission(doc, ptype=None, user=None):
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


def prune_station_shares(stations=None, user=None):
    """Frappe adds shared names after list restrictions. Keep shares in scope at rest."""
    filters = {"share_doctype": "URY Production Unit"}
    if stations is not None:
        if not stations:
            return
        filters["share_name"] = ["in", stations]
    if user:
        filters["user"] = user
    shares = frappe.qb.get_query("DocShare", filters=filters,
                                 fields=["name", "share_name", "everyone", "user"], for_update=True).run(as_dict=True)
    scopes = {}
    for share in shares:
        unit = frappe.db.get_value("URY Production Unit", share.share_name, ["company", "branch"], as_dict=True, for_update=True)
        if share.user and share.user not in scopes:
            scopes[share.user] = _transaction_scope(share.user)
        if share.everyone or not share.user or not unit or not station_company(unit, permissions=scopes[share.user]):
            frappe.db.delete("DocShare", {"name": share.name})


def branch_owner_updated(doc, method=None):
    if doc.has_value_changed("company"):
        prune_station_shares(frappe.get_all("URY Production Unit", filters={"branch": doc.name}, pluck="name"))


def user_scope_updated(doc, method=None):
    rules = (doc.get_doc_before_save(), doc)
    users = {rule.user for rule in rules if rule and rule.allow in ("Company", "Branch")}
    for user in users:
        # Removing one permission can narrow a union of allowed companies.
        invalidate = partial(frappe.cache.hdel, "user_permissions", user)
        frappe.db.after_commit.add(invalidate)
        frappe.db.after_rollback.add(invalidate)
        prune_station_shares(user=user)
