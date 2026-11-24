# ury_kot_routing.py
#
# Read-only routing-resolution module for item-to-production-unit routing
# (V3-51 + POS-PR-11 G27 routing overrides and cycle-safe station failover).
#
# Precedence implemented:
#   0. Active `URY Routing Override` matching branch + item/item_group/room/
#      pos_profile/order_type, ordered by specificity then lowest priority.
#      Conflicting ties at identical specificity and priority fail closed
#      (`ROUTING_AMBIGUOUS`).
#   1. Active `URY Item Production Configuration` exact item mapping
#      (company/branch/department/production_unit/production_policy/service_context
#      scoped, where those fields exist on the doctype).
#   2. Multiple ambiguous exact mappings fail closed (`ROUTING_AMBIGUOUS`).
#   3. No exact mapping -> fall back to branch-scoped
#      `URY Production Unit.item_groups` matching.
#   4. Disabled department blocks routing (`DEPARTMENT_DISABLED`).
#   5. Disabled, manually failed-over, or heartbeat-stale production units
#      automatically traverse `custom_failover_production_unit` with cycle
#      detection (`ROUTING_FAILOVER_CYCLE`, max hops `MAX_FAILOVER_HOPS`) before
#      failing closed with `PRODUCTION_UNIT_DISABLED` when no healthy failover
#      target is configured.
#   6. Missing branch/company scope, or an unresolved production unit for a
#      controlled item, fails closed (`ROUTING_NOT_CONFIGURED`).
#   7. `production_policy == "DIRECT_RETAIL"` items are not forced into
#      production-unit execution unless an override or exact config explicitly
#      routes them — returns an empty list in that case.

import frappe
from frappe.utils import cint, get_datetime

ITEM_PRODUCTION_CONFIG_DOCTYPE = "URY Item Production Configuration"
PRODUCTION_UNIT_DOCTYPE = "URY Production Unit"
PRODUCTION_ITEM_GROUPS_DOCTYPE = "URY Production Item Groups"
PRODUCTION_DEPARTMENT_DOCTYPE = "URY Production Department"
ROUTING_OVERRIDE_DOCTYPE = "URY Routing Override"

DIRECT_RETAIL_POLICY = "DIRECT_RETAIL"
MAX_FAILOVER_HOPS = 5
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 120

# Stable reason codes. Treat these strings as a contract for callers.
ROUTING_AMBIGUOUS = "ROUTING_AMBIGUOUS"
ROUTING_NOT_CONFIGURED = "ROUTING_NOT_CONFIGURED"
DEPARTMENT_DISABLED = "DEPARTMENT_DISABLED"
PRODUCTION_UNIT_DISABLED = "PRODUCTION_UNIT_DISABLED"
ROUTING_FAILOVER_CYCLE = "ROUTING_FAILOVER_CYCLE"
ROUTING_FAILOVER_EXHAUSTED = "ROUTING_FAILOVER_EXHAUSTED"
ROUTING_FAILOVER_BRANCH_MISMATCH = "ROUTING_FAILOVER_BRANCH_MISMATCH"


class RoutingError(Exception):
    """Raised whenever routing resolution must fail closed.

    `reason_code` is one of the stable ROUTING_* / *_DISABLED constants above.
    Callers should branch on `reason_code`, not on the message text.
    """

    def __init__(self, reason_code, message=None):
        self.reason_code = reason_code
        super().__init__(message or reason_code)


