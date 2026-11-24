"""Expose server-authoritative item availability with machine-readable diagnostics.

This module is the concrete implementation of V3-40's availability
projection contract (`tracks/sa-v3_nxt/outputs/V3-40-prep-handoff.md`). It is
a pure, READ-ONLY composition over three already-accepted read-only modules:

- `ury_bom_compiler.compile_bom_vector` / `compile_shared_component_index`
  (V3-41) -- BOM explosion for MADE_TO_ORDER items.
- `ury_inventory_projection.get_allocatable_qty` /
  `project_fg_allocatable` / `project_component_allocatable` (V3-42) --
  ERPNext Bin-derived allocatable stock, net of active URY reservations.
- `ury_reservation_service` (V3-43) -- imported so its doctype
  (`URY Stock Reservation`) exists in this app for `ury_inventory_projection`
  and future reservation-aware callers; this module does not call its
  mutating entry points (`create_reservation`/`release_reservation`/etc.) --
  see "Read-only, not reservation" below.

The staff endpoints `get_item_availability` and `get_menu_availability`
answer "can this item be ordered at this branch right now, and why not" for display
purposes (menus, dashboards, KDS). It never reserves, never mutates Bin/
Stock Ledger Entry/menu `disabled` state, and is not a substitute for the
transaction-time authoritative check + reservation that real order
acceptance must still perform against `ury_reservation_service` directly
(V3-40's "Cache, Reservation, and Reconciliation Invariants": "Cached
availability may drive menus, dashboards, and UX, but order acceptance must
re-check transactionally").

## Stock policy tiers

The catalog gate applies in both tiers. Tier 1 (native) stops there: enabled
items are sellable with no capacity limit. Only a branch that enables
reservation control runs the Tier 2 production engine below.

## Read-only, not reservation

This endpoint DISPLAYS availability. It does not create, release, fulfil, or
cancel any `URY Stock Reservation`. Reservation creation is a separate,
explicit call to `ury_reservation_service.create_reservation`, made by order
placement code -- that call site is out of this task's scope (V3-45 wires
POS/Captain/QR consumers; the actual order-acceptance transactional check is
a later fulfilment task per V3-40's contract).

## Reconciliation debt: production_policy/department/production_unit/warehouse
## resolution (V3-13/V3-15 dependency gap)

V3-40 states production_policy and department/production-unit/warehouse
scope come from "item/branch/department/unit production configuration from
V3-13/V3-15". Neither V3-13 nor V3-15 is in this worktree (or, as far as
this task's evidence trail shows, merged anywhere yet), so their concrete
DocType/field schema does not exist to import or query against. Rather than
guess a schema and risk baking in an incompatible shape,
`_resolve_production_config` is implemented defensively:

- It checks whether a `URY Item Production Configuration` DocType/table
  exists at all (`frappe.db.table_exists`, wrapped so a missing table never
  raises an unhandled DB error).
- If the table does not exist, it returns `None` -- callers treat that as
  `CONFIGURATION_ERROR` (fail closed), not a guess.
- If the table exists, it reads a single row scoped by `item_code` +
  `branch` (+ `department` when given) via `frappe.db.get_value(..., as_dict=True)`
  requesting a best-guess field list (`production_policy`, `department`,
  `production_unit`, `warehouse`, `disabled`, `production_unit_disabled`,
  `department_disabled`). Any of those fields the real V3-13/V3-15 schema
  names differently will simply come back as `None` on that dict -- this
  function does not fabricate values for missing fields, so downstream
  reason-code logic keeps failing closed (`MISSING_PRODUCTION_UNIT`/
  `MISSING_DEPARTMENT`/`CONFIGURATION_ERROR`) rather than silently
  misreporting availability.

TODO(V3-13/V3-15 merge): once the real DocType lands, replace the guessed
field list in `_resolve_production_config` with the accepted schema's actual
field names and reconcile this function's return shape with whatever
resolution helper V3-13/V3-15 itself exposes (this function may become a
thin wrapper over that helper instead of querying the table directly).

## Sales Plan entitlement resolution (`_resolve_plan_remaining`)

`URY Sales Plan`/`URY Sales Plan Item` now carry real `committed_qty`/
`fulfilled_qty` columns, transactionally maintained by
`ury_sales_plan_commit.apply_commit_delta` at reservation create/release/
fulfil time (see `ury_reservation_service.py`). `_resolve_plan_remaining`
still guards `frappe.db.table_exists("URY Sales Plan")` (defensive against a
site where the doctype migration hasn't run) and returns `None` when no
matching plan/plan-item row is found -- callers treat `plan_qty is None` as
`NO_ACTIVE_PLAN` (fail closed).

## Server-authoritative branch/company scope

V3-40: "Do not trust client-supplied branch/company scope; derive or verify
it server-side against the session user, POS Profile, document permission,
and the relevant production configuration." There is no live session/POS
Profile context available for wiring in this static-review environment, so
`_verify_branch_scope` is a named, isolated hook: it fails closed (raises)
when `branch`/`company` are missing/empty, and carries a documented TODO for
wiring the real session/POS-Profile/permission check. Every call path in
`get_item_availability` goes through this one function, so that future wiring
requires no change anywhere else in this module.
"""

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import json
import math

import frappe
from frappe import _
from frappe.utils import get_datetime, getdate, now_datetime

from ury.ury.api.ury_bom_compiler import compile_bom_vector
from ury.ury.api.ury_production_context import resolve_production_context
from ury.ury.api.ury_inventory_projection import (
	get_allocatable_qty,
	project_component_allocatable,
	project_fg_allocatable,
)
from ury.ury.api.ury_projection_service import project_mto_capacity
from ury.ury.api.ury_sales_plan_commit import resolve_plan_item_rows
from ury.ury.api.ury_stock_policy import get_branch_stock_policy

# Imported for its side effect of making `URY Stock Reservation` a real,
# loaded doctype module in this app (ury_inventory_projection's reservation
# hook and any future reservation-aware caller depend on it existing) --
# not because this module calls any of its mutating entry points.
import ury.ury.api.ury_reservation_service  # noqa: F401


PRODUCTION_CONFIG_DOCTYPE = "URY Item Production Configuration"
SALES_PLAN_DOCTYPE = "URY Sales Plan"

