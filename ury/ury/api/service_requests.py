# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
#
# Staff-facing API for URY Service Request (customer "Request Bill" /
# "Request Assistance" taps from the QR/self-order app — created by
# request_bill() in ury/ury/api/self_ordering.py, the guest-facing trust
# boundary). This module is the opposite side: thin, session-authenticated
# endpoints for floor staff (Captains) to see and clear open requests. Kept
# separate from self_ordering.py rather than added there, since that file is
# explicitly documented as the allow_guest trust boundary and these
# endpoints are deliberately NOT allow_guest.
#
# Backported from upstream version-16 (100c43fe). Maximus additions: every
# endpoint is limited to staff of the request's branch, and
# get_service_request_alert_settings() serves the staff alert settings.

import frappe
from frappe.utils import cint, now_datetime

BRANCH_EXEMPT_ROLES = ("System Manager",)
DEFAULT_ESCALATE_MINUTES = 3


def _staff_branches():
	if frappe.session.user == "Administrator" or set(frappe.get_roles()) & set(BRANCH_EXEMPT_ROLES):
		return None
	return set(frappe.get_all("URY User", filters={"user": frappe.session.user, "parenttype": "Branch"}, pluck="parent"))


def _require_branch_staff(branch):
	branches = _staff_branches()
	if branches is not None and (not branch or branch not in branches):
		frappe.throw(frappe._("URY_SERVICE_REQUEST_OTHER_BRANCH"), frappe.PermissionError)


def _require_request_branch(req):
	_require_branch_staff(frappe.db.get_value("URY Table", req.table, "branch") if req.table else None)


@frappe.whitelist()
def list_open_service_requests(branch):
	"""Open/Acknowledged service requests for tables in `branch`, oldest first."""
	if not branch:
		return []
	_require_branch_staff(branch)

	tables = frappe.get_all("URY Table", filters={"branch": branch}, pluck="name")
	if not tables:
		return []

	return frappe.get_all(
		"URY Service Request",
		filters={"table": ["in", tables], "status": ["in", ["Open", "Acknowledged"]]},
		fields=["name", "request_type", "table", "status", "requested_at"],
		order_by="requested_at asc",
	)


@frappe.whitelist()
def acknowledge_service_request(name):
	req = frappe.get_doc("URY Service Request", name)
	if not frappe.has_permission("URY Service Request", "write", doc=req):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	_require_request_branch(req)
	req.status = "Acknowledged"
	req.save()
	return {"name": req.name, "status": req.status}


@frappe.whitelist()
def resolve_service_request(name):
	req = frappe.get_doc("URY Service Request", name)
	if not frappe.has_permission("URY Service Request", "write", doc=req):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	_require_request_branch(req)
	req.status = "Resolved"
	req.resolved_at = now_datetime()
	req.resolved_by = frappe.session.user
	req.save()
	return {"name": req.name, "status": req.status}


@frappe.whitelist()
def get_service_request_alert_settings(branch):
	"""Staff alert settings from the branch's POS Profile: chime on/off and escalation minutes."""
	_require_branch_staff(branch)
	values = frappe.db.get_value(
		"POS Profile",
		{"branch": branch, "disabled": 0},
		["custom_service_request_sound", "custom_service_request_escalate_minutes"],
		as_dict=True,
	) or {}
	sound = values.get("custom_service_request_sound")
	minutes = cint(values.get("custom_service_request_escalate_minutes"))
	return {
		"sound": 1 if sound is None else cint(sound),
		"escalate_minutes": minutes if minutes > 0 else DEFAULT_ESCALATE_MINUTES,
	}
