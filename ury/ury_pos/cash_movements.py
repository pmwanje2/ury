"""POS touchscreen API for shift till cash movements (Pickup, Loan, Paid Out).

Cashiers record drawer pickups (safe drops), float loans, and paid-outs (including
Tip Payouts attributed to a server via ``tip_server``) from the POS register
without holding general-ledger ``Journal Entry`` DocPerms. This module validates
branch membership, cashier/manager role authorization, and the POS Profile's
configured rules, then writes the native ``Cash Entry`` Journal Entry under app
authority (with ``multi_currency`` for foreign-currency tills, while
``ury_cash_movement`` enforces all GL/shift/SoD invariants on the real
``Journal Entry`` document lifecycle).
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from ury.brand import coded_message
from ury.ury.hooks.ury_cash_movement import (
	LEAVES_DRAWER,
	_drawer_accounts,
	_is_fx_mode,
	_mode_account,
	build_movement_je_accounts,
	line_amount_in_profile_currency,
	paid_out_threshold,
	till_cash_movements,
)
from ury.ury_pos.api import getBranch

TILL_CASH_ROLES = {"URY Cashier", "URY Manager", "URY Admin", "POS Manager", "Accounts Manager", "System Manager"}


def _authorized_roles(profile):
	roles = set(TILL_CASH_ROLES)
	approver_role = profile.get("custom_cash_movement_approver_role")
	if approver_role:
		roles.add(approver_role)
	for row in profile.get("role_allowed_for_billing") or []:
		if row.get("role"):
			roles.add(row.role)
	return roles


def _check_profile_access(profile):
	user = frappe.session.user
	if user == "Administrator":
		return
	user_roles = set(frappe.get_roles(user))
	if "System Manager" in user_roles:
		return
	user_branch = getBranch()
	if profile.branch and user_branch != profile.branch:
		frappe.throw(_("Not permitted outside your branch."), frappe.PermissionError)
	if not _authorized_roles(profile).intersection(user_roles):
		frappe.throw(_("Not permitted to manage till cash movements."), frappe.PermissionError)


def _can_approve_for_profile(profile, user=None):
	user = user or frappe.session.user
	approver_role = profile.get("custom_cash_movement_approver_role")
	if not approver_role:
		return False
	return approver_role in set(frappe.get_roles(user))


def _resolve_open_shift(profile, pos_opening_entry=None, required=True):
	if pos_opening_entry:
		opening = frappe.db.get_value(
			"POS Opening Entry",
			pos_opening_entry,
			["name", "pos_profile", "user", "company", "status", "docstatus"],
			as_dict=True,
		)
		if (
			not opening
			or opening.pos_profile != profile.name
			or opening.company != profile.company
			or opening.docstatus != 1
			or opening.status != "Open"
		):
			if required:
				frappe.throw(
					coded_message(
						"CASH-SHIFT",
						_("Till cash moves only on an open POS shift of {0}.").format(profile.company),
					)
				)
			return None
		return opening

	rows = frappe.get_all(
		"POS Opening Entry",
		filters={"pos_profile": profile.name, "docstatus": 1, "status": "Open"},
		fields=["name", "pos_profile", "user", "company", "status", "docstatus"],
		order_by="period_start_date desc, creation desc",
		limit_page_length=10,
	)
	if not rows:
		if required:
			frappe.throw(
				coded_message(
					"CASH-SHIFT",
					_("Open a POS shift on {0} before recording a till cash movement.").format(profile.name),
				)
			)
		return None
	own = next((row for row in rows if row.user == frappe.session.user), None)
	return own or rows[0]


def _single_cash_drawer(profile):
	drawers = sorted(_drawer_accounts(profile, profile.company))
	if len(drawers) != 1:
		frappe.throw(
			coded_message(
				"CASH-DRAWER",
				_("Configure exactly one Cash payment mode with a company account on {0}.").format(profile.name),
			)
		)
	return drawers[0]


def _counter_account(profile, movement_type, paid_out_target):
	if movement_type in ("Pickup", "Loan"):
		safe = profile.get("custom_cash_safe_account")
		if not safe:
			frappe.throw(
				coded_message(
					"CASH-COUNTER",
					_("Set the POS Profile's Safe Account before recording a {0}.").format(movement_type),
				)
			)
		return safe
	if (paid_out_target or "expense").strip().lower() == "tips":
		tips = profile.get("custom_tips_payable_account")
		if not tips:
			frappe.throw(
				coded_message(
					"CASH-COUNTER",
					_("Set the POS Profile's Tips Payable Account before recording a tip payout."),
				)
			)
		return tips
	paid_out = profile.get("custom_paid_out_account")
	if not paid_out:
		frappe.throw(
			coded_message(
				"CASH-COUNTER",
				_("Set the POS Profile's Paid Out Account before recording a paid-out."),
			)
		)
	return paid_out


def _user_full_name(user):
	if not user:
		return ""
	return frappe.db.get_value("User", user, "full_name") or user


def _till_servers(profile):
	"""Return branch and POS Profile users eligible to be attributed a Tip Payout."""
	emails = []
	for row in profile.get("applicable_for_users") or []:
		if row.get("user") and row.user not in emails:
			emails.append(row.user)
	if profile.get("branch") and frappe.db.exists("Branch", profile.branch):
		branch_doc = frappe.get_doc("Branch", profile.branch)
		for row in branch_doc.get("user") or []:
			if row.get("user") and row.user not in emails:
				emails.append(row.user)
	options = [
		{"user": email, "full_name": _user_full_name(email)}
		for email in emails
		if email not in ("Administrator", "Guest")
	]
	options.sort(key=lambda item: (item["full_name"].lower(), item["user"].lower()))
	return options


def _serialize_movement(entry, profile, can_approve_role):
	kind = entry.get("custom_cash_movement_type") or ""
	leaves = LEAVES_DRAWER.get(kind, True)
	drawers = _drawer_accounts(profile, profile.company)
	company_currency = frappe.get_cached_value("Company", profile.company, "default_currency") or profile.currency
	account_rows = entry.get("accounts") or frappe.get_all(
		"Journal Entry Account",
		filters={"parent": entry.name, "parenttype": "Journal Entry"},
		fields=[
			"account",
			"account_currency",
			"debit_in_account_currency",
			"credit_in_account_currency",
			"debit",
			"credit",
		],
	)
	drawer_line = next(
		(
			row
			for row in account_rows
			if (row.account if hasattr(row, "account") else row.get("account")) in drawers
		),
		None,
	)
	if drawer_line is not None:
		amount = line_amount_in_profile_currency(
			drawer_line, leaves, profile.currency, company_currency, entry.get("posting_date")
		)
	else:
		amount = flt(entry.get("total_debit"))
	drawer_change = -amount if leaves else amount
	tips_account = profile.get("custom_tips_payable_account")
	target_kind = "safe" if kind in ("Pickup", "Loan") else ("paid_in" if kind == "Paid In" else "expense")
	if kind == "Paid Out" and tips_account:
		account_names = [a.account if hasattr(a, "account") else a.get("account") for a in account_rows]
		if tips_account in account_names:
			target_kind = "tips"
	docstatus = cint(entry.get("docstatus"))
	current_user = frappe.session.user
	owner = entry.get("owner") or ""
	approver = entry.get("custom_cash_movement_approver") or ""
	tip_server = entry.get("custom_tip_server") or ""
	is_draft = docstatus == 0
	return {
		"name": entry.name,
		"movement_type": kind,
		"amount": amount,
		"drawer_change": drawer_change,
		"reason": entry.get("custom_cash_movement_reason") or "",
		"reference": entry.get("custom_cash_movement_reference") or "",
		"target_kind": target_kind,
		"tip_server": tip_server,
		"tip_server_name": _user_full_name(tip_server),
		"status": "Submitted" if docstatus == 1 else "Pending Approval",
		"docstatus": docstatus,
		"owner": owner,
		"owner_name": _user_full_name(owner),
		"approver": approver,
		"approver_name": _user_full_name(approver),
		"creation": str(entry.get("creation") or ""),
		"can_approve": bool(is_draft and can_approve_role and owner != current_user),
		"can_discard": bool(is_draft and (owner == current_user or can_approve_role)),
	}


@frappe.whitelist()
def get_till_cash_state(pos_profile):
	"""Return shift cash movement configuration, pending/submitted entries, and net drawer change."""
	profile = frappe.get_doc("POS Profile", pos_profile)
	_check_profile_access(profile)

	enabled = bool(cint(profile.get("custom_enable_cash_movements")))
	opening = _resolve_open_shift(profile, required=False)
	can_approve = _can_approve_for_profile(profile)
	safe = profile.get("custom_cash_safe_account")
	paid_out = profile.get("custom_paid_out_account")
	paid_in = profile.get("custom_paid_in_account")
	tips = profile.get("custom_tips_payable_account")

	paid_out_targets = []
	if paid_out:
		paid_out_targets.append({"kind": "expense", "label": _("Operating Paid-Out")})
	if tips:
		paid_out_targets.append({"kind": "tips", "label": _("Tip Payout")})

	movements = []
	net_drawer_movement = 0.0
	pending_count = 0
	if opening:
		entries = frappe.get_all(
			"Journal Entry",
			filters={"custom_pos_opening_entry": opening.name, "docstatus": ["in", [0, 1]]},
			fields=[
				"name",
				"posting_date",
				"custom_cash_movement_type",
				"total_debit",
				"custom_cash_movement_reason",
				"custom_cash_movement_reference",
				"custom_tip_server",
				"docstatus",
				"owner",
				"custom_cash_movement_approver",
				"creation",
			],
			order_by="creation desc",
			limit_page_length=200,
		)
		for row in entries:
			item = _serialize_movement(row, profile, can_approve)
			movements.append(item)
			if item["docstatus"] == 1:
				net_drawer_movement = flt(net_drawer_movement + item["drawer_change"])
			else:
				pending_count += 1

	return {
		"enabled": enabled,
		"pos_profile": profile.name,
		"company": profile.company,
		"currency": profile.currency,
		"opening_entry": opening.name if opening else None,
		"opening_user": opening.user if opening else None,
		"paid_out_threshold": paid_out_threshold(profile),
		"paid_in_threshold": flt(profile.get("custom_paid_in_approval_threshold")),
		"cash_pull_threshold": flt(profile.get("custom_cash_pull_threshold")),
		"can_approve": can_approve,
		"current_user": frappe.session.user,
		"available_types": {
			"Pickup": bool(safe),
			"Loan": bool(safe),
			"Paid Out": bool(paid_out or tips),
			"Paid In": bool(paid_in),
		},
		"paid_out_targets": paid_out_targets,
		"servers": _till_servers(profile),
		"movements": movements,
		"net_drawer_movement": net_drawer_movement,
		"pending_count": pending_count,
	}


@frappe.whitelist()
def record_till_cash_movement(
	pos_profile,
	movement_type,
	amount,
	reason=None,
	paid_out_target="expense",
	pos_opening_entry=None,
	tip_server=None,
	reference=None,
	approval_token=None,
):
	"""Create (and submit if within threshold or Manager PIN approved) a native Cash Entry Journal Entry.

	Delegates ALL movement types (Pickup, Loan, Paid Out, Paid In) through
	``till_controls._record_cash_movement_impl`` so currency decimal precision (0/2/3dp),
	drawer collision checks, privilege/PIN gates (including ``cash_loan``), Paid-Out
	Data threshold semantics (None vs 0), ``tip_server`` attribution, and multi-currency
	FX rules are enforced identically across every entry point.
	"""
	profile = frappe.get_doc("POS Profile", pos_profile)
	_check_profile_access(profile)

	if not cint(profile.get("custom_enable_cash_movements")):
		frappe.throw(
			coded_message("CASH-DISABLED", _("Till cash movements are not enabled on this POS Profile."))
		)

	from ury.ury_pos.till_controls import _record_cash_movement_impl

	entry, resolved_profile = _record_cash_movement_impl(
		pos_profile=pos_profile,
		movement_type=movement_type,
		amount=amount,
		reason=reason,
		reference=reference,
		paid_out_target=paid_out_target,
		pos_opening_entry=pos_opening_entry,
		approval_token=approval_token,
		tip_server=tip_server,
		allow_pending_draft=not bool(approval_token),
		return_entry=True,
	)
	return _serialize_movement(entry, resolved_profile, _can_approve_for_profile(resolved_profile))


@frappe.whitelist()
def approve_till_cash_movement(journal_entry, approval_token=None, approval_reason=None):
	"""Approve and submit a pending Paid Out / Paid In — either as a signed-in manager or
	in place with a Manager PIN token while the cashier stays signed in."""
	if not frappe.db.exists("Journal Entry", journal_entry):
		frappe.throw(coded_message("CASH-NOT-FOUND", _("Cash movement not found.")))
	entry = frappe.get_doc("Journal Entry", journal_entry)
	if not entry.get("custom_pos_opening_entry"):
		frappe.throw(coded_message("CASH-SHIFT", _("A till cash movement needs its POS shift.")))
	pos_profile = frappe.db.get_value("POS Opening Entry", entry.custom_pos_opening_entry, "pos_profile")
	profile = frappe.get_doc("POS Profile", pos_profile)
	_check_profile_access(profile)

	if not cint(profile.get("custom_enable_cash_movements")):
		frappe.throw(
			coded_message("CASH-DISABLED", _("Till cash movements are not enabled on this POS Profile."))
		)
	if cint(entry.docstatus) != 0:
		frappe.throw(coded_message("CASH-NOT-DRAFT", _("Only pending cash movements can be approved.")))

	if approval_token:
		from ury.ury_pos.till_controls import _require_till_privilege

		claim = {
			"action": "approve_cash_movement",
			"journal_entry": entry.name,
			"movement_type": entry.get("custom_cash_movement_type") or "",
			"amount": flt(entry.total_debit),
			"pos_opening_entry": entry.custom_pos_opening_entry,
		}
		clean_reason = (approval_reason or entry.get("custom_cash_movement_reason") or "").strip()
		if clean_reason:
			claim["reason"] = clean_reason
		approval = _require_till_privilege(
			profile,
			"paid_out",
			entry.name,
			claim,
			token=approval_token,
			amount=flt(entry.total_debit),
			fallback_privilege="cash_movement",
			require_second_person=True,
			excluded_approvers={frappe.session.user, entry.owner},
		)
		entry.flags.cash_movement_approval = {
			"approver": approval.approver,
			"token_consumed": True,
		}

	# before_submit enforces second-person SoD and stamps approver.
	entry.flags.ignore_permissions = True
	entry.submit()
	return _serialize_movement(entry, profile, _can_approve_for_profile(profile))


@frappe.whitelist()
def discard_till_cash_movement(journal_entry):
	"""Delete a draft pending cash movement (creator or approver); refuse submitted movements."""
	if not frappe.db.exists("Journal Entry", journal_entry):
		frappe.throw(coded_message("CASH-NOT-FOUND", _("Cash movement not found.")))
	entry = frappe.get_doc("Journal Entry", journal_entry)
	if not entry.get("custom_pos_opening_entry"):
		frappe.throw(coded_message("CASH-SHIFT", _("A till cash movement needs its POS shift.")))
	pos_profile = frappe.db.get_value("POS Opening Entry", entry.custom_pos_opening_entry, "pos_profile")
	profile = frappe.get_doc("POS Profile", pos_profile)
	_check_profile_access(profile)

	if not cint(profile.get("custom_enable_cash_movements")):
		frappe.throw(
			coded_message("CASH-DISABLED", _("Till cash movements are not enabled on this POS Profile."))
		)
	if cint(entry.docstatus) != 0:
		frappe.throw(coded_message("CASH-NOT-DRAFT", _("Submitted cash movements cannot be discarded.")))

	user = frappe.session.user
	if user != entry.owner and not _can_approve_for_profile(profile, user) and user != "Administrator":
		frappe.throw(_("Only the creator or an authorized approver can discard a pending movement."), frappe.PermissionError)

	frappe.delete_doc("Journal Entry", entry.name, ignore_permissions=True)
	return {"name": entry.name, "status": "Discarded"}


@frappe.whitelist()
def get_shift_cash_movement_totals(pos_opening_entry):
	"""Return submitted net drawer change per Cash payment mode (profile currency) and pending count."""
	opening = frappe.db.get_value(
		"POS Opening Entry",
		pos_opening_entry,
		["name", "pos_profile", "company", "user"],
		as_dict=True,
	)
	if not opening:
		frappe.throw(coded_message("CASH-SHIFT", _("POS shift not found.")))
	profile = frappe.get_doc("POS Profile", opening.pos_profile)
	_check_profile_access(profile)

	precision = frappe.get_precision("POS Closing Entry Detail", "expected_amount") or 2
	till_curr = (
		profile.get("currency")
		or frappe.get_cached_value("Company", opening.company, "default_currency")
		or ""
	).strip().upper()
	cash_modes_by_account = {}
	for row in profile.get("payments") or []:
		if frappe.get_cached_value("Mode of Payment", row.mode_of_payment, "type") == "Cash":
			if _is_fx_mode(row.mode_of_payment, till_curr):
				continue
			account = _mode_account(row.mode_of_payment, opening.company, required=False)
			if account:
				cash_modes_by_account.setdefault(account, []).append(row.mode_of_payment)

	by_mode = {}
	for account, amount in till_cash_movements(opening.name).items():
		modes = cash_modes_by_account.get(account, [])
		if len(modes) == 1:
			mode = modes[0]
			by_mode[mode] = flt(by_mode.get(mode, 0) + amount, precision)

	pending_count = frappe.db.count(
		"Journal Entry",
		filters={"custom_pos_opening_entry": opening.name, "docstatus": 0},
	)
	return {
		"pos_opening_entry": opening.name,
		"by_mode": by_mode,
		"pending_count": cint(pending_count),
	}