POLICY_PRE_PRODUCED = "PRE_PRODUCED"
POLICY_MADE_TO_ORDER = "MADE_TO_ORDER"
POLICY_DIRECT_RETAIL = "DIRECT_RETAIL"
POLICY_BUNDLE = "BUNDLE"
MAX_BATCH_CODES = 500
OVERRIDE_DOCTYPE = "URY Item Availability"
OVERRIDE_FIELDS = ["item", "status", "countdown_enabled", "remaining_qty", "daily_limit", "auto_reset_on_open"]
MANAGE_ROLES = frozenset({"URY Manager", "URY Admin", "System Manager"})
MARK_ROLES = MANAGE_ROLES | {"URY Cashier", "URY Captain", "URY Waiter", "Kitchen User", "Kitchen Cook"}


def _verify_branch_scope(user, branch, company):
	"""Fail closed unless `branch`/`company` are present, then verify the
	caller is actually assigned to `branch` (and that `branch` belongs to
	`company`) before any availability data for it is returned.

	Mirrors the branch-assignment check used elsewhere in this codebase
	(e.g. `ury/ury_pos/api.py:getBranch()` and
	`self_ordering.py:assign_device_table()`'s table-branch check): a user
	is scoped to a branch via the `URY User` child table on `Branch`
	(`tabURY User.parent == Branch.name`, `tabURY User.user == user`).
	System Manager / URY Admin are treated as branch-agnostic staff who
	manage availability across branches, consistent with the manager-role
	handling in `ury_kot_item_execution_service.py`.
	"""
	if not branch or not company:
		frappe.throw(_("[AVAIL-SCOPE] Branch and company are required."), frappe.ValidationError)

	if user == "Administrator":
		return

	roles = set(frappe.get_roles(user))
	if roles & {"System Manager", "URY Admin"}:
		return

	branch_company = frappe.db.get_value("Branch", branch, "company")
	if branch_company != company:
		frappe.throw(_("[AVAIL-SCOPE] Branch does not belong to the given company."), frappe.PermissionError)

	assigned = frappe.db.exists("URY User", {"parenttype": "Branch", "parent": branch, "user": user})
	if not assigned:
		frappe.throw(_("[AVAIL-SCOPE] You are not permitted to view availability for this branch."), frappe.PermissionError)


def _resolve_production_config(item_code, branch, company, department=None):
	"""Read item/branch/(department) production config; `None` if unresolvable.

	See module docstring's "Reconciliation debt: production_policy/..."
	section -- this is a defensive, guessed-schema read against a DocType
	that may not exist yet (V3-13/V3-15 dependency). Returns `None` (not an
	exception) when the table is absent or no matching row exists, so callers
	can fail closed with `CONFIGURATION_ERROR` rather than crash.

	Returns (when resolved) a `frappe._dict` with whichever of these keys the
	underlying table actually has (missing ones come back `None`), so callers
	may use either attribute (`config.production_policy`) or dict-style
	(`config.get("production_policy")`) access -- this matches the shape
	`resolve_production_context` (the authoritative production resolver this
	adapter wraps) itself already returns:
		production_policy, department, production_unit, warehouse,
		production_unit_disabled, department_disabled
	"""
	row = resolve_production_context(item_code, branch, company=company, department=department)
	if not row:
		return None
	return frappe._dict(
		{
			"production_policy": row.get("production_policy"),
			"department": row.get("department"),
			"production_unit": row.get("production_unit"),
			"warehouse": row.get("warehouse"),
			"direct_retail_warehouse": row.get("direct_retail_warehouse"),
			"controlled_by_sales_plan": row.get("controlled_by_sales_plan"),
			"allow_over_plan_sale": row.get("allow_over_plan_sale"),
			"availability_mode": row.get("availability_mode"),
			"product_bundle": row.get("product_bundle"),
			"production_unit_disabled": row.get("production_unit_disabled", 0),
			"department_disabled": row.get("department_disabled", 0),
		}
	)


def _resolve_plan_remaining(item_code, branch, company, department=None):
	"""Read approved plan qty/remaining for `item_code`; `None` if unresolvable.

	See module docstring's "Reconciliation debt: plan_qty/plan_remaining..."
	section (V3-23 dependency). Returns `None` (not an exception) when the
	table is absent or no approved/submitted plan row is found -- callers
	treat that as `NO_ACTIVE_PLAN`.

	`URY Sales Plan` stores per-item quantities in its `items` child table
	(`URY Sales Plan Item`: `item_code`, `qty`, ...), not on the parent --
	the parent only carries scope/status fields (`branch`, `company`,
	`status`, `plan_date`, ...). `URY Sales Plan` is NOT a submittable
	doctype (`is_submittable` unset in its JSON), so `docstatus` is always
	0 for every row -- its approval workflow is tracked entirely via the
	`status` field, not Frappe's submit mechanism. A `docstatus: 1` filter
	here was a bug: it made this query match zero rows on any site,
	regardless of how many plans were genuinely `Approved`/`Locked for
	Production` (found live, tracing why a real seeded-and-approved plan
	was still invisible to this resolver). This resolves the parent
	plan(s) in scope for today's service date and an active `status`
	(`Approved`/`Locked for Production` -- excluding `Draft`/`Proposed`/
	`Submitted for Approval`/`Superseded/Cancelled`), then sums the
	matching child rows, following the same `parent`/`parenttype`
	child-table query convention used elsewhere in this codebase (e.g.
	`ury_bom_compiler.py`) rather than `frappe.db.get_value`
	against nonexistent parent columns.

	`committed_qty`/`fulfilled_qty` are real, transactionally-maintained
	columns on `URY Sales Plan Item` (see `ury_sales_plan_commit.py`'s
	`apply_commit_delta`, wired into `ury_reservation_service.create_reservation`
	/ `_transition_group` at reservation create/release/fulfil time) -- this is
	a plain aggregate `SELECT` over the matched rows, not a locking read, since
	this function only ever informs a display/pre-flight decision and never
	itself mutates the counters.

	Returns (when resolved) a dict: {"plan_qty": ..., "plan_remaining": ...}
	"""
	if not frappe.db.table_exists(SALES_PLAN_DOCTYPE):
		return None

	# Scope to plans that are still in force for today's service date and in
	# an active status -- an unfiltered query sums every submitted plan in
	# the branch/company's entire history, including Superseded/Cancelled
	# ones, so plan_qty/plan_remaining would inflate without bound.
	rows = resolve_plan_item_rows(item_code, branch, company, department=department, plan_date=getdate())
	if not rows:
		return None

	plan_qty = sum(row.get("qty") or 0 for row in rows)
	if not plan_qty:
		return None

	committed = sum(row.get("committed_qty") or 0 for row in rows)
	fulfilled = sum(row.get("fulfilled_qty") or 0 for row in rows)
	plan_remaining = plan_qty - committed - fulfilled
	return {"plan_qty": plan_qty, "plan_remaining": plan_remaining}