def resolve_production_units(
    item_code,
    company,
    branch,
    department=None,
    production_policy=None,
    service_context=None,
    pos_profile=None,
    order_type=None,
    room=None,
):
    """Resolve the Production Unit(s) an item should route to.

    Read-only. Returns a list of healthy `URY Production Unit` names, or an
    empty list for direct-retail items without explicit routing configuration.
    """
    if not item_code:
        raise RoutingError(ROUTING_NOT_CONFIGURED, "item_code is required to resolve routing")
    if not company or not branch:
        raise RoutingError(
            ROUTING_NOT_CONFIGURED, "company and branch scope are required to resolve routing"
        )

    override_unit = _resolve_routing_override(
        item_code=item_code,
        branch=branch,
        pos_profile=pos_profile,
        order_type=order_type,
        room=room,
    )
    if override_unit:
        return [_resolve_unit_with_failover(override_unit, branch=branch)]

    exact_matches = _get_exact_mappings(
        item_code=item_code,
        company=company,
        branch=branch,
        department=department,
        production_policy=production_policy,
        service_context=service_context,
    )

    if exact_matches:
        if len(exact_matches) > 1:
            raise RoutingError(
                ROUTING_AMBIGUOUS,
                f"Multiple active production configuration records match "
                f"item {item_code} for branch {branch}: "
                f"{[m['name'] for m in exact_matches]}",
            )
        return _resolve_from_exact_match(
            exact_matches[0], production_policy=production_policy, branch=branch
        )

    # No exact mapping. Direct-retail items are not forced into production-unit
    # execution unless an explicit override or config said otherwise.
    if production_policy == DIRECT_RETAIL_POLICY:
        return []

    fallback_units = _legacy_item_group_fallback(item_code, branch)
    if not fallback_units:
        raise RoutingError(
            ROUTING_NOT_CONFIGURED,
            f"No exact mapping and no Item Group match found for item "
            f"{item_code} in branch {branch}; routing cannot be determined for "
            f"this controlled item.",
        )

    resolved = []
    for unit in fallback_units:
        healthy = _resolve_unit_with_failover(unit, branch=branch)
        if healthy not in resolved:
            resolved.append(healthy)
    return resolved


def _resolve_routing_override(item_code, branch, pos_profile=None, order_type=None, room=None):
    """Evaluate active `URY Routing Override` rules for the branch.

    Defensive against unmigrated schemas and legacy unit test stubs: returns None
    without querying when the doctype or its `target_production_unit` field is absent.
    """
    if not frappe.db.exists("DocType", ROUTING_OVERRIDE_DOCTYPE):
        return None

    fieldnames = _doctype_fieldnames(ROUTING_OVERRIDE_DOCTYPE)
    if "target_production_unit" not in fieldnames:
        return None

    filters = {"branch": branch}
    if "active" in fieldnames:
        filters["active"] = 1

    query_fields = [
        f
        for f in (
            "name",
            "priority",
            "pos_profile",
            "room",
            "order_type",
            "item_group",
            "item",
            "target_production_unit",
        )
        if f in fieldnames
    ]
    rows = frappe.get_all(ROUTING_OVERRIDE_DOCTYPE, filters=filters, fields=query_fields)
    if not rows:
        return None

    item_group = None
    item_group_loaded = False
    matches = []

    for row in rows:
        target = row.get("target_production_unit")
        if not target:
            continue

        rule_item = row.get("item") or None
        rule_group = row.get("item_group") or None
        rule_room = row.get("room") or None
        rule_profile = row.get("pos_profile") or None
        rule_order_type = row.get("order_type") or None

        if not any((rule_item, rule_group, rule_room, rule_profile, rule_order_type)):
            continue

        if rule_item and rule_item != item_code:
            continue

        if rule_group:
            if not item_group_loaded:
                item_group = frappe.db.get_value("Item", item_code, "item_group")
                item_group_loaded = True
            if rule_group != item_group:
                continue

        if rule_room and rule_room != room:
            continue
        if rule_profile and rule_profile != pos_profile:
            continue
        if rule_order_type and rule_order_type != order_type:
            continue

        specificity = (
            (100 if rule_item else 0)
            + (50 if rule_group else 0)
            + (20 if rule_room else 0)
            + (10 if rule_profile else 0)
            + (5 if rule_order_type else 0)
        )
        raw_priority = row.get("priority")
        priority = cint(raw_priority) if raw_priority not in (None, "") else 100
        matches.append((specificity, -priority, row))

    if not matches:
        return None

    matches.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    best_specificity, best_neg_priority, best_row = matches[0]
    tied = [
        entry[2]
        for entry in matches
        if entry[0] == best_specificity and entry[1] == best_neg_priority
    ]
    distinct_targets = {row.get("target_production_unit") for row in tied}
    if len(distinct_targets) > 1:
        raise RoutingError(
            ROUTING_AMBIGUOUS,
            f"Multiple routing overrides match item {item_code} in branch {branch} "
            f"with equal specificity and priority: {[r.get('name') for r in tied]}",
        )
    return best_row.get("target_production_unit")


def _resolve_from_exact_match(config_row, production_policy=None, branch=None):
    department = config_row.get("department")
    production_unit = config_row.get("production_unit")
    policy = (config_row.get("production_policy") or production_policy or "").strip().upper()

    # Direct-retail items are not forced into production-unit execution unless
    # an explicit production_unit was configured on the mapping.
    if not production_unit and policy == DIRECT_RETAIL_POLICY:
        return []

    _assert_department_enabled(department)

    if not production_unit:
        raise RoutingError(
            ROUTING_NOT_CONFIGURED,
            f"Production configuration {config_row.get('name')} has no production_unit set",
        )

    resolved_unit = _resolve_unit_with_failover(production_unit, branch=branch)
    return [resolved_unit]