def _base_response(
	item_code, company, branch, department, production_policy,
	*, stock_policy_mode="RESERVATION_CONTROLLED", source="PRODUCTION",
):
	return {
		"item_code": item_code,
		"sellable": False,
		"available_qty": 0,
		"production_policy": production_policy,
		"company": company,
		"branch": branch,
		"department": department,
		"production_unit": None,
		"warehouse": None,
		"plan_qty": None,
		"plan_remaining": None,
		"fg_available": None,
		"max_producible": None,
		"blocking_component": None,
		"reason_code": "CONFIGURATION_ERROR",
		"stock_policy_mode": stock_policy_mode,
		"source": source,
		"as_of": now_datetime(),
	}


def _fail_closed(item_code, company, branch, department, production_policy, reason_code):
	response = _base_response(item_code, company, branch, department, production_policy)
	response["reason_code"] = reason_code
	return response


@frappe.whitelist(allow_guest=False)
def get_item_availability(item_code, branch, company, department=None):
	"""Return read-only item availability within the staff member's scope."""
	if not item_code:
		frappe.throw(_("Item code is required"), frappe.ValidationError)

	_verify_branch_scope(frappe.session.user, branch, company)
	return compute_item_availability(item_code, branch, company, department)


def _catalog_gate(item_code):
	"""An unknown item is not sellable, just like a disabled one."""
	return frappe.db.get_value("Item", item_code, "disabled") == 0


def _stock_policy_mode(policy):
	return "RESERVATION_CONTROLLED" if policy.reservation_control_enabled else "NATIVE"


def _catalog_response(item_code, branch, company, department, mode, enabled):
	response = _base_response(
		item_code, company, branch, department, None, stock_policy_mode=mode, source="CATALOG"
	)
	response.update(
		sellable=enabled,
		available_qty=None if enabled else 0,
		reason_code="AVAILABLE" if enabled else "ITEM_DISABLED",
	)
	return response


def _read_overrides(branch, codes=None):
	filters = {"branch": branch}
	if codes is not None:
		filters["item"] = ("in", codes)
	return {
		row["item"]: row
		for row in frappe.get_all(OVERRIDE_DOCTYPE, filters=filters, fields=OVERRIDE_FIELDS)
	}


def _operational_response(item_code, branch, company, department, mode, enabled, override):
	response = _catalog_response(item_code, branch, company, department, mode, enabled)
	if not enabled or not override:
		return response
	if override["status"] == "Sold Out":
		response.update(sellable=False, available_qty=0, source="MANUAL", reason_code="SOLD_OUT_MANUAL")
	elif override["countdown_enabled"]:
		remaining = override["remaining_qty"]
		response.update(
			sellable=remaining > 0, available_qty=remaining, source="COUNT_DOWN",
			reason_code="AVAILABLE" if remaining > 0 else "COUNT_EXHAUSTED",
		)
	return response


def _evaluate_item(item_code, branch, company, department, mode, enabled, override):
	response = _operational_response(item_code, branch, company, department, mode, enabled, override)
	if response["sellable"] and mode != "NATIVE":
		cap = response["available_qty"]
		response = _compute_production_availability(item_code, branch, company, department)
		production_qty = response["available_qty"]
		if response["sellable"] and cap is not None and (production_qty is None or cap <= production_qty):
			response.update(available_qty=cap, source="COUNT_DOWN")
	if override:
		response.update(
			manual_status=override["status"], auto_reset_on_open=bool(override["auto_reset_on_open"]),
			daily_limit=override["daily_limit"] if override["countdown_enabled"] else None,
		)
	return response


def compute_item_availability(item_code, branch, company, department=None, *, policy=None):
	"""Evaluate an item after the caller has verified its branch/company scope."""
	enabled = _catalog_gate(item_code)
	if policy is None:
		policy = get_branch_stock_policy(branch, company)
	mode = _stock_policy_mode(policy)
	override = frappe.db.get_value(
		OVERRIDE_DOCTYPE, {"branch": branch, "item": item_code}, OVERRIDE_FIELDS, as_dict=True,
	) if enabled else None
	return _evaluate_item(item_code, branch, company, department, mode, enabled, override)


def get_reservation_availability(item_code, branch, company, department=None):
	"""Reservation changes reconcile Layer 2 only; order admission owns Layers 0c/1."""
	_verify_branch_scope(frappe.session.user, branch, company)
	mode = _stock_policy_mode(get_branch_stock_policy(branch, company))
	enabled = _catalog_gate(item_code)
	if not enabled or mode == "NATIVE":
		return _catalog_response(item_code, branch, company, department, mode, enabled)
	return _compute_production_availability(item_code, branch, company, department)


def _compute_production_availability(item_code, branch, company, department):
	config = _resolve_production_config(item_code, branch, company, department)
	if config is None:
		return _fail_closed(item_code, company, branch, department, None, "CONFIGURATION_ERROR")

	production_policy = config.get("production_policy")
	resolved_department = config.get("department") or department
	production_unit = config.get("production_unit")
	warehouse = config.get("warehouse")

	if not resolved_department:
		return _fail_closed(
			item_code, company, branch, resolved_department, production_policy, "MISSING_DEPARTMENT"
		)

	if config.get("department_disabled"):
		return _fail_closed(
			item_code, company, branch, resolved_department, production_policy, "DEPARTMENT_DISABLED"
		)

	if not production_policy:
		return _fail_closed(
			item_code, company, branch, resolved_department, production_policy, "CONFIGURATION_ERROR"
		)

	if production_policy in (POLICY_PRE_PRODUCED, POLICY_MADE_TO_ORDER) and not production_unit:
		return _fail_closed(
			item_code, company, branch, resolved_department, production_policy, "MISSING_PRODUCTION_UNIT"
		)

	if production_policy in (POLICY_PRE_PRODUCED, POLICY_MADE_TO_ORDER) and config.get(
		"production_unit_disabled"
	):
		return _fail_closed(
			item_code, company, branch, resolved_department, production_policy, "PRODUCTION_UNIT_DISABLED"
		)

	# BUNDLE items do not require a warehouse on the parent IPC — availability
	# is computed by delegating to each child's own warehouse.
	if production_policy != POLICY_BUNDLE and not warehouse:
		return _fail_closed(
			item_code, company, branch, resolved_department, production_policy, "CONFIGURATION_ERROR"
		)

	response = _base_response(item_code, company, branch, resolved_department, production_policy)
	response["production_unit"] = production_unit
	response["warehouse"] = warehouse

	if production_policy == POLICY_PRE_PRODUCED:
		_fill_pre_produced(response, item_code, branch, company, resolved_department, warehouse, config)
	elif production_policy == POLICY_MADE_TO_ORDER:
		_fill_made_to_order(response, item_code, branch, company, resolved_department, warehouse, config)
	elif production_policy == POLICY_DIRECT_RETAIL:
		_fill_direct_retail(response, item_code, branch, company, warehouse)
	elif production_policy == POLICY_BUNDLE:
		_fill_bundle(response, item_code, branch, company)
	else:
		response["reason_code"] = "CONFIGURATION_ERROR"
		return response

	# `availability_mode` has three options: "Plan Available" (default/safe, no
	# override), "Always Available" and "Stock Available". "Always Available"
	# overrides *commercial* not-sellable reasons only (stock/plan-derived: out
	# of stock, no/exhausted plan, blocking recipe component) — it must NEVER
	# override a structural/config error (missing BOM, missing/disabled
	# department or production unit, generic configuration error), since
	# those indicate the item is not actually safe to sell/produce at all,
	# regardless of plan or stock.
	#
	# "Stock Available" bypasses the Sales Plan gates inside
	# `_fill_pre_produced`/`_fill_made_to_order` (availability is computed
	# from stock/recipe-capacity alone, with plan_qty/plan_remaining left
	# null) but never overrides a structural/config error, so it needs no
	# handling here. It is mutually exclusive with "Always Available".
	_STRUCTURAL_ERROR_CODES = {
		"MISSING_BOM",
		"CONFIGURATION_ERROR",
		"MISSING_DEPARTMENT",
		"DEPARTMENT_DISABLED",
		"PRODUCTION_UNIT_DISABLED",
		"MISSING_PRODUCTION_UNIT",
	}
	availability_mode = config.get("availability_mode") if config else None
	if availability_mode == "Always Available" and response.get("reason_code") not in _STRUCTURAL_ERROR_CODES:
		response["reason_code"] = "AVAILABLE"
		response["sellable"] = True
		# The override makes the item unconditionally sellable, so its computed
		# (possibly zero/negative) stock-derived quantities no longer reflect a
		# real constraint. Signal "unconstrained" with None rather than leaving
		# the real, possibly-zero value in place, which would otherwise block
		# cart-capacity/headroom checks downstream despite sellable=True.
		response["available_qty"] = None
		if "max_producible" in response:
			response["max_producible"] = None

	return response


def _batch_availability(codes, branch, company, policy):
	"""Read availability for the caller's already-resolved menu item codes."""
	codes = list(dict.fromkeys(codes))
	mode = _stock_policy_mode(policy)
	if not codes:
		return {}

	disabled = {
		row["name"]: row["disabled"]
		for row in frappe.get_all("Item", filters={"name": ["in", codes]}, fields=["name", "disabled"])
	}
	overrides = _read_overrides(branch, codes)
	return {
		code: _evaluate_item(code, branch, company, None, mode, disabled.get(code) == 0, overrides.get(code))
		for code in codes
	}


@frappe.whitelist(methods=["GET", "POST"])
def get_menu_availability(branch, company, item_codes):
	"""Read a staff menu's item codes in one request; ordering still checks menu membership."""
	_verify_branch_scope(frappe.session.user, branch, company)
	if isinstance(item_codes, str):
		try:
			item_codes = json.loads(item_codes)
		except ValueError:
			item_codes = None
	if not isinstance(item_codes, list) or any(not isinstance(code, str) for code in item_codes):
		frappe.throw(_("Item codes must be a list of strings"), frappe.ValidationError)
	if len(item_codes) > MAX_BATCH_CODES:
		frappe.throw(_("No more than {0} item codes are allowed").format(MAX_BATCH_CODES), frappe.ValidationError)

	policy = get_branch_stock_policy(branch, company)
	return {
		"stock_policy_mode": _stock_policy_mode(policy),
		"as_of": now_datetime(),
		"items": _batch_availability(item_codes, branch, company, policy),
	}


def _branch_menu_items(branch):
	"""Union of the branch's operational active, room, and order-type menus."""
	restaurants = frappe.get_all(
		"URY Restaurant", filters={"branch": branch}, fields=["name", "active_menu", "room_wise_menu", "order_type_wise_menu"],
	)
	menus = {r["active_menu"] for r in restaurants if r["active_menu"]}
	for table, configuration in (("Menu for Room", "room_wise_menu"), ("Order Type Menu", "order_type_wise_menu")):
		parents = [r["name"] for r in restaurants if r[configuration]]
		if parents:
			menus.update(frappe.get_all(table, filters={"parent": ("in", parents), "parenttype": "URY Restaurant"}, pluck="menu"))
	if not menus:
		return []
	rows = frappe.get_all(
		"URY Menu Item", filters={"parent": ("in", sorted(menus)), "parenttype": "URY Menu", "disabled": 0},
		fields=["item", "item_name"], order_by="item_name asc, item asc",
	)
	return list({r["item"]: {"item": r["item"], "name": r["item_name"] or r["item"]} for r in rows}.values())


def _is_branch_modifier_item(branch, item_code, menu_item_codes):
	"""True when `item_code` is referenced by an active Modifier / Combo Choice group on this branch's menu."""
	if not item_code or not menu_item_codes:
		return False
	try:
		if not (
			frappe.db.table_exists("URY Modifier Group")
			and frappe.db.table_exists("URY Modifier Option")
			and frappe.db.table_exists("URY Item Modifier Group")
		):
			return False
	except Exception:
		return False

	item_groups = list(
		dict.fromkeys(
			g
			for g in (
				frappe.get_all("Item", filters={"name": ("in", menu_item_codes)}, pluck="item_group") or []
			)
			if g
		)
	)
	group_names = set(
		frappe.get_all(
			"URY Item Modifier Group",
			filters={"parenttype": "Item", "parent": ("in", menu_item_codes)},
			pluck="modifier_group",
		)
		or []
	)
	if item_groups:
		group_names.update(
			frappe.get_all(
				"URY Item Modifier Group",
				filters={"parenttype": "Item Group", "parent": ("in", item_groups)},
				pluck="modifier_group",
			)
			or []
		)
	if not group_names:
		return False

	active_groups = frappe.get_all(
		"URY Modifier Group",
		filters={"name": ("in", sorted(group_names)), "disabled": 0},
		pluck="name",
	)
	if not active_groups:
		return False

	options = frappe.get_all(
		"URY Modifier Option",
		filters={"parent": ("in", active_groups), "disabled": 0},
		fields=["item", "upcharge_item"],
	)
	return any(opt.get("item") == item_code or opt.get("upcharge_item") == item_code for opt in options or [])