def _get_exact_mappings(item_code, company, branch, department, production_policy, service_context):
    """Query active exact item mappings, scoping by every field that both the
    caller supplied AND actually exists on the doctype."""
    if not frappe.db.exists("DocType", ITEM_PRODUCTION_CONFIG_DOCTYPE):
        return []

    fieldnames = _doctype_fieldnames(ITEM_PRODUCTION_CONFIG_DOCTYPE)

    filters = {"item": item_code, "branch": branch}
    if "active" in fieldnames:
        filters["active"] = 1

    optional_scopes = {
        "company": company,
        "department": department,
        "production_policy": production_policy,
        "service_context": service_context,
    }
    for fieldname, value in optional_scopes.items():
        if value is not None and fieldname in fieldnames:
            filters[fieldname] = value

    fields = ["name", "production_unit", "department"]
    if "production_policy" in fieldnames:
        fields.append("production_policy")

    return frappe.get_all(
        ITEM_PRODUCTION_CONFIG_DOCTYPE,
        filters=filters,
        fields=fields,
    )


def _legacy_item_group_fallback(item_code, branch):
    """Read-only branch-scoped `URY Production Unit.item_groups` matching."""
    productions = frappe.db.get_all(
        PRODUCTION_UNIT_DOCTYPE, filters={"branch": branch}, fields=["name"]
    )
    if not productions:
        return []

    item_group = frappe.db.get_value("Item", item_code, "item_group")
    if not item_group:
        return []

    matched_units = []
    for production in productions:
        production_item_groups_list = frappe.get_all(
            PRODUCTION_ITEM_GROUPS_DOCTYPE,
            fields=["item_group"],
            filters={
                "parent": production.name,
                "parenttype": PRODUCTION_UNIT_DOCTYPE,
            },
            order_by="idx",
        )
        production_item_groups = [
            row.item_group for row in production_item_groups_list
        ]
        if item_group in production_item_groups:
            matched_units.append(production.name)

    return matched_units


def _assert_department_enabled(department):
    if not department:
        return
    if not frappe.db.exists("DocType", PRODUCTION_DEPARTMENT_DOCTYPE):
        return
    fieldnames = _doctype_fieldnames(PRODUCTION_DEPARTMENT_DOCTYPE)
    if "enabled" not in fieldnames:
        return
    enabled = frappe.db.get_value(PRODUCTION_DEPARTMENT_DOCTYPE, department, "enabled")
    if enabled is not None and not enabled:
        raise RoutingError(
            DEPARTMENT_DISABLED, f"Production department {department} is disabled"
        )


def _is_heartbeat_stale(last_heartbeat, timeout_seconds):
    if not last_heartbeat:
        return True
    try:
        timeout = int(timeout_seconds) if timeout_seconds not in (None, "") else DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        timeout = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
    timeout = max(1, timeout)
    try:
        last_dt = get_datetime(last_heartbeat)
        now_dt = get_datetime()
        return (now_dt - last_dt).total_seconds() > timeout
    except Exception:
        return True


def _get_unit_failover_state(production_unit, fieldnames):
    """Read enabled/failover fields defensively for schema revisions & unit mocks."""
    interesting = [
        f
        for f in (
            "enabled",
            "branch",
            "custom_failover_production_unit",
            "custom_backup_production_unit",
            "custom_failover_active",
            "custom_auto_failover_enabled",
            "custom_heartbeat_timeout_seconds",
            "custom_last_heartbeat",
        )
        if f in fieldnames
    ]
    if not interesting:
        return {}

    # Preserve single-field get_value call shape for legacy unit tests that only
    # stub `enabled` on URY Production Unit.
    if interesting == ["enabled"]:
        return {"enabled": frappe.db.get_value(PRODUCTION_UNIT_DOCTYPE, production_unit, "enabled")}

    raw = frappe.db.get_value(
        PRODUCTION_UNIT_DOCTYPE, production_unit, interesting, as_dict=True
    )
    if isinstance(raw, dict):
        return raw

    return {
        field: frappe.db.get_value(PRODUCTION_UNIT_DOCTYPE, production_unit, field)
        for field in interesting
    }