def _branch_company(branch, *, lock=False):
	company = frappe.db.get_value("Branch", branch, "company", for_update=lock) if branch else None
	if not company:
		frappe.throw(_("[AVAIL-SCOPE] Select a valid branch."), frappe.PermissionError)
	return company


@frappe.whitelist(methods=["GET"])
def get_branch_availability_menu(branch):
	company = _branch_company(branch)
	_verify_branch_scope(frappe.session.user, branch, company)
	return {"company": company, "items": _branch_menu_items(branch)}


@frappe.whitelist(methods=["GET"])
def get_branch_availability_overrides(branch, company):
	_verify_branch_scope(frappe.session.user, branch, company)
	rows = frappe.get_all(
		OVERRIDE_DOCTYPE, filters={"branch": branch},
		fields=OVERRIDE_FIELDS + ["last_reset_on", "reason", "updated_by", "updated_at"], order_by="item asc",
	)
	if rows:
		names = {r["name"]: r["item_name"] for r in frappe.get_all(
			"Item", filters={"name": ("in", [r["item"] for r in rows])}, fields=["name", "item_name"],
		)}
		for row in rows:
			row["item_name"] = names.get(row["item"]) or row["item"]
	return rows


def _publish_availability(branch, items, source):
	if items:
		frappe.publish_realtime(
			f"menu_availability_update_{branch}",
			{"affected_items": sorted(set(items)), "branch": branch, "source": source},
			after_commit=True,
		)


def _whole_count(value):
	try:
		number = Decimal(str(value))
		if number.is_finite() and 0 <= number <= 2147483647 and number == number.to_integral_value():
			return int(number)
	except (InvalidOperation, ValueError):
		pass
	frappe.throw(_("[AVAIL-COUNT-INVALID] Enter a non-negative whole count."), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
def set_item_availability(branch, item_code, status=None, count=None, reset_policy=None, reason=None):
	"""The only public write path; floor staff can make only the safe transition."""
	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	manager = bool(roles & MANAGE_ROLES)
	if not (roles & MARK_ROLES) or (not manager and (status != "Sold Out" or count is not None or reset_policy is not None)):
		frappe.throw(_("[AVAIL-ROLE] You do not have permission to make this change."), frappe.PermissionError)
	company = _branch_company(branch)
	_verify_branch_scope(user, branch, company)
	if status not in (None, "Available", "Sold Out") or reset_policy not in (None, "Never", "Start of business day"):
		frappe.throw(_("[AVAIL-COUNT-INVALID] Select a valid status and reset option."), frappe.ValidationError)
	if status is None and count is None and reset_policy is None:
		frappe.throw(_("[AVAIL-COUNT-INVALID] Choose an availability change."), frappe.ValidationError)
	if count is not None:
		count = _whole_count(count)
	menu_items = _branch_menu_items(branch)
	menu_codes = [r["item"] for r in menu_items]
	if item_code not in menu_codes and not _is_branch_modifier_item(branch, item_code, menu_codes):
		frappe.throw(_("[AVAIL-NOT-ON-MENU] This item is not on a menu for this branch."), frappe.ValidationError)

	# Serialize new rows and first openings. Counts lock only their item rows.
	_branch_company(branch, lock=True)
	name = frappe.db.get_value(OVERRIDE_DOCTYPE, {"branch": branch, "item": item_code}, "name", for_update=True)
	doc = frappe.get_doc(OVERRIDE_DOCTYPE, name, for_update=True) if name else frappe.new_doc(OVERRIDE_DOCTYPE)
	stamp = now_datetime()
	if not name:
		doc.branch, doc.item = branch, item_code
		# A mark made after opening must survive another cashier opening today.
		doc.last_reset_on = frappe.db.get_value(
			"POS Opening Entry", {"branch": branch, "docstatus": 1, "posting_date": ("<=", getdate(stamp))},
			"posting_date", order_by="posting_date desc",
		)
	if status is not None:
		doc.status = status
	if count is not None:
		doc.countdown_enabled, doc.daily_limit, doc.remaining_qty = 1, count, count
		doc.count_started_at = stamp
	if reset_policy is not None:
		doc.auto_reset_on_open = int(reset_policy == "Start of business day")
	doc.reason, doc.updated_by, doc.updated_at = reason, user, stamp
	doc.flags.availability_operation = True
	doc.save(ignore_permissions=True)
	_publish_availability(branch, [item_code], "MANUAL")
	return compute_item_availability(item_code, branch, company)


def _item_totals(lines):
	totals = defaultdict(Decimal)
	for line in lines:
		code = line.get("item") or line.get("item_code")
		if not code:
			continue
		try:
			qty = Decimal(str(line.get("qty") or 0))
			if qty.is_finite():
				totals[code] += qty
				continue
		except InvalidOperation:
			pass
		frappe.throw(_("[AVAIL-COUNT-INVALID] Enter a valid quantity."), frappe.ValidationError)
	return totals


def _void_sources(invoice):
	"""Returns follow the original check, not the creation time of a split bill."""
	names = set()
	current = getattr(invoice, "name", None)
	created_at = getattr(invoice, "creation", None)
	parent = getattr(invoice, "custom_split_from", None)
	if current:
		names.add(current)
	while parent:
		if parent in names:
			return names, None
		names.add(parent)
		row = frappe.db.get_value("POS Invoice", parent, ["creation", "custom_split_from"], as_dict=True)
		if not row:
			return names, None
		created_at, parent = row["creation"], row["custom_split_from"]
	return names, created_at


def _items_with_preparation(invoices, codes):
	"""Conservatively withhold returns once any of this dish has been started."""
	params = (*sorted(invoices), *codes)
	invoice_markers = ", ".join(["%s"] * len(invoices))
	markers = ", ".join(["%s"] * len(codes))
	return {row[0] for row in frappe.db.sql(
		f"""SELECT i.item
		FROM `tabURY KOT` k
		INNER JOIN `tabURY KOT Items` i ON i.parent = k.name AND i.parenttype = 'URY KOT'
		LEFT JOIN `tabURY KOT Item Execution` e ON e.kot_item = i.name
		LEFT JOIN `tabURY KOT Execution` t ON t.kot = k.name
		WHERE k.invoice IN ({invoice_markers}) AND k.type IN ('New Order', 'Order Modified') AND k.docstatus = 1
		AND i.item IN ({markers})
		AND (e.started_at IS NOT NULL OR e.ready_at IS NOT NULL OR e.served_at IS NOT NULL
			OR (e.name IS NULL AND (t.started_at IS NOT NULL OR t.ready_at IS NOT NULL OR t.served_at IS NOT NULL))
			OR k.order_status = 'Served')
		FOR UPDATE""",
		params,
	)}


def apply_count_delta(branch, previous_items, accepted_items, *, invoice=None, prepared=None):
	"""Apply operational admission and net quantity changes in the order transaction."""
	before, after = _item_totals(previous_items), _item_totals(accepted_items)
	deltas = {code: after[code] - before[code] for code in before.keys() | after.keys() if after[code] != before[code]}
	if not branch or not deltas:
		return
	rows = frappe.db.get_values(
		OVERRIDE_DOCTYPE, {"branch": branch, "item": ("in", sorted(deltas))},
		OVERRIDE_FIELDS + ["count_started_at"], as_dict=True, for_update=True, order_by="item asc",
	)
	reductions = [row["item"] for row in rows if row["countdown_enabled"] and deltas[row["item"]] < 0]
	invoice_names, created_at = _void_sources(invoice) if invoice and reductions else ((), None)
	if prepared is None:
		prepared = _items_with_preparation(invoice_names, reductions) if invoice_names else ()
	changed = []
	for row in rows:
		code = row["item"]
		delta = deltas[code]
		if delta > 0 and row["status"] == "Sold Out":
			name = frappe.db.get_value("Item", code, "item_name") or _("This item")
			frappe.throw(_("[AVAIL-SOLD-OUT] {0} is sold out.").format(name), frappe.ValidationError)
		if not row["countdown_enabled"]:
			continue
		if delta > 0:
			qty = _whole_count(delta)
			frappe.db.sql(
				"""UPDATE `tabURY Item Availability` SET remaining_qty = remaining_qty - %s
				WHERE branch = %s AND item = %s AND countdown_enabled = 1 AND remaining_qty >= %s""",
				(qty, branch, code, qty),
			)
			if frappe.db._cursor.rowcount != 1:
				name = frappe.db.get_value("Item", code, "item_name") or _("This item")
				frappe.throw(_("[AVAIL-COUNT-EXHAUSTED] {0} is sold out.").format(name), frappe.ValidationError)
		else:
			# Older orders did not necessarily consume the active allocation.
			started_at = row.get("count_started_at")
			if code in prepared or (
				started_at and invoice and (not created_at or get_datetime(created_at) < get_datetime(started_at))
			):
				continue
			qty = int(-delta)
			if not qty:
				continue
			frappe.db.sql(
				"""UPDATE `tabURY Item Availability` SET remaining_qty = LEAST(daily_limit, remaining_qty + %s)
				WHERE branch = %s AND item = %s AND countdown_enabled = 1 AND remaining_qty < daily_limit""",
				(qty, branch, code),
			)
			if not frappe.db._cursor.rowcount:
				continue
		changed.append(code)
	_publish_availability(branch, changed, "COUNT_DOWN")


def reset_branch_availability(branch, posting_date, opening=None):
	if not branch or not posting_date:
		return
	_branch_company(branch, lock=True)
	stamp = now_datetime()
	# Later tills must not reset a row opted in or changed after the first opening.
	if opening and frappe.db.exists("POS Opening Entry", {
		"branch": branch, "docstatus": 1, "posting_date": posting_date, "name": ("!=", opening),
	}):
		return
	where = "branch = %s AND auto_reset_on_open = 1 AND (last_reset_on IS NULL OR last_reset_on < %s)"
	affected = frappe.db.sql(
		f"SELECT item FROM `tabURY Item Availability` WHERE {where} ORDER BY item FOR UPDATE", (branch, posting_date),
	)
	if not affected:
		return
	frappe.db.sql(
		f"""UPDATE `tabURY Item Availability`
		SET status = 'Available', remaining_qty = CASE WHEN countdown_enabled = 1 THEN daily_limit ELSE remaining_qty END,
			last_reset_on = %s, count_started_at = %s, updated_at = %s, updated_by = %s, reason = NULL
		WHERE {where}""",
		(posting_date, stamp, stamp, frappe.session.user, branch, posting_date),
	)
	_publish_availability(branch, [row[0] for row in affected], "COUNT_DOWN")


def _fill_pre_produced(response, item_code, branch, company, department, warehouse, config=None):
	"""Fill `response` in place for a PRE_PRODUCED item, per V3-40's formula.

	`effective_available = min(plan_remaining, fg_allocatable)`. Reason-code
	priority (per this task's spec): NOT_PRODUCED (fg_available<=0 and never
	produced, i.e. no Bin.actual_qty ever recorded) takes precedence, then
	PLAN_EXHAUSTED, then FG_OUT_OF_STOCK, else AVAILABLE. A missing/absent
	plan is reported as NO_ACTIVE_PLAN before any of those, unless
	`controlled_by_sales_plan` is False or `availability_mode` is
	"Stock Available", in which case the item falls through to stock-based
	availability.
	"""
	fg_projection = project_fg_allocatable(item_code, warehouse, company)
	fg_available = fg_projection["allocatable_qty"]
	never_produced = (fg_projection["bin_actual_qty"] or 0) <= 0

	response["fg_available"] = fg_available
	response["max_producible"] = fg_available

	# controlled_by_sales_plan=0 means the Sales Plan never gates this item,
	# regardless of whether a plan row exists (V3-40 no-plan case) or exists
	# but is exhausted (previously only the no-plan case respected this flag,
	# leaving PLAN_EXHAUSTED able to hard-block a sale even with the flag
	# explicitly off -- both cases must resolve the same way: stock alone).
	# "Stock Available" availability_mode forces the same stock-only path,
	# overriding controlled_by_sales_plan/allow_over_plan_sale.
	controlled_by_sales_plan = config.get("controlled_by_sales_plan", 1) if config else 1
	stock_available_mode = config.get("availability_mode") == "Stock Available" if config else False
	if not controlled_by_sales_plan or stock_available_mode:
		effective_available = fg_available
		response["available_qty"] = max(effective_available, 0)
		if fg_available <= 0 and never_produced:
			response["reason_code"] = "NOT_PRODUCED"
			response["sellable"] = False
		elif fg_available <= 0:
			response["reason_code"] = "FG_OUT_OF_STOCK"
			response["sellable"] = False
		else:
			response["reason_code"] = "AVAILABLE"
			response["sellable"] = effective_available > 0
		return

	plan = _resolve_plan_remaining(item_code, branch, company, department)
	if plan is None:
		# Plan gate is enabled; fail closed without an active plan
		response["reason_code"] = "NO_ACTIVE_PLAN"
		response["sellable"] = False
		response["available_qty"] = 0
		return

	response["plan_qty"] = plan["plan_qty"]
	response["plan_remaining"] = plan["plan_remaining"]

	effective_available = min(plan["plan_remaining"], fg_available)
	response["available_qty"] = max(effective_available, 0)

	if fg_available <= 0 and never_produced:
		response["reason_code"] = "NOT_PRODUCED"
		response["sellable"] = False
	elif plan["plan_remaining"] <= 0:
		# If allow_over_plan_sale is True, allow selling past the plan quantity.
		allow_over_plan_sale = config.get("allow_over_plan_sale", 0) if config else 0
		if allow_over_plan_sale and fg_available > 0:
			# Plan exhausted but over-plan sales are allowed; use stock availability
			response["reason_code"] = "AVAILABLE"
			response["sellable"] = fg_available > 0
			response["available_qty"] = max(fg_available, 0)
		else:
			response["reason_code"] = "PLAN_EXHAUSTED"
			response["sellable"] = False
	elif fg_available <= 0:
		response["reason_code"] = "FG_OUT_OF_STOCK"
		response["sellable"] = False
	else:
		response["reason_code"] = "AVAILABLE"
		response["sellable"] = effective_available > 0


def _fill_made_to_order(response, item_code, branch, company, department, warehouse, config=None):
	"""Fill `response` in place for a MADE_TO_ORDER item, per V3-40's formula.

	`recipe_capacity = floor(min(component_allocatable_i / required_qty_i))`;
	`effective_available = min(plan_remaining, recipe_capacity)`.
	`blocking_component` is set to the limiting component's item_code
	whenever recipe_capacity is the binding constraint (i.e. whenever
	recipe_capacity < plan_remaining, or there is no plan and
	recipe_capacity <= 0), per this task's spec. If `controlled_by_sales_plan`
	is False or `availability_mode` is "Stock Available", plan is optional
	and the item falls through to capacity-based availability. Under
	"Stock Available", `max_producible` is further raised by the read-only
	raw-material top-up projection (`ury_projection_service`), while
	`available_qty` remains the immediate recipe capacity. If
	`allow_over_plan_sale` is True, PLAN_EXHAUSTED can be
	overridden by available recipe capacity.
	"""
	try:
		bom_vector = compile_bom_vector(item_code, 1, company)
	except frappe.ValidationError:
		response["reason_code"] = "MISSING_BOM"
		response["sellable"] = False
		response["available_qty"] = 0
		return

	components = bom_vector["components"]
	component_items = [row["component_item"] for row in components]
	allocatable_by_component = project_component_allocatable(component_items, warehouse, company)

	recipe_capacity = None
	blocking_component = None
	for row in components:
		component_item = row["component_item"]
		qty_per_unit = row["qty_per_unit"]
		if not qty_per_unit:
			continue
		allocatable_qty = allocatable_by_component[component_item]["allocatable_qty"]
		component_capacity = math.floor(allocatable_qty / qty_per_unit)
		if recipe_capacity is None or component_capacity < recipe_capacity:
			recipe_capacity = component_capacity
			blocking_component = component_item

	recipe_capacity = recipe_capacity if recipe_capacity is not None else 0
	recipe_capacity = max(recipe_capacity, 0)

	response["max_producible"] = recipe_capacity

	# controlled_by_sales_plan=0 means the Sales Plan never gates this item,
	# regardless of whether a plan row exists (no-plan case) or exists but is
	# exhausted (previously only the no-plan case respected this flag,
	# leaving PLAN_EXHAUSTED able to hard-block a sale even with the flag
	# explicitly off -- both cases must resolve the same way: capacity alone).
	# "Stock Available" availability_mode forces the same capacity-only path,
	# overriding controlled_by_sales_plan/allow_over_plan_sale.
	controlled_by_sales_plan = config.get("controlled_by_sales_plan", 1) if config else 1
	stock_available_mode = config.get("availability_mode") == "Stock Available" if config else False
	if not controlled_by_sales_plan or stock_available_mode:
		response["available_qty"] = max(recipe_capacity, 0)
		if stock_available_mode:
			# Project additional capacity from raw materials for short
			# PRE_PRODUCED components (read-only; no production documents).
			# available_qty stays the immediate reservable quantity above;
			# only the advisory max_producible is raised.
			try:
				projected = project_mto_capacity(
					item_code,
					company,
					warehouse,
					branch=branch,
					department=department,
					parent_components=components,
					immediate_capacity=recipe_capacity,
					allocatable_by_component={
						component_item: projection["allocatable_qty"]
						for component_item, projection in allocatable_by_component.items()
					},
				)
				response["max_producible"] = max(
					projected.get("max_producible", recipe_capacity), recipe_capacity
				)
			except Exception:
				# Projection is advisory; a failure here must never break the
				# stock-only availability response.
				frappe.logger("ury_availability").exception(
					"MTO capacity projection failed for {0}; falling back to recipe capacity".format(
						item_code
					)
				)
		if recipe_capacity <= 0:
			response["reason_code"] = "BLOCKING_COMPONENT"
			response["sellable"] = False
			response["blocking_component"] = blocking_component
		else:
			response["reason_code"] = "AVAILABLE"
			response["sellable"] = True
		return

	plan = _resolve_plan_remaining(item_code, branch, company, department)
	if plan is None:
		# Plan gate is enabled; fail closed without an active plan
		response["reason_code"] = "NO_ACTIVE_PLAN"
		response["sellable"] = False
		response["available_qty"] = 0
		return

	response["plan_qty"] = plan["plan_qty"]
	response["plan_remaining"] = plan["plan_remaining"]

	effective_available = min(plan["plan_remaining"], recipe_capacity)
	response["available_qty"] = max(effective_available, 0)

	if recipe_capacity < plan["plan_remaining"]:
		response["blocking_component"] = blocking_component

	if recipe_capacity <= 0:
		response["reason_code"] = "BLOCKING_COMPONENT"
		response["sellable"] = False
	elif plan["plan_remaining"] <= 0:
		# If allow_over_plan_sale is True, allow selling past the plan quantity.
		allow_over_plan_sale = config.get("allow_over_plan_sale", 0) if config else 0
		if allow_over_plan_sale and recipe_capacity > 0:
			# Plan exhausted but over-plan sales are allowed; use capacity.
			# No blocking component in this case since we have available capacity.
			response["reason_code"] = "AVAILABLE"
			response["sellable"] = True
			response["available_qty"] = max(recipe_capacity, 0)
		else:
			response["reason_code"] = "PLAN_EXHAUSTED"
			response["sellable"] = False
	else:
		response["reason_code"] = "AVAILABLE"
		response["sellable"] = effective_available > 0


def _fill_direct_retail(response, item_code, branch, company, warehouse):
	"""Fill `response` in place for a DIRECT_RETAIL item.

	Per V3-40: "availability is based on allocatable stock in the relevant
	branch/department warehouse, still subject to reservation and final
	transaction-time validation." No plan/BOM concept applies.
	"""
	allocatable = get_allocatable_qty(item_code, warehouse, company)
	available_qty = allocatable["allocatable_qty"]

	response["fg_available"] = available_qty
	response["max_producible"] = available_qty
	response["available_qty"] = max(available_qty, 0)

	if available_qty <= 0:
		response["reason_code"] = "FG_OUT_OF_STOCK"
		response["sellable"] = False
	else:
		response["reason_code"] = "AVAILABLE"
		response["sellable"] = True


def _fetch_bundle_children(item_code, branch, company):
	"""Return [{child_item_code, qty_of, child_config}, ...] for a BUNDLE item.

	Reads the ``Product Bundle Item`` child table via the IPC's ``product_bundle``
	link and resolves each child's own production context. Returns ``None`` when
	the Product Bundle cannot be resolved (caller treats it as CONFIGURATION_ERROR).
	"""
	ipc = _resolve_production_config(item_code, branch, company)
	if not ipc:
		return None

	product_bundle = ipc.get("product_bundle") or frappe.db.get_value("Product Bundle", {"new_item_code": item_code}, "name")
	if not product_bundle:
		return None

	children = frappe.get_all(
		"Product Bundle Item",
		filters={"parent": product_bundle},
		fields=["item_code", "qty"],
		order_by="idx asc",
	)
	if not children:
		return None

	result = []
	for child in children:
		child_item = child["item_code"]
		child_config = _resolve_production_config(child_item, branch, company)
		result.append({
			"child_item": child_item,
			"qty_of": child["qty"],
			"child_config": child_config,
		})
	return result


def _fill_bundle(response, item_code, branch, company):
	"""Fill ``response`` in place for a BUNDLE item.

	The bundle's ``available_qty`` is the minimum capacity across all Product
	Bundle children. Each child is evaluated using its own production policy:

	  - PRE_PRODUCED child: ``project_fg_allocatable`` on its warehouse.
	  - MADE_TO_ORDER child: BOM explosion + ``project_component_allocatable``.
	  - DIRECT_RETAIL child: ``get_allocatable_qty`` on its warehouse.

	A child without a resolvable config fails the whole bundle (fail closed).
	The ``blocking_component`` field identifies the tightest child.
	"""
	children = _fetch_bundle_children(item_code, branch, company)
	if not children:
		response["reason_code"] = "CONFIGURATION_ERROR"
		response["sellable"] = False
		response["available_qty"] = 0
		return

	min_capacity = None
	blocking = None

	for entry in children:
		child_item = entry["child_item"]
		child_config = entry["child_config"]
		qty_of = entry["qty_of"]

		if not child_config:
			# Missing child config → cannot determine capacity → fail closed.
			response["reason_code"] = "CONFIGURATION_ERROR"
			response["blocking_component"] = child_item
			response["sellable"] = False
			response["available_qty"] = 0
			return

		child_policy = child_config.get("production_policy")
		child_warehouse = child_config.get("warehouse") or child_config.get("direct_retail_warehouse")

		if not child_warehouse:
			response["reason_code"] = "CONFIGURATION_ERROR"
			response["blocking_component"] = child_item
			response["sellable"] = False
			response["available_qty"] = 0
			return

		if child_policy == POLICY_PRE_PRODUCED:
			projection = project_fg_allocatable(child_item, child_warehouse, company)
			child_available = projection["allocatable_qty"]
			# qty_of represents units of the child per one bundle.
			# capacity = how many bundles can be made from this child's stock.
			capacity = math.floor(child_available / qty_of) if qty_of else 0

		elif child_policy == POLICY_MADE_TO_ORDER:
			try:
				bom_vector = compile_bom_vector(child_item, qty_of, company)
			except frappe.ValidationError:
				response["reason_code"] = "MISSING_BOM"
				response["blocking_component"] = child_item
				response["sellable"] = False
				response["available_qty"] = 0
				return
			components = bom_vector["components"]
			component_items = [r["component_item"] for r in components]
			allocatable_map = project_component_allocatable(component_items, child_warehouse, company)
			# Capacity = floor(min(allocatable / required)) across components.
			child_cap = None
			for comp in components:
				comp_item = comp["component_item"]
				comp_qty = comp["qty"]
				if not comp_qty:
					continue
				alloc = allocatable_map[comp_item]["allocatable_qty"]
				c = math.floor(alloc / comp_qty)
				if child_cap is None or c < child_cap:
					child_cap = c
			capacity = max(child_cap if child_cap is not None else 0, 0)

		elif child_policy == POLICY_DIRECT_RETAIL:
			allocatable = get_allocatable_qty(child_item, child_warehouse, company)
			child_available = allocatable["allocatable_qty"]
			capacity = math.floor(child_available / qty_of) if qty_of else 0

		else:
			# Nested bundle or unknown policy: fail closed.
			response["reason_code"] = "CONFIGURATION_ERROR"
			response["blocking_component"] = child_item
			response["sellable"] = False
			response["available_qty"] = 0
			return

		capacity = max(capacity, 0)
		if min_capacity is None or capacity < min_capacity:
			min_capacity = capacity
			blocking = child_item

	available = min_capacity if min_capacity is not None else 0
	response["available_qty"] = available
	response["max_producible"] = available
	response["blocking_component"] = blocking if available <= 0 else None

	if available <= 0:
		response["reason_code"] = "BLOCKING_COMPONENT"
		response["sellable"] = False
	else:
		response["reason_code"] = "AVAILABLE"
		response["sellable"] = True