def _resolve_unit_with_failover(production_unit, branch=None):
    """Verify a production unit is healthy or traverse its cycle-safe failover chain."""
    fieldnames = _doctype_fieldnames(PRODUCTION_UNIT_DOCTYPE)
    visited = []
    current = production_unit

    while True:
        if not frappe.db.exists(PRODUCTION_UNIT_DOCTYPE, current):
            raise RoutingError(
                ROUTING_NOT_CONFIGURED,
                f"Configured production station {current} does not exist",
            )

        visited.append(current)
        state = _get_unit_failover_state(current, fieldnames)

        enabled_val = state.get("enabled")
        is_disabled = enabled_val is not None and not cint(enabled_val)
        is_manual_failover = bool(cint(state.get("custom_failover_active")))
        is_heartbeat_offline = bool(cint(state.get("custom_auto_failover_enabled"))) and _is_heartbeat_stale(
            state.get("custom_last_heartbeat"),
            state.get("custom_heartbeat_timeout_seconds"),
        )

        if not (is_disabled or is_manual_failover or is_heartbeat_offline):
            return current

        next_unit = (
            state.get("custom_failover_production_unit")
            or state.get("custom_backup_production_unit")
            or None
        )
        if not next_unit:
            reason = (
                f"Production station {current} is disabled"
                if is_disabled
                else f"Production station {current} is offline or in failover without a failover target"
            )
            raise RoutingError(PRODUCTION_UNIT_DISABLED, reason)

        if next_unit in visited:
            chain = " -> ".join(visited + [next_unit])
            raise RoutingError(
                ROUTING_FAILOVER_CYCLE,
                f"Circular station failover detected: {chain}",
            )

        if len(visited) > MAX_FAILOVER_HOPS:
            chain = " -> ".join(visited + [next_unit])
            raise RoutingError(
                ROUTING_FAILOVER_EXHAUSTED,
                f"Station failover exceeded maximum {MAX_FAILOVER_HOPS} hops: {chain}",
            )

        if branch:
            target_branch = frappe.db.get_value(PRODUCTION_UNIT_DOCTYPE, next_unit, "branch")
            if isinstance(target_branch, str) and target_branch and target_branch != branch:
                raise RoutingError(
                    ROUTING_FAILOVER_BRANCH_MISMATCH,
                    f"Failover station {next_unit} belongs to branch {target_branch}, not {branch}",
                )

        current = next_unit


def validate_failover_chain(unit_name, failover_target, branch=None):
    """Validate that assigning `failover_target` to `unit_name` creates no cycle or cross-branch link."""
    if not failover_target:
        return
    if not unit_name:
        return
    if failover_target == unit_name:
        raise RoutingError(
            ROUTING_FAILOVER_CYCLE,
            f"Circular station failover detected: {unit_name} -> {failover_target}",
        )

    if not frappe.db.exists(PRODUCTION_UNIT_DOCTYPE, failover_target):
        raise RoutingError(
            ROUTING_NOT_CONFIGURED,
            f"Failover station {failover_target} does not exist",
        )

    if branch:
        target_branch = frappe.db.get_value(PRODUCTION_UNIT_DOCTYPE, failover_target, "branch")
        if isinstance(target_branch, str) and target_branch and target_branch != branch:
            raise RoutingError(
                ROUTING_FAILOVER_BRANCH_MISMATCH,
                f"Failover station {failover_target} belongs to branch {target_branch}, not {branch}",
            )

    visited = [unit_name]
    current = failover_target
    fieldnames = _doctype_fieldnames(PRODUCTION_UNIT_DOCTYPE)
    while current:
        if current in visited:
            chain = " -> ".join(visited + [current])
            raise RoutingError(
                ROUTING_FAILOVER_CYCLE,
                f"Circular station failover detected: {chain}",
            )
        if len(visited) > MAX_FAILOVER_HOPS:
            chain = " -> ".join(visited + [current])
            raise RoutingError(
                ROUTING_FAILOVER_EXHAUSTED,
                f"Station failover exceeded maximum {MAX_FAILOVER_HOPS} hops: {chain}",
            )
        visited.append(current)
        state = _get_unit_failover_state(current, fieldnames)
        current = (
            state.get("custom_failover_production_unit")
            or state.get("custom_backup_production_unit")
            or None
        )


def _assert_production_unit_enabled(production_unit):
    _resolve_unit_with_failover(production_unit)


def _doctype_fieldnames(doctype):
    meta = frappe.get_meta(doctype)
    return {df.fieldname for df in meta.fields}
