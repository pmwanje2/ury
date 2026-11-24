"""POS till cash controls & supervisor authorisation gates (B5b — Simphony / SAP parity).

Covers matrix rows 23–29 and 32 (tracker G23, G24, G34):
- Paid Out / Paid In above the till's limit with Manager PIN in place (never Desk),
  mandatory reason & reference, posting through #145's native Cash Entry Journal Entry path.
- Pickup (cash pull / safe drop) & Loan (float), with Cash Pull threshold prompt and
  PIN-approved logged Threshold Exception.
- No Sale (drawer kick) with reason, PIN when not allowed alone, immutable POS Drawer Event,
  counted on X/Z reports.
- Currency precision (including zero-decimal UGX) and foreign-currency drawer isolation.
- Physical cash drawer collision guard (two tills cannot operate one assigned physical drawer).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt


def now_datetime():
	return frappe.utils.now_datetime()


def nowdate():
	return frappe.utils.nowdate()


def get_datetime(val=None):
	return frappe.utils.get_datetime(val)


def get_time(val=None):
	return frappe.utils.get_time(val)

from ury.brand import coded_message
from ury.ury.hooks.ury_cash_movement import (
	LEAVES_DRAWER,
	_drawer_accounts,
	_mode_account,
	build_movement_je_accounts,
	paid_out_threshold,
	till_cash_movements,
)
from ury.ury_pos.api import getBranch

TILL_OPERATOR_ROLES = frozenset(
	{"URY Cashier", "URY Manager", "URY Admin", "POS Manager", "Accounts Manager", "System Manager"}
)
LEGACY_MANAGER_ROLES = frozenset({"URY Manager", "URY Admin", "Accounts Manager", "System Manager"})
ZERO_DECIMAL_CURRENCIES = frozenset(
	{"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
)
THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})
DRAWER_KICK_HEX = "1b700019fa"
DRAWER_KICK_COMMAND = "\x1b\x70\x00\x19\xfa"


def _load_override_module():
	"""Import B2a's `ury.ury_pos.override` when installed on this branch."""
	try:
		from ury.ury_pos import override as override_mod

		return override_mod
	except Exception:
		return None


def _resolve_privilege_code(override_mod, preferred: str, fallback: str | None = None) -> str:
	privileges = getattr(override_mod, "PRIVILEGES", None) or {}
	if preferred in privileges:
		return preferred
	if fallback and fallback in privileges:
		return fallback
	return preferred


def _has_configured_rules(company: str | None, privilege: str) -> bool:
	override_mod = _load_override_module()
	if not override_mod or not company:
		return False
	try:
		return bool(override_mod.is_policy_active(company))
	except Exception:
		return False


def _may_perform_till_privilege(
	user: str,
	profile,
	privilege: str,
	*,
	amount: float | None = None,
	fallback_privilege: str | None = None,
	allow_cashier_default: bool = False,
) -> bool:
	"""Check privilege via B2a override rules when active, else fail-safe legacy manager defaults.

	Keeps the till usable on sites with no POS Privilege Rule rows: today's manager roles may act,
	others need a manager PIN (unless the action explicitly permits cashiers by default, e.g. Pickup).
	"""
	if not user or user == "Guest":
		return False
	override_mod = _load_override_module()
	is_non_interactive = getattr(override_mod, "_is_non_interactive_or_agent_session", None) if override_mod else None
	if callable(is_non_interactive) and is_non_interactive(user):
		return False
	if override_mod and _has_configured_rules(profile.company, privilege):
		code = _resolve_privilege_code(override_mod, privilege, fallback_privilege)
		return bool(override_mod.may_perform(user, profile.name, code, amount=amount))

	roles = set(frappe.get_roles(user))
	if user == "Administrator" or bool(LEGACY_MANAGER_ROLES & roles):
		return True
	approver_role = profile.get("custom_cash_movement_approver_role")
	if approver_role and approver_role in roles:
		return True
	if allow_cashier_default and bool(TILL_OPERATOR_ROLES & roles):
		return True
	return False


def _require_till_privilege(
	profile,
	privilege: str,
	reference_name: str,
	claim: dict[str, Any],
	*,
	token: str | None = None,
	amount: float | None = None,
	fallback_privilege: str | None = None,
	allow_cashier_default: bool = False,
	require_second_person: bool = False,
	excluded_approvers: set[str] | None = None,
):
	"""Enforce a till privilege. Returns consumed approval dict when a PIN token was used, else None."""
	override_mod = _load_override_module()
	code = _resolve_privilege_code(override_mod, privilege, fallback_privilege) if override_mod else privilege

	if token:
		if not override_mod:
			frappe.throw(
				coded_message("AUTH-REQUIRED", _("Manager PIN approval service is unavailable.")),
				frappe.PermissionError,
			)
		approval = override_mod.consume(token, code, profile.name, reference_name, claim)
		approver = (approval.get("approver") or "").strip()
		excluded = {(frappe.session.user or "").casefold()}
		if excluded_approvers:
			excluded |= {u.casefold() for u in excluded_approvers if u}
		if approver.casefold() in excluded:
			frappe.throw(
				coded_message("AUTH-SELF", _("You cannot approve your own request. Ask another manager.")),
				frappe.PermissionError,
			)
		return approval

	if not require_second_person and _may_perform_till_privilege(
		frappe.session.user,
		profile,
		privilege,
		amount=amount,
		fallback_privilege=fallback_privilege,
		allow_cashier_default=allow_cashier_default,
	):
		return None

	frappe.throw(
		coded_message("AUTH-REQUIRED", _("A manager must approve this action with their PIN.")),
		frappe.PermissionError,
	)


def currency_precision(currency: str | None) -> int:
	"""Return decimal places for ``currency`` (0 for UGX/JPY/etc., honoring Currency master)."""
	code = (currency or "").strip().upper()
	if code:
		row = frappe.db.get_value(
			"Currency",
			code,
			["fraction_units", "smallest_currency_fraction_value"],
			as_dict=True,
		)
		if row:
			units = cint(row.get("fraction_units"))
			smallest = flt(row.get("smallest_currency_fraction_value"))
			if units == 1 or smallest >= 1:
				return 0
			if units == 1000:
				return 3
			if units == 100:
				return 2
		if code in ZERO_DECIMAL_CURRENCIES:
			return 0
		if code in THREE_DECIMAL_CURRENCIES:
			return 3
	return cint(frappe.get_precision("POS Closing Entry Detail", "expected_amount")) or 2


def parse_positive_amount(raw_amount: Any, currency: str | None) -> float:
	"""Parse a finite strictly-positive amount and enforce currency decimal precision."""
	if isinstance(raw_amount, bool) or raw_amount is None or raw_amount == "":
		frappe.throw(coded_message("CASH-AMOUNT", _("Amount must be greater than zero.")))
	try:
		value = float(raw_amount)
	except (TypeError, ValueError):
		frappe.throw(coded_message("CASH-AMOUNT", _("Amount must be a valid number greater than zero.")))
	if not math.isfinite(value) or value <= 0:
		frappe.throw(coded_message("CASH-AMOUNT", _("Amount must be greater than zero.")))

	precision = currency_precision(currency)
	rounded = flt(value, precision)
	if abs(value - rounded) > 1e-6:
		if precision == 0:
			frappe.throw(
				coded_message(
					"CASH-PRECISION",
					_("{0} amounts must be whole numbers without decimals.").format(currency or _("Currency")),
				)
			)
		frappe.throw(
			coded_message(
				"CASH-PRECISION",
				_("{0} amounts allow at most {1} decimal places.").format(currency or _("Currency"), precision),
			)
		)
	if rounded <= 0:
		frappe.throw(coded_message("CASH-AMOUNT", _("Amount must be greater than zero.")))
	return int(rounded) if precision == 0 else rounded


def _check_profile_access(profile):
	user = frappe.session.user
	if user == "Administrator":
		return
	roles = set(frappe.get_roles(user))
	if "System Manager" in roles:
		return
	user_branch = getBranch()
	if profile.get("branch") and user_branch != profile.get("branch"):
		frappe.throw(_("Not permitted outside your branch."), frappe.PermissionError)
	allowed = set(TILL_OPERATOR_ROLES)
	approver_role = profile.get("custom_cash_movement_approver_role")
	if approver_role:
		allowed.add(approver_role)
	for row in profile.get("role_allowed_for_billing") or []:
		if row.get("role"):
			allowed.add(row.role)
	if not allowed.intersection(roles):
		frappe.throw(_("Not permitted to operate this till."), frappe.PermissionError)


def _resolve_open_shift(profile, pos_opening_entry=None, required=True):
	if pos_opening_entry:
		opening = frappe.db.get_value(
			"POS Opening Entry",
			pos_opening_entry,
			["name", "pos_profile", "user", "company", "status", "docstatus", "period_start_date"],
			as_dict=True,
		)
		if (
			not opening
			or opening.pos_profile != profile.name
			or opening.company != profile.company
			or cint(opening.docstatus) != 1
			or opening.status != "Open"
		):
			if required:
				frappe.throw(
					coded_message(
						"CASH-SHIFT",
						_("This action requires an open POS shift on {0}.").format(profile.name),
					)
				)
			return None
		return opening

	rows = frappe.get_all(
		"POS Opening Entry",
		filters={"pos_profile": profile.name, "docstatus": 1, "status": "Open"},
		fields=["name", "pos_profile", "user", "company", "status", "docstatus", "period_start_date"],
		order_by="period_start_date desc, creation desc",
		limit_page_length=10,
	)
	if not rows:
		if required:
			frappe.throw(
				coded_message(
					"CASH-SHIFT",
					_("Open a POS shift on {0} first.").format(profile.name),
				)
			)
		return None
	own = next((row for row in rows if row.user == frappe.session.user), None)
	return own or rows[0]


def check_drawer_collision(profile, opening_entry_name: str | None = None) -> None:
	"""Refuse when another open POS Profile claims the same physical cash drawer ID."""
	drawer_id = (profile.get("custom_cash_drawer_id") or "").strip()
	if not drawer_id:
		return
	other_profiles = frappe.get_all(
		"POS Profile",
		filters={"company": profile.company, "custom_cash_drawer_id": drawer_id},
		pluck="name",
	)
	peers = [p for p in other_profiles if p and p != profile.name]
	if not peers:
		return
	active_peers = frappe.get_all(
		"POS Opening Entry",
		filters={"pos_profile": ["in", peers], "docstatus": 1, "status": "Open"},
		fields=["name", "pos_profile", "user"],
		limit_page_length=5,
	)
	conflict = next((row for row in active_peers if row.name != opening_entry_name), None)
	if conflict:
		frappe.throw(
			coded_message(
				"DRAWER-IN-USE",
				_(
					"Cash drawer {0} is already assigned to open till {1} ({2}). "
					"Close that shift or assign another drawer first."
				).format(drawer_id, conflict.pos_profile, conflict.name),
			)
		)


def _validate_reason_and_note(reason: str | None, note_or_ref: str | None, *, missing_code: str, note_code: str):
	clean_reason = (reason or "").strip()
	if not clean_reason:
		frappe.throw(coded_message(missing_code, _("Select or enter a reason first.")))
	clean_note = (note_or_ref or "").strip()
	if frappe.db.table_exists("POS Reason") and frappe.db.exists("POS Reason", clean_reason):
		reason_row = frappe.db.get_value(
			"POS Reason", clean_reason, ["disabled", "requires_note", "description"], as_dict=True
		)
		if reason_row:
			if cint(reason_row.get("disabled")):
				frappe.throw(coded_message(missing_code, _("That reason code is disabled.")))
			if cint(reason_row.get("requires_note")) and not clean_note:
				frappe.throw(
					coded_message(note_code, _("This reason requires a reference or note.")),
				)
	return clean_reason, clean_note


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


def _counter_account(profile, movement_type: str, paid_out_target: str):
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
	if movement_type == "Paid In":
		paid_in = profile.get("custom_paid_in_account")
		if not paid_in:
			frappe.throw(
				coded_message(
					"CASH-COUNTER",
					_("Set the POS Profile's Paid In Account before recording a paid-in."),
				)
			)
		return paid_in
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


def _account_currency(account: str, company_currency: str) -> str:
	return frappe.db.get_value("Account", account, "account_currency") or company_currency


def _resolve_exchange_rate(source_currency: str, target_currency: str, posting_date: str) -> float:
	if not source_currency or not target_currency or source_currency == target_currency:
		return 1.0
	from erpnext.setup.utils import get_exchange_rate

	rate = flt(get_exchange_rate(source_currency, target_currency, posting_date))
	if rate <= 0:
		frappe.throw(
			coded_message(
				"CASH-EXCHANGE-RATE",
				_("Set an exchange rate from {0} to {1} before posting a foreign-currency drawer movement.").format(
					source_currency, target_currency
				),
			)
		)
	return rate


def _is_blind_masked_for_user(profile, user: str | None = None) -> bool:
	user = user or frappe.session.user
	if cint(profile.get("custom_blind_cash_count")):
		return True
	roles = set(frappe.get_roles(user))
	masked_roles = {row.get("role") for row in (profile.get("role_masked_for_amounts") or []) if row.get("role")}
	return bool(masked_roles & roles)


def _compute_expected_drawer_cash(profile, opening) -> float:
	"""Expected cash in this shift's drawer: opening float + submitted cash sales - change + submitted movements."""
	currency = profile.get("currency") or frappe.db.get_value("Company", profile.company, "default_currency")
	precision = currency_precision(currency)
	cash_modes = [
		row.mode_of_payment
		for row in (profile.get("payments") or [])
		if frappe.get_cached_value("Mode of Payment", row.mode_of_payment, "type") == "Cash"
	]
	cash_mode_set = set(cash_modes)
	drawer_accounts = _drawer_accounts(profile, profile.company)

	# 1. Opening float for Cash tenders on this shift
	opening_doc = frappe.get_doc("POS Opening Entry", opening.name)
	total = 0.0
	for row in opening_doc.get("balance_details") or []:
		if row.get("mode_of_payment") in cash_mode_set:
			total += flt(row.get("opening_amount"))

	# 2. Submitted unconsolidated POS Invoices on this profile since shift start
	start_dt = get_datetime(opening.get("period_start_date")) if opening.get("period_start_date") else None
	invoices = frappe.get_all(
		"POS Invoice",
		filters={"docstatus": 1, "pos_profile": profile.name, "consolidated_invoice": ["is", "not set"]},
		fields=["name", "posting_date", "posting_time", "change_amount", "is_return"],
		limit_page_length=0,
	)
	for inv_row in invoices:
		if start_dt and inv_row.get("posting_date"):
			inv_ts = datetime.combine(
				get_datetime(inv_row.posting_date).date(),
				get_time(inv_row.get("posting_time") or "00:00:00"),
			)
			if inv_ts < start_dt:
				continue
		inv_doc = frappe.get_doc("POS Invoice", inv_row.name)
		cash_paid = 0.0
		for pay in inv_doc.get("payments") or []:
			if pay.get("mode_of_payment") in cash_mode_set:
				cash_paid += flt(pay.get("amount"))
		change = flt(inv_doc.get("change_amount")) if not inv_doc.get("is_return") else 0.0
		total += cash_paid - change

	# 3. Submitted cash movements linked strictly to THIS POS Opening Entry
	movements = till_cash_movements(opening.name, currency=currency)
	for acct, delta in movements.items():
		if acct in drawer_accounts:
			total += flt(delta)

	rounded = flt(total, precision)
	return int(rounded) if precision == 0 else rounded


def _insert_drawer_event(
	*,
	event_type: str,
	profile,
	opening_entry: str | None = None,
	closing_entry: str | None = None,
	approver: str | None = None,
	reason: str,
	note: str = "",
	drawer_expected_cash: float = 0,
	threshold_amount: float = 0,
	drawer_kick_sent: int = 0,
	open_checks_snapshot: str = "",
):
	ev = frappe.get_doc(
		{
			"doctype": "POS Drawer Event",
			"event_type": event_type,
			"company": profile.company,
			"pos_profile": profile.name,
			"branch": profile.get("branch"),
			"pos_opening_entry": opening_entry,
			"pos_closing_entry": closing_entry,
			"operator": frappe.session.user,
			"approver": approver,
			"reason": reason,
			"note": note,
			"drawer_expected_cash": drawer_expected_cash,
			"threshold_amount": threshold_amount,
			"drawer_kick_sent": cint(drawer_kick_sent),
			"open_checks_snapshot": open_checks_snapshot,
			"event_time": now_datetime(),
		}
	)
	ev.flags.pos_drawer_server_insert = True
	ev.flags.ignore_permissions = True
	ev.insert(ignore_permissions=True)
	return ev


def _user_full_name(user: str | None) -> str:
	if not user:
		return ""
	return frappe.db.get_value("User", user, "full_name") or user


def _record_cash_movement_impl(
	*,
	pos_profile: str,
	movement_type: str,
	amount: Any,
	reason: str | None = None,
	reference: str | None = None,
	paid_out_target: str = "expense",
	pos_opening_entry: str | None = None,
	approval_token: str | None = None,
	tip_server: str | None = None,
	allow_pending_draft: bool = False,
	return_entry: bool = False,
) -> Any:
	"""Canonical till cash movement implementation enforcing precision, drawer collision,
	privileges/PIN, Paid-Out Data threshold (None vs 0), multi-currency FX, and tip_server."""
	profile = frappe.get_doc("POS Profile", pos_profile)
	_check_profile_access(profile)

	movement_type = (movement_type or "").strip()
	if movement_type not in LEAVES_DRAWER:
		frappe.throw(
			coded_message("CASH-TYPE", _("Choose Pickup, Loan, Paid Out or Paid In for a till cash movement."))
		)

	currency = profile.get("currency") or frappe.db.get_value("Company", profile.company, "default_currency")
	amount_val = parse_positive_amount(amount, currency)

	opening = _resolve_open_shift(profile, pos_opening_entry=pos_opening_entry, required=True)
	check_drawer_collision(profile, opening.name)

	clean_reason = (reason or "").strip()
	clean_ref = (reference or "").strip()
	if movement_type in ("Paid Out", "Paid In"):
		clean_reason, clean_ref = _validate_reason_and_note(
			clean_reason,
			clean_ref,
			missing_code="CASH-REASON",
			note_code="CASH-REFERENCE",
		)

	clean_target = (paid_out_target or "expense").strip().lower()
	action_key = {
		"Paid Out": "paid_out",
		"Paid In": "paid_in",
		"Pickup": "cash_pickup",
		"Loan": "cash_loan",
	}[movement_type]
	claim = {
		"action": action_key,
		"movement_type": movement_type,
		"amount": amount_val,
		"reason": clean_reason,
		"reference": clean_ref,
		"paid_out_target": clean_target,
		"pos_opening_entry": opening.name,
	}

	approval = None
	save_as_pending_draft = False
	if movement_type == "Paid Out":
		threshold = paid_out_threshold(profile)
		if threshold is not None and amount_val > threshold:
			if allow_pending_draft and not approval_token:
				# Two-step touchscreen workflow: save Pending Approval draft; never submit
				# without a second authorized person approving.
				save_as_pending_draft = True
			else:
				# Above the till limit: strictly requires a second person (Manager PIN in place)
				approval = _require_till_privilege(
					profile,
					"paid_out",
					opening.name,
					claim,
					token=approval_token,
					amount=amount_val,
					fallback_privilege="cash_movement",
					require_second_person=True,
				)
	elif movement_type == "Paid In":
		threshold = flt(profile.get("custom_paid_in_approval_threshold"))
		if amount_val > threshold:
			# Above the till limit: strictly requires a second person (Manager PIN in place)
			approval = _require_till_privilege(
				profile,
				"paid_out",
				opening.name,
				claim,
				token=approval_token,
				amount=amount_val,
				fallback_privilege="cash_movement",
				require_second_person=True,
			)
	elif movement_type == "Loan":
		approval = _require_till_privilege(
			profile,
			"cash_loan",
			opening.name,
			claim,
			token=approval_token,
			amount=amount_val,
			fallback_privilege="cash_movement",
			allow_cashier_default=False,
		)
	elif movement_type == "Pickup":
		approval = _require_till_privilege(
			profile,
			"cash_pickup",
			opening.name,
			claim,
			token=approval_token,
			amount=amount_val,
			fallback_privilege="cash_movement",
			allow_cashier_default=True,
		)

	drawer = _single_cash_drawer(profile)
	counter = _counter_account(profile, movement_type, clean_target)
	leaves = LEAVES_DRAWER[movement_type]

	posting_date = nowdate()
	company_currency = frappe.db.get_value("Company", profile.company, "default_currency") or currency
	multi_currency, je_accounts = build_movement_je_accounts(
		drawer=drawer,
		counter=counter,
		drawer_leaves=leaves,
		profile_amount=amount_val,
		profile_currency=currency or company_currency,
		company_currency=company_currency,
		posting_date=posting_date,
		cost_center=profile.get("cost_center"),
	)

	clean_server = (
		(tip_server or "").strip() or None
		if movement_type == "Paid Out" and clean_target == "tips"
		else None
	)
	remark_parts = [clean_reason or _("{0} on shift {1}").format(movement_type, opening.name)]
	if clean_ref:
		remark_parts.append(f"Ref: {clean_ref}")

	entry = frappe.new_doc("Journal Entry")
	entry.voucher_type = "Cash Entry"
	entry.company = profile.company
	entry.posting_date = posting_date
	entry.custom_pos_opening_entry = opening.name
	entry.custom_cash_movement_type = movement_type
	entry.custom_cash_movement_reason = clean_reason or None
	entry.custom_cash_movement_reference = clean_ref or None
	entry.custom_tip_server = clean_server
	entry.user_remark = " · ".join(remark_parts)
	if multi_currency:
		entry.multi_currency = 1
	for row in je_accounts:
		entry.append("accounts", row)
	entry.flags.ignore_permissions = True
	if approval:
		entry.flags.cash_movement_approval = {
			"approver": approval.approver,
			"token_consumed": True,
		}
	entry.insert(ignore_permissions=True)
	if not save_as_pending_draft:
		entry.flags.ignore_permissions = True
		entry.submit()

	if return_entry:
		return entry, profile

	drawer_change = -amount_val if leaves else amount_val
	approver_user = entry.get("custom_cash_movement_approver") or (approval.approver if approval else None)
	return {
		"name": entry.name,
		"movement_type": movement_type,
		"amount": amount_val,
		"currency": currency,
		"drawer_change": drawer_change,
		"reason": clean_reason,
		"reference": clean_ref,
		"tip_server": clean_server or "",
		"tip_server_name": _user_full_name(clean_server),
		"docstatus": cint(entry.docstatus),
		"status": "Submitted" if cint(entry.docstatus) == 1 else "Pending Approval",
		"owner": entry.owner,
		"owner_name": _user_full_name(entry.owner),
		"approver": approver_user,
		"approver_name": _user_full_name(approver_user),
	}


@frappe.whitelist(methods=["POST"])
def record_cash_movement(
	pos_profile: str,
	movement_type: str,
	amount: Any,
	reason: str | None = None,
	reference: str | None = None,
	paid_out_target: str = "expense",
	pos_opening_entry: str | None = None,
	approval_token: str | None = None,
	tip_server: str | None = None,
) -> dict[str, Any]:
	"""Record & post a native Cash Entry Journal Entry (Pickup, Loan, Paid Out, Paid In)
	with in-place Manager PIN approval above limits — never requiring a trip to Desk."""
	return _record_cash_movement_impl(
		pos_profile=pos_profile,
		movement_type=movement_type,
		amount=amount,
		reason=reason,
		reference=reference,
		paid_out_target=paid_out_target,
		pos_opening_entry=pos_opening_entry,
		approval_token=approval_token,
		tip_server=tip_server,
		allow_pending_draft=False,
		return_entry=False,
	)


@frappe.whitelist(methods=["GET"])
def get_till_drawer_status(pos_profile: str, pos_opening_entry: str | None = None) -> dict[str, Any]:
	"""Return current drawer cash pull threshold status, respecting blind close masking."""
	profile = frappe.get_doc("POS Profile", pos_profile)
	_check_profile_access(profile)
	currency = profile.get("currency") or frappe.db.get_value("Company", profile.company, "default_currency")
	opening = _resolve_open_shift(profile, pos_opening_entry=pos_opening_entry, required=False)
	threshold = flt(profile.get("custom_cash_pull_threshold"))
	blind = _is_blind_masked_for_user(profile)

	if not opening:
		return {
			"pos_profile": profile.name,
			"currency": currency,
			"opening_entry": None,
			"blind_close": blind,
			"cash_pull_threshold": threshold,
			"threshold_exceeded": False,
			"expected_drawer_cash": None,
			"suggested_pickup_amount": None,
			"threshold_exception_count": 0,
			"no_sale_count": 0,
			"over_short_tolerance": flt(profile.get("custom_over_short_tolerance")),
			"paid_out_threshold": paid_out_threshold(profile),
			"paid_in_threshold": flt(profile.get("custom_paid_in_approval_threshold")),
		}

	expected_cash = _compute_expected_drawer_cash(profile, opening)
	threshold_exceeded = bool(threshold > 0 and expected_cash >= threshold)
	precision = currency_precision(currency)
	suggested = None
	if not blind and threshold_exceeded:
		diff = max(0.0, expected_cash - threshold * 0.5)
		suggested = int(flt(diff, 0)) if precision == 0 else flt(diff, precision)

	no_sale_count = frappe.db.count(
		"POS Drawer Event", {"pos_opening_entry": opening.name, "event_type": "No Sale"}
	)
	exc_count = frappe.db.count(
		"POS Drawer Event", {"pos_opening_entry": opening.name, "event_type": "Threshold Exception"}
	)
	return {
		"pos_profile": profile.name,
		"currency": currency,
		"opening_entry": opening.name,
		"opening_user": opening.user,
		"blind_close": blind,
		"cash_pull_threshold": threshold,
		"threshold_exceeded": threshold_exceeded,
		"expected_drawer_cash": None if blind else expected_cash,
		"suggested_pickup_amount": None if blind else suggested,
		"threshold_exception_count": cint(exc_count),
		"no_sale_count": cint(no_sale_count),
		"over_short_tolerance": flt(profile.get("custom_over_short_tolerance")),
		"paid_out_threshold": paid_out_threshold(profile),
		"paid_in_threshold": flt(profile.get("custom_paid_in_approval_threshold")),
	}


@frappe.whitelist(methods=["POST"])
def log_cash_pull_exception(
	pos_profile: str,
	reason: str,
	note: str = "",
	approval_token: str | None = None,
	pos_opening_entry: str | None = None,
) -> dict[str, Any]:
	"""Log a Simphony Opt 258 Threshold Level Exception with Manager PIN and mandatory reason."""
	profile = frappe.get_doc("POS Profile", pos_profile)
	_check_profile_access(profile)
	opening = _resolve_open_shift(profile, pos_opening_entry=pos_opening_entry, required=True)
	check_drawer_collision(profile, opening.name)

	clean_reason, clean_note = _validate_reason_and_note(
		reason, note, missing_code="CASH-REASON", note_code="CASH-REFERENCE"
	)
	currency = profile.get("currency") or frappe.db.get_value("Company", profile.company, "default_currency")
	precision = currency_precision(currency)
	raw_threshold = flt(profile.get("custom_cash_pull_threshold"), precision)
	threshold = int(raw_threshold) if precision == 0 else raw_threshold
	expected_cash = _compute_expected_drawer_cash(profile, opening)

	claim = {
		"action": "threshold_exception",
		"pos_profile": profile.name,
		"pos_opening_entry": opening.name,
		"threshold_amount": threshold,
		"reason": clean_reason,
	}
	# Simphony Opt 258 (Threshold Level Exception) is Manager-only
	approval = _require_till_privilege(
		profile,
		"cash_pickup",
		opening.name,
		claim,
		token=approval_token,
		amount=expected_cash,
		fallback_privilege="cash_movement",
		allow_cashier_default=False,
	)
	approver = approval.approver if approval else None
	ev = _insert_drawer_event(
		event_type="Threshold Exception",
		profile=profile,
		opening_entry=opening.name,
		approver=approver,
		reason=clean_reason,
		note=clean_note,
		drawer_expected_cash=expected_cash,
		threshold_amount=threshold,
	)
	return {
		"name": ev.name,
		"event_type": ev.event_type,
		"pos_opening_entry": opening.name,
		"reason": clean_reason,
		"note": clean_note,
		"operator": ev.operator,
		"approver": approver,
		"approver_name": _user_full_name(approver),
		"event_time": str(ev.event_time),
	}


@frappe.whitelist(methods=["POST"])
def trigger_no_sale(
	pos_profile: str,
	reason: str,
	note: str = "",
	approval_token: str | None = None,
	pos_opening_entry: str | None = None,
) -> dict[str, Any]:
	"""Audited No Sale cash drawer kick with reason and Manager PIN when not allowed alone."""
	profile = frappe.get_doc("POS Profile", pos_profile)
	_check_profile_access(profile)
	opening = _resolve_open_shift(profile, pos_opening_entry=pos_opening_entry, required=True)
	check_drawer_collision(profile, opening.name)

	clean_reason, clean_note = _validate_reason_and_note(
		reason, note, missing_code="NO-SALE-REASON", note_code="NO-SALE-NOTE"
	)

	claim = {
		"action": "no_sale",
		"pos_profile": profile.name,
		"pos_opening_entry": opening.name,
		"reason": clean_reason,
		"note": clean_note,
	}
	approval = _require_till_privilege(
		profile,
		"no_sale",
		opening.name,
		claim,
		token=approval_token,
		allow_cashier_default=False,
	)
	approver = approval.approver if approval else None
	ev = _insert_drawer_event(
		event_type="No Sale",
		profile=profile,
		opening_entry=opening.name,
		approver=approver,
		reason=clean_reason,
		note=clean_note,
		drawer_kick_sent=1,
	)
	no_sale_count = frappe.db.count(
		"POS Drawer Event", {"pos_opening_entry": opening.name, "event_type": "No Sale"}
	)
	return {
		"event_name": ev.name,
		"event_type": "No Sale",
		"pos_opening_entry": opening.name,
		"reason": clean_reason,
		"note": clean_note,
		"operator": ev.operator,
		"approver": approver,
		"approver_name": _user_full_name(approver),
		"drawer_kick_sent": 1,
		"drawer_kick_hex": DRAWER_KICK_HEX,
		"drawer_kick_command": DRAWER_KICK_COMMAND,
		"no_sale_count_for_shift": cint(no_sale_count),
		"qz_print": cint(profile.get("qz_print")),
		"qz_host": profile.get("qz_host") or "",
		"event_time": str(ev.event_time),
	}


@frappe.whitelist(methods=["GET"])
def get_shift_x_report(pos_profile: str, pos_opening_entry: str | None = None) -> dict[str, Any]:
	"""Mid-shift X-report snapshot: No Sale count, cash movements, threshold exceptions, open checks."""
	profile = frappe.get_doc("POS Profile", pos_profile)
	_check_profile_access(profile)
	opening = _resolve_open_shift(profile, pos_opening_entry=pos_opening_entry, required=True)
	currency = profile.get("currency") or frappe.db.get_value("Company", profile.company, "default_currency")
	blind = _is_blind_masked_for_user(profile)

	no_sales = frappe.get_all(
		"POS Drawer Event",
		filters={"pos_opening_entry": opening.name, "event_type": "No Sale"},
		fields=["name", "operator", "approver", "reason", "note", "event_time"],
		order_by="event_time desc",
		limit_page_length=200,
	)
	exceptions = frappe.get_all(
		"POS Drawer Event",
		filters={"pos_opening_entry": opening.name, "event_type": "Threshold Exception"},
		fields=["name", "operator", "approver", "reason", "threshold_amount", "event_time"],
		order_by="event_time desc",
		limit_page_length=200,
	)
	jes = frappe.get_all(
		"Journal Entry",
		filters={"custom_pos_opening_entry": opening.name, "docstatus": ["in", [0, 1]]},
		fields=[
			"name",
			"custom_cash_movement_type",
			"custom_cash_movement_reason",
			"custom_cash_movement_reference",
			"custom_cash_movement_approver",
			"total_debit",
			"docstatus",
			"owner",
			"creation",
		],
		order_by="creation desc",
		limit_page_length=200,
	)
	drawer_accounts = _drawer_accounts(profile, profile.company)
	net_by_acct = till_cash_movements(opening.name, currency=currency)
	net_drawer_movement = sum(flt(v) for acct, v in net_by_acct.items() if acct in drawer_accounts)
	precision = currency_precision(currency)
	net_drawer_movement = int(flt(net_drawer_movement, 0)) if precision == 0 else flt(net_drawer_movement, precision)

	open_checks = frappe.get_all(
		"POS Invoice",
		filters={"pos_profile": profile.name, "docstatus": 0},
		fields=["name", "customer", "restaurant_table", "grand_total"],
		limit_page_length=200,
	)
	expected_cash = None if blind else _compute_expected_drawer_cash(profile, opening)
	return {
		"pos_profile": profile.name,
		"pos_opening_entry": opening.name,
		"opening_user": opening.user,
		"currency": currency,
		"blind_close": blind,
		"expected_drawer_cash": expected_cash,
		"net_drawer_movement": net_drawer_movement,
		"no_sale_count": len(no_sales),
		"no_sale_events": no_sales,
		"threshold_exception_count": len(exceptions),
		"threshold_exceptions": exceptions,
		"movements": jes,
		"open_checks_count": len(open_checks),
		"open_checks": open_checks,
	}


def _can_perform_over_short_solo(user: str, profile, opening_user: str | None, max_abs_diff: float) -> bool:
	"""Whether ``user`` may submit an over-tolerance closing variance without a second-person PIN.

	- When POS Privilege Rule rows exist for the company: requires an active `over_short_close`
	  rule with `may_perform=1` covering ``max_abs_diff``, AND either `allow_self_approval=1` or
	  `user != opening_user` (so a cashier/manager never self-approves their own till variance
	  unless explicitly allowed by policy).
	- When no POS Privilege Rule rows exist (unconfigured site): keeps legacy manager roles usable
	  while ALWAYS requiring a manager PIN for non-managers (e.g. URY Cashier).
	"""
	if not user or user == "Guest":
		return False
	override_mod = _load_override_module()
	is_non_interactive = getattr(override_mod, "_is_non_interactive_or_agent_session", None) if override_mod else None
	if callable(is_non_interactive) and is_non_interactive(user):
		return False
	if override_mod and _has_configured_rules(profile.company, "over_short_close"):
		code = _resolve_privilege_code(override_mod, "over_short_close")
		if not override_mod.may_perform(user, profile.name, code, amount=max_abs_diff):
			return False
		if opening_user and user.casefold() == opening_user.casefold():
			# Respect allow_self_approval on the matching rule (default 0 blocks self-approval of own till)
			roles = list(frappe.get_roles(user))
			rules = frappe.get_all(
				"POS Privilege Rule",
				filters={
					"company": profile.company,
					"privilege": code,
					"role": ["in", roles],
					"pos_profile": ["in", [profile.name, "", None]],
				},
				fields=["allow_self_approval", "may_perform"],
			)
			if not any(cint(r.get("may_perform")) == 1 and cint(r.get("allow_self_approval")) == 1 for r in rules):
				return False
		return True

	roles = set(frappe.get_roles(user))
	return user == "Administrator" or bool(LEGACY_MANAGER_ROLES & roles)


def _cash_variances_above_tolerance(doc, profile):
	"""Return (exceeding_list, tolerance, precision) for Cash reconciliation rows beyond tolerance."""
	currency = profile.get("currency") or frappe.db.get_value("Company", profile.company, "default_currency")
	precision = currency_precision(currency)
	tolerance = flt(profile.get("custom_over_short_tolerance"), precision)
	if tolerance < 0:
		tolerance = 0.0
	exceeding = []
	for row in doc.get("payment_reconciliation") or []:
		if frappe.get_cached_value("Mode of Payment", row.mode_of_payment, "type") != "Cash":
			continue
		diff = flt(row.difference, precision)
		if abs(diff) > tolerance + 1e-9:
			exceeding.append((row.mode_of_payment, diff))
	return exceeding, tolerance, precision


def _is_concurrent_row_change_error(exc: BaseException) -> bool:
	"""Detect MariaDB REPEATABLE-READ concurrent row-change / deadlock errors (errno 1020 / 1213)."""
	deadlock_cls = getattr(frappe, "QueryDeadlockError", None)
	if isinstance(deadlock_cls, type) and isinstance(exc, deadlock_cls):
		return True
	exc_mod = getattr(frappe, "exceptions", None)
	mod_deadlock = getattr(exc_mod, "QueryDeadlockError", None) if exc_mod else None
	if isinstance(mod_deadlock, type) and isinstance(exc, mod_deadlock):
		return True
	args = getattr(exc, "args", ()) or ()
	if args and args[0] in (1020, 1213):
		return True
	text = str(exc)
	return "1020" in text and "Record has changed since last read" in text


def _raise_already_closed_on_deadlock(pos_opening_entry: str) -> None:
	rollback_fn = getattr(frappe.db, "rollback", None)
	if callable(rollback_fn):
		rollback_fn()
	frappe.throw(
		coded_message(
			"CLOSE-ALREADY-CLOSED",
			_("This shift ({0}) is already closed.").format(pos_opening_entry),
		)
	)


def _lock_opening_entry_for_close(pos_opening_entry: str, fields: list[str]):
	try:
		return frappe.db.get_value(
			"POS Opening Entry",
			pos_opening_entry,
			fields,
			as_dict=True,
			for_update=True,
		)
	except Exception as exc:
		if _is_concurrent_row_change_error(exc):
			_raise_already_closed_on_deadlock(pos_opening_entry)
		raise


def _blind_safe_variance_refusal(profile, tolerance: float) -> None:
	blind = _is_blind_masked_for_user(profile)
	if blind:
		msg = _(
			"Counted cash is outside this till's allowed variance. "
			"Recount the drawer or ask a manager to approve with their PIN and a reason."
		)
	else:
		msg = _(
			"Cash variance exceeds the till's allowed tolerance ({0}). "
			"Enter a reason and get manager PIN approval."
		).format(tolerance)
	frappe.throw(coded_message("CLOSE-VARIANCE-APPROVAL", msg), frappe.PermissionError)


def validate_closing_controls(doc, method=None) -> None:
	"""POS Closing Entry before_submit gate (runs for POS API, Desk, REST, and offline replay).

	Enforces:
	1. FOR UPDATE lock on POS Opening Entry + refuse closing an already-closed shift (no double close).
	2. Non-negative closing amounts.
	3. Close Another Person's Till (`close_other_till`) privilege / PIN gate.
	4. Over/Short variance tolerance (`over_short_close`) privilege / PIN + reason gate,
	   strictly hiding expected/variance figures in blind close mode.
	5. Server-stamped `custom_no_sale_count` for X/Z reports.
	"""
	for row in doc.get("payment_reconciliation") or []:
		if flt(row.get("closing_amount")) < 0:
			frappe.throw(
				coded_message(
					"CLOSE-NEGATIVE-AMOUNT",
					_("Counted closing amount for {0} cannot be negative.").format(row.get("mode_of_payment") or _("tender")),
				)
			)

	opening_name = doc.get("pos_opening_entry")
	if not opening_name:
		return

	# Lock the POS Opening Entry row to serialize concurrent close & settle and prevent double close
	opening = _lock_opening_entry_for_close(
		opening_name,
		["name", "status", "docstatus", "user", "pos_profile", "company"],
	)
	if not opening:
		return

	if cint(opening.docstatus) != 1 or opening.status != "Open":
		frappe.throw(
			coded_message(
				"CLOSE-ALREADY-CLOSED",
				_("This shift ({0}) is already closed.").format(opening_name),
			)
		)

	existing_closing = frappe.get_all(
		"POS Closing Entry",
		filters={"pos_opening_entry": opening_name, "docstatus": 1},
		pluck="name",
		limit_page_length=2,
	)
	other_submitted = [name for name in existing_closing if name != doc.get("name")]
	if other_submitted:
		frappe.throw(
			coded_message(
				"CLOSE-ALREADY-CLOSED",
				_("This shift ({0}) already has a submitted closing ({1}).").format(opening_name, other_submitted[0]),
			)
		)

	profile_name = doc.get("pos_profile") or opening.pos_profile
	if not profile_name or not frappe.db.exists("POS Profile", profile_name):
		return
	profile = frappe.get_doc("POS Profile", profile_name)
	flags = getattr(doc, "flags", None)

	# Gate: closing another person's till
	session_user = frappe.session.user
	holder = opening.user or doc.get("user")
	if holder and session_user and holder.casefold() != session_user.casefold():
		close_other_approval = getattr(flags, "close_other_approval", None)
		if isinstance(close_other_approval, dict) and close_other_approval.get("approver"):
			doc.custom_close_approver = close_other_approval["approver"]
		elif not _may_perform_till_privilege(session_user, profile, "close_other_till"):
			frappe.throw(
				coded_message("AUTH-REQUIRED", _("Manager PIN approval is required to close another person's till.")),
				frappe.PermissionError,
			)

	# Gate: Over/Short variance above till tolerance
	exceeding, tolerance, _precision = _cash_variances_above_tolerance(doc, profile)
	if exceeding:
		max_abs_diff = max(abs(diff) for _mode, diff in exceeding)
		over_short_approval = getattr(flags, "over_short_approval", None)
		if isinstance(over_short_approval, dict) and over_short_approval.get("token_consumed") and over_short_approval.get("approver"):
			approver = str(over_short_approval["approver"]).strip()
			excluded = {(session_user or "").casefold(), (holder or "").casefold()}
			if approver.casefold() in excluded:
				frappe.throw(
					coded_message("AUTH-SELF", _("You cannot approve your own over/short variance. Ask another manager.")),
					frappe.PermissionError,
				)
			if not (doc.get("custom_over_short_reason") or "").strip():
				frappe.throw(
					coded_message("CLOSE-OVER-SHORT-REASON", _("Enter an Over/Short reason when variance exceeds tolerance."))
				)
			doc.custom_over_short_approver = approver
		elif not _can_perform_over_short_solo(session_user, profile, holder, max_abs_diff):
			blind = _is_blind_masked_for_user(profile, session_user)
			if blind:
				msg = _(
					"Counted cash is outside this till's allowed variance. "
					"Recount the drawer or ask a manager to approve with their PIN and a reason."
				)
			else:
				msg = _(
					"Cash variance exceeds the till's allowed tolerance ({0}). "
					"Enter a reason and get manager PIN approval."
				).format(tolerance)
			frappe.throw(coded_message("CLOSE-VARIANCE-APPROVAL", msg), frappe.PermissionError)

	# Server-stamp shift No Sale count for X/Z reports
	if frappe.db.table_exists("POS Drawer Event"):
		doc.custom_no_sale_count = cint(
			frappe.db.count("POS Drawer Event", {"pos_opening_entry": opening_name, "event_type": "No Sale"})
		)


def validate_closing_cancel(doc, method=None) -> None:
	"""POS Closing Entry before_cancel gate: reopening a closed till requires `reopen_till` privilege or PIN."""
	profile_name = doc.get("pos_profile")
	if not profile_name or not frappe.db.exists("POS Profile", profile_name):
		return
	profile = frappe.get_doc("POS Profile", profile_name)
	flags = getattr(doc, "flags", None)
	reopen_approval = getattr(flags, "reopen_till_approval", None)
	if isinstance(reopen_approval, dict) and reopen_approval.get("token_consumed") and reopen_approval.get("approver"):
		return
	if _may_perform_till_privilege(frappe.session.user, profile, "reopen_till"):
		return
	frappe.throw(
		coded_message("AUTH-REQUIRED", _("Manager PIN approval is required to reopen a closed till.")),
		frappe.PermissionError,
	)


def _parse_counted_payments(counted_payments: Any, currency: str | None) -> list[dict[str, Any]]:
	if isinstance(counted_payments, str):
		counted_payments = frappe.parse_json(counted_payments) or []
	if not isinstance(counted_payments, (list, tuple)):
		frappe.throw(coded_message("CLOSE-INVALID-COUNT", _("Provide counted closing amounts per payment mode.")))
	precision = currency_precision(currency)
	parsed = []
	for row in counted_payments:
		if not isinstance(row, dict):
			continue
		mode = (row.get("mode_of_payment") or "").strip()
		if not mode:
			continue
		raw = row.get("closing_amount", 0)
		if isinstance(raw, bool) or raw is None or raw == "":
			val = 0.0
		else:
			try:
				val = float(raw)
			except (TypeError, ValueError):
				frappe.throw(coded_message("CLOSE-INVALID-COUNT", _("Counted amount for {0} must be a number.").format(mode)))
		if not math.isfinite(val):
			frappe.throw(coded_message("CLOSE-INVALID-COUNT", _("Counted amount for {0} must be finite.").format(mode)))
		if val < 0:
			frappe.throw(
				coded_message("CLOSE-NEGATIVE-AMOUNT", _("Counted closing amount for {0} cannot be negative.").format(mode))
			)
		rounded = flt(val, precision)
		if abs(val - rounded) > 1e-6:
			frappe.throw(
				coded_message(
					"CASH-PRECISION",
					_("{0} amounts must match currency decimal precision ({1}).").format(currency or _("Currency"), precision),
				)
			)
		amt = int(rounded) if precision == 0 else rounded
		parsed.append({"mode_of_payment": mode, "closing_amount": amt})
	return parsed


@frappe.whitelist(methods=["GET"])
def list_open_tills(pos_profile: str) -> list[dict[str, Any]]:
	"""List all open shifts for ``pos_profile`` (no financial totals exposed; safe for blind mode)."""
	profile = frappe.get_doc("POS Profile", pos_profile)
	_check_profile_access(profile)
	rows = frappe.get_all(
		"POS Opening Entry",
		filters={"pos_profile": profile.name, "docstatus": 1, "status": "Open"},
		fields=["name", "pos_profile", "user", "period_start_date", "posting_date"],
		order_by="period_start_date desc",
		limit_page_length=50,
	)
	current = (frappe.session.user or "").casefold()
	return [
		{
			"name": r.name,
			"pos_profile": r.pos_profile,
			"user": r.user,
			"user_name": _user_full_name(r.user),
			"period_start_date": str(r.period_start_date or ""),
			"posting_date": str(r.posting_date or ""),
			"is_own": (r.user or "").casefold() == current,
		}
		for r in rows
	]


@frappe.whitelist(methods=["GET"])
def list_closed_tills(pos_profile: str, limit: int = 20) -> list[dict[str, Any]]:
	"""List recent submitted POS Closing Entries for ``pos_profile`` (for Reopen Till UI)."""
	profile = frappe.get_doc("POS Profile", pos_profile)
	_check_profile_access(profile)
	rows = frappe.get_all(
		"POS Closing Entry",
		filters={"pos_profile": profile.name, "docstatus": 1},
		fields=[
			"name",
			"pos_opening_entry",
			"user",
			"posting_date",
			"period_start_date",
			"period_end_date",
			"status",
			"custom_force_closed",
			"custom_no_sale_count",
		],
		order_by="period_end_date desc, creation desc",
		limit_page_length=min(max(1, cint(limit)), 50),
	)
	return [
		{
			"name": r.name,
			"pos_opening_entry": r.pos_opening_entry,
			"user": r.user,
			"user_name": _user_full_name(r.user),
			"posting_date": str(r.posting_date or ""),
			"period_start_date": str(r.period_start_date or ""),
			"period_end_date": str(r.period_end_date or ""),
			"status": r.status or "Submitted",
			"force_closed": bool(cint(r.get("custom_force_closed"))),
			"no_sale_count": cint(r.get("custom_no_sale_count")),
		}
		for r in rows
	]


@frappe.whitelist(methods=["GET"])
def get_till_close_preview(pos_profile: str, pos_opening_entry: str | None = None) -> dict[str, Any]:
	"""Return shift close metadata & rows; strictly strips expected cash & sales totals in blind mode."""
	profile = frappe.get_doc("POS Profile", pos_profile)
	_check_profile_access(profile)
	opening = _resolve_open_shift(profile, pos_opening_entry=pos_opening_entry, required=True)
	currency = profile.get("currency") or frappe.db.get_value("Company", profile.company, "default_currency")
	blind = _is_blind_masked_for_user(profile)
	precision = currency_precision(currency)

	opening_doc = frappe.get_doc("POS Opening Entry", opening.name)
	opening_by_mode = {}
	for row in opening_doc.get("balance_details") or []:
		opening_by_mode[row.mode_of_payment] = flt(opening_by_mode.get(row.mode_of_payment, 0) + flt(row.opening_amount), precision)

	expected_cash = _compute_expected_drawer_cash(profile, opening)
	open_checks = frappe.get_all(
		"POS Invoice",
		filters={"pos_profile": profile.name, "docstatus": 0},
		fields=["name", "customer", "restaurant_table", "grand_total"],
		limit_page_length=200,
	)
	pending_movements = frappe.get_all(
		"Journal Entry",
		filters={"custom_pos_opening_entry": opening.name, "docstatus": 0},
		fields=["name", "custom_cash_movement_type", "total_debit", "custom_cash_movement_reason"],
		limit_page_length=50,
	)
	no_sale_count = frappe.db.count(
		"POS Drawer Event", {"pos_opening_entry": opening.name, "event_type": "No Sale"}
	)

	payment_rows = []
	for p in profile.get("payments") or []:
		mode = p.mode_of_payment
		is_cash = frappe.get_cached_value("Mode of Payment", mode, "type") == "Cash"
		payment_rows.append(
			{
				"mode_of_payment": mode,
				"is_cash": is_cash,
				"opening_amount": opening_by_mode.get(mode, 0),
				"expected_amount": None if blind else (expected_cash if is_cash else opening_by_mode.get(mode, 0)),
			}
		)

	grand_total = None
	if not blind:
		invs = frappe.get_all(
			"POS Invoice",
			filters={"pos_profile": profile.name, "docstatus": 1, "consolidated_invoice": ["is", "not set"]},
			fields=["grand_total"],
			limit_page_length=0,
		)
		grand_total = flt(sum(flt(i.grand_total) for i in invs), precision)

	return {
		"pos_profile": profile.name,
		"pos_opening_entry": opening.name,
		"opening_user": opening.user,
		"opening_user_name": _user_full_name(opening.user),
		"is_other_till": (opening.user or "").casefold() != (frappe.session.user or "").casefold(),
		"currency": currency,
		"blind_close": blind,
		"over_short_tolerance": flt(profile.get("custom_over_short_tolerance"), precision),
		"grand_total": grand_total,
		"no_sale_count": cint(no_sale_count),
		"open_checks_count": len(open_checks),
		"open_checks": open_checks,
		"pending_movements_count": len(pending_movements),
		"pending_movements": pending_movements,
		"payment_rows": payment_rows,
	}


@frappe.whitelist(methods=["POST"])
def close_till_with_controls(
	pos_profile: str,
	pos_opening_entry: str,
	counted_payments: Any,
	over_short_reason: str | None = None,
	over_short_approval_token: str | None = None,
	close_other_approval_token: str | None = None,
	close_other_reason: str | None = None,
	period_end_date: str | None = None,
) -> dict[str, Any]:
	"""Close an open till through native POS Closing Entry with Over/Short tolerance PIN,
	blind-close protection, close-another-till PIN, and FOR UPDATE lock against double-close."""
	profile = frappe.get_doc("POS Profile", pos_profile)
	_check_profile_access(profile)
	currency = profile.get("currency") or frappe.db.get_value("Company", profile.company, "default_currency")
	parsed_counts = _parse_counted_payments(counted_payments, currency)

	# Lock opening entry before checking status so two concurrent closes never both proceed
	opening = _lock_opening_entry_for_close(
		pos_opening_entry,
		["name", "pos_profile", "company", "user", "status", "docstatus", "period_start_date"],
	)
	if not opening or opening.pos_profile != profile.name or opening.company != profile.company:
		frappe.throw(coded_message("CASH-SHIFT", _("POS shift not found on {0}.").format(profile.name)))
	if cint(opening.docstatus) != 1 or opening.status != "Open":
		frappe.throw(coded_message("CLOSE-ALREADY-CLOSED", _("This shift ({0}) is already closed.").format(opening.name)))

	# Fail fast when closing another person's till without permission or PIN token,
	# but DEFER override.consume(close_other_approval_token) until after variance checks
	# succeed so blind-close variance refusals never prematurely burn close_other_approval_token.
	is_other_till = (opening.user or "").casefold() != (frappe.session.user or "").casefold()
	if is_other_till and not close_other_approval_token:
		if not _may_perform_till_privilege(
			frappe.session.user,
			profile,
			"close_other_till",
			allow_cashier_default=False,
		):
			frappe.throw(
				coded_message("AUTH-REQUIRED", _("A manager must approve this action with their PIN.")),
				frappe.PermissionError,
			)

	end_dt = get_datetime(period_end_date) if period_end_date else now_datetime()
	closing = frappe.new_doc("POS Closing Entry")
	closing.pos_opening_entry = opening.name
	closing.pos_profile = profile.name
	closing.company = profile.company
	# Native ERPNext rule: closing.user MUST equal opening.user (till holder)
	closing.user = opening.user
	closing.period_start_date = opening.period_start_date
	closing.period_end_date = end_dt.strftime("%Y-%m-%d %H:%M:%S.%f")
	closing.posting_date = end_dt.strftime("%Y-%m-%d")
	closing.posting_time = end_dt.strftime("%H:%M:%S.%f")
	for row in parsed_counts:
		closing.append(
			"payment_reconciliation",
			{"mode_of_payment": row["mode_of_payment"], "closing_amount": row["closing_amount"]},
		)
	closing.flags.ignore_permissions = True
	try:
		closing.insert(ignore_permissions=True)
	except Exception as exc:
		if _is_concurrent_row_change_error(exc):
			_raise_already_closed_on_deadlock(opening.name)
		raise

	# Evaluate server-computed cash variances against till tolerance BEFORE consuming any token
	exceeding, tolerance, _precision = _cash_variances_above_tolerance(closing, profile)
	clean_os_reason = (over_short_reason or "").strip()
	max_abs_diff = 0.0
	if exceeding:
		max_abs_diff = max(abs(diff) for _m, diff in exceeding)
		if over_short_approval_token:
			if not clean_os_reason:
				frappe.throw(
					coded_message("CLOSE-OVER-SHORT-REASON", _("Enter an Over/Short reason when variance exceeds tolerance."))
				)
		elif not _can_perform_over_short_solo(frappe.session.user, profile, opening.user, max_abs_diff):
			_blind_safe_variance_refusal(profile, tolerance)

	# All pre-checks passed: now consume approval tokens atomically right before submit.
	over_short_approval = None
	if exceeding and over_short_approval_token:
		os_claim = {
			"action": "over_short_close",
			"pos_profile": profile.name,
			"pos_opening_entry": opening.name,
			"counted_payments": parsed_counts,
			"reason": clean_os_reason,
		}
		over_short_approval = _require_till_privilege(
			profile,
			"over_short_close",
			opening.name,
			os_claim,
			token=over_short_approval_token,
			amount=max_abs_diff,
			require_second_person=True,
			excluded_approvers={frappe.session.user, opening.user},
		)

	close_other_approval = None
	if is_other_till and close_other_approval_token:
		other_claim = {
			"action": "close_other_till",
			"pos_profile": profile.name,
			"pos_opening_entry": opening.name,
			"opening_user": opening.user,
		}
		clean_other_reason = (close_other_reason or "").strip()
		if clean_other_reason:
			other_claim["reason"] = clean_other_reason
		close_other_approval = _require_till_privilege(
			profile,
			"close_other_till",
			opening.name,
			other_claim,
			token=close_other_approval_token,
			allow_cashier_default=False,
		)

	if clean_os_reason:
		closing.custom_over_short_reason = clean_os_reason
	if over_short_approval:
		closing.flags.over_short_approval = {
			"approver": over_short_approval.approver,
			"token_consumed": True,
		}
	if close_other_approval:
		closing.flags.close_other_approval = {
			"approver": close_other_approval.approver,
			"token_consumed": True,
		}

	closing.flags.ignore_permissions = True
	prev_ignore = bool(getattr(frappe.flags, "ignore_permissions", False))
	try:
		frappe.flags.ignore_permissions = True
		closing.submit()
	except Exception as exc:
		if _is_concurrent_row_change_error(exc):
			_raise_already_closed_on_deadlock(opening.name)
		raise
	finally:
		frappe.flags.ignore_permissions = prev_ignore
	closing.reload()
	return {
		"name": closing.name,
		"pos_opening_entry": opening.name,
		"docstatus": cint(closing.docstatus),
		"user": closing.user,
		"over_short_approver": closing.get("custom_over_short_approver"),
		"over_short_reason": closing.get("custom_over_short_reason"),
		"close_approver": closing.get("custom_close_approver"),
		"no_sale_count": cint(closing.get("custom_no_sale_count")),
	}


@frappe.whitelist(methods=["POST"])
def reopen_till(
	pos_closing_entry: str,
	reason: str,
	approval_token: str | None = None,
) -> dict[str, Any]:
	"""Reopen a closed till by cancelling its POS Closing Entry natively (unconsolidating once
	and restoring POS Opening Entry to Open), guarded by `reopen_till` Manager PIN + locks."""
	clean_reason = (reason or "").strip()
	if not clean_reason:
		frappe.throw(coded_message("REOPEN-REASON", _("Enter a reason before reopening a closed till.")))

	closing_meta = frappe.db.get_value(
		"POS Closing Entry",
		pos_closing_entry,
		["name", "pos_profile", "pos_opening_entry", "company", "user", "docstatus", "status"],
		as_dict=True,
		for_update=True,
	)
	if not closing_meta:
		frappe.throw(coded_message("REOPEN-NOT-FOUND", _("POS Closing Entry not found.")))
	if cint(closing_meta.docstatus) != 1:
		frappe.throw(coded_message("REOPEN-NOT-CLOSED", _("Only a submitted POS Closing Entry can be reopened.")))
	if (closing_meta.status or "").strip() == "Queued":
		frappe.throw(
			coded_message("REOPEN-IN-PROGRESS", _("Consolidation is still running for this closing. Try again shortly."))
		)

	profile = frappe.get_doc("POS Profile", closing_meta.pos_profile)
	_check_profile_access(profile)

	# Lock the POS Opening Entry row and verify no conflicting open shift exists
	opening_meta = frappe.db.get_value(
		"POS Opening Entry",
		closing_meta.pos_opening_entry,
		["name", "status", "docstatus", "user", "pos_profile"],
		as_dict=True,
		for_update=True,
	)
	if not opening_meta:
		frappe.throw(coded_message("CASH-SHIFT", _("Linked POS Opening Entry not found.")))

	other_open = frappe.get_all(
		"POS Opening Entry",
		filters={"pos_profile": profile.name, "user": opening_meta.user, "docstatus": 1, "status": "Open"},
		pluck="name",
		limit_page_length=2,
	)
	if any(name != opening_meta.name for name in other_open):
		frappe.throw(
			coded_message(
				"REOPEN-CONFLICT",
				_("Another shift is already open for this cashier on {0}. Close it before reopening {1}.").format(
					profile.name, closing_meta.name
				),
			)
		)

	# Refuse reopening if any consolidated Sales Invoice has already been fiscalised (EFRIS).
	# Guard column existence so non-Uganda sites without uganda_compliance don't hit Unknown column 1054.
	has_col = getattr(frappe.db, "has_column", None)
	efris_cols = [
		col
		for col in ("custom_efris_fdn", "custom_efris_irn")
		if (has_col("Sales Invoice", col) if callable(has_col) else True)
	]
	if efris_cols:
		consolidated_sis = frappe.get_all(
			"Sales Invoice",
			filters={"pos_closing_entry": closing_meta.name, "docstatus": 1},
			fields=["name", *efris_cols],
			limit_page_length=50,
		)
		for si in consolidated_sis:
			if any(si.get(col) for col in efris_cols):
				frappe.throw(
					coded_message(
						"REOPEN-LOCKED",
						_("Consolidated invoice {0} is already fiscalised and cannot be reopened.").format(si.name),
					)
				)

	claim = {
		"action": "reopen_till",
		"pos_profile": profile.name,
		"pos_closing_entry": closing_meta.name,
		"pos_opening_entry": opening_meta.name,
		"reason": clean_reason,
	}
	approval = _require_till_privilege(
		profile,
		"reopen_till",
		closing_meta.name,
		claim,
		token=approval_token,
		allow_cashier_default=False,
	)
	approver = approval.approver if approval else None

	closing_doc = frappe.get_doc("POS Closing Entry", closing_meta.name)
	closing_doc.flags.reopen_till_approval = {
		"approver": approver or frappe.session.user,
		"token_consumed": bool(approval),
	}
	closing_doc.flags.ignore_permissions = True
	orig_user = frappe.session.user
	set_user_fn = getattr(frappe, "set_user", None)
	try:
		if callable(set_user_fn):
			set_user_fn("Administrator")
		if frappe.session.user != "Administrator":
			frappe.session.user = "Administrator"
		closing_doc.cancel()
	finally:
		if callable(set_user_fn):
			set_user_fn(orig_user)
		if frappe.session.user != orig_user:
			frappe.session.user = orig_user

	ev = _insert_drawer_event(
		event_type="Till Reopen",
		profile=profile,
		opening_entry=opening_meta.name,
		closing_entry=closing_meta.name,
		approver=approver,
		reason=clean_reason,
	)
	return {
		"event_name": ev.name,
		"pos_closing_entry": closing_meta.name,
		"pos_opening_entry": opening_meta.name,
		"status": "Open",
		"approver": approver,
		"approver_name": _user_full_name(approver),
		"reason": clean_reason,
	}


@frappe.whitelist(methods=["POST"])
def force_close_till(
	pos_profile: str,
	pos_opening_entry: str,
	counted_payments: Any,
	reason: str,
	discard_pending_movements: Any = False,
	approval_token: str | None = None,
	over_short_reason: str | None = None,
	over_short_approval_token: str | None = None,
	period_end_date: str | None = None,
) -> dict[str, Any]:
	"""Manager-PIN Force Close: snapshots open draft checks, optionally discards pending draft
	cash movements when explicitly confirmed, and submits native POS Closing Entry so every
	submitted sale is consolidated once and nothing is lost."""
	clean_reason = (reason or "").strip()
	if not clean_reason:
		frappe.throw(coded_message("FORCE-CLOSE-REASON", _("Enter a reason before force-closing a till.")))

	profile = frappe.get_doc("POS Profile", pos_profile)
	_check_profile_access(profile)
	currency = profile.get("currency") or frappe.db.get_value("Company", profile.company, "default_currency")
	parsed_counts = _parse_counted_payments(counted_payments, currency)
	discard_pending = bool(cint(discard_pending_movements)) if not isinstance(discard_pending_movements, bool) else discard_pending_movements

	opening = _lock_opening_entry_for_close(
		pos_opening_entry,
		["name", "pos_profile", "company", "user", "status", "docstatus", "period_start_date"],
	)
	if not opening or opening.pos_profile != profile.name or opening.company != profile.company:
		frappe.throw(coded_message("CASH-SHIFT", _("POS shift not found on {0}.").format(profile.name)))
	if cint(opening.docstatus) != 1 or opening.status != "Open":
		frappe.throw(coded_message("CLOSE-ALREADY-CLOSED", _("This shift ({0}) is already closed.").format(opening.name)))

	# Fail fast when neither force-close token nor solo privilege exists,
	# but defer consuming approval_token until variance/over_short_close gates succeed.
	if not approval_token and not _may_perform_till_privilege(
		frappe.session.user,
		profile,
		"reopen_till",
		fallback_privilege="close_other_till",
		allow_cashier_default=False,
	):
		frappe.throw(
			coded_message("AUTH-REQUIRED", _("A manager must approve this action with their PIN.")),
			frappe.PermissionError,
		)

	end_dt = get_datetime(period_end_date) if period_end_date else now_datetime()
	closing = frappe.new_doc("POS Closing Entry")
	closing.pos_opening_entry = opening.name
	closing.pos_profile = profile.name
	closing.company = profile.company
	closing.user = opening.user
	closing.period_start_date = opening.period_start_date
	closing.period_end_date = end_dt.strftime("%Y-%m-%d %H:%M:%S.%f")
	closing.posting_date = end_dt.strftime("%Y-%m-%d")
	closing.posting_time = end_dt.strftime("%H:%M:%S.%f")
	closing.custom_force_closed = 1
	closing.custom_force_close_reason = clean_reason
	for row in parsed_counts:
		closing.append(
			"payment_reconciliation",
			{"mode_of_payment": row["mode_of_payment"], "closing_amount": row["closing_amount"]},
		)
	closing.flags.ignore_permissions = True
	closing.flags.pos_force_close = True
	try:
		closing.insert(ignore_permissions=True)
	except Exception as exc:
		if _is_concurrent_row_change_error(exc):
			_raise_already_closed_on_deadlock(opening.name)
		raise

	# Enforce over_short_close privilege & max_amount when variance exceeds tolerance —
	# NEVER stamp closing.flags.over_short_approval unchecked from a reopen_till force-close PIN.
	exceeding, tolerance, _prec = _cash_variances_above_tolerance(closing, profile)
	os_app = None
	effective_os_reason = ""
	if exceeding:
		max_abs_diff = max(abs(d) for _m, d in exceeding)
		effective_os_reason = (over_short_reason or "").strip() or clean_reason
		if not over_short_approval_token and not _can_perform_over_short_solo(
			frappe.session.user, profile, opening.user, max_abs_diff
		):
			_blind_safe_variance_refusal(profile, tolerance)

	# All pre-checks passed: now consume over_short and force_close approval tokens.
	if exceeding and over_short_approval_token:
		os_claim = {
			"action": "over_short_close",
			"pos_profile": profile.name,
			"pos_opening_entry": opening.name,
			"counted_payments": parsed_counts,
			"reason": effective_os_reason,
		}
		os_app = _require_till_privilege(
			profile,
			"over_short_close",
			opening.name,
			os_claim,
			token=over_short_approval_token,
			amount=max(abs(d) for _m, d in exceeding),
			require_second_person=True,
			excluded_approvers={frappe.session.user, opening.user},
		)

	claim = {
		"action": "force_close",
		"pos_profile": profile.name,
		"pos_opening_entry": opening.name,
		"reason": clean_reason,
		"discard_pending_movements": discard_pending,
	}
	approval = _require_till_privilege(
		profile,
		"reopen_till",
		opening.name,
		claim,
		token=approval_token,
		fallback_privilege="close_other_till",
		allow_cashier_default=False,
	)
	approver = approval.approver if approval else None

	open_checks = frappe.get_all(
		"POS Invoice",
		filters={"pos_profile": profile.name, "docstatus": 0},
		pluck="name",
		limit_page_length=200,
	)
	discarded_names = []
	if discard_pending:
		pending_jes = frappe.get_all(
			"Journal Entry",
			filters={"custom_pos_opening_entry": opening.name, "docstatus": 0},
			pluck="name",
			limit_page_length=50,
		)
		for je_name in pending_jes:
			frappe.delete_doc("Journal Entry", je_name, ignore_permissions=True)
			discarded_names.append(je_name)

	closing.custom_close_approver = approver or frappe.session.user
	if effective_os_reason:
		closing.custom_over_short_reason = effective_os_reason
	if os_app:
		closing.flags.over_short_approval = {"approver": os_app.approver, "token_consumed": True}

	closing.flags.pos_force_close = True
	closing.flags.close_other_approval = {
		"approver": approver or frappe.session.user,
		"token_consumed": bool(approval),
	}
	closing.flags.ignore_permissions = True
	prev_ignore = bool(getattr(frappe.flags, "ignore_permissions", False))
	try:
		frappe.flags.ignore_permissions = True
		closing.submit()
	except Exception as exc:
		if _is_concurrent_row_change_error(exc):
			_raise_already_closed_on_deadlock(opening.name)
		raise
	finally:
		frappe.flags.ignore_permissions = prev_ignore
	closing.reload()

	note_parts = []
	if discarded_names:
		note_parts.append(f"Discarded pending movements: {', '.join(discarded_names)}")
	ev = _insert_drawer_event(
		event_type="Force Close",
		profile=profile,
		opening_entry=opening.name,
		closing_entry=closing.name,
		approver=approver,
		reason=clean_reason,
		note="; ".join(note_parts),
		open_checks_snapshot=", ".join(open_checks),
	)
	return {
		"name": closing.name,
		"event_name": ev.name,
		"pos_opening_entry": opening.name,
		"docstatus": cint(closing.docstatus),
		"force_closed": True,
		"open_checks_snapshot": open_checks,
		"discarded_pending_movements": discarded_names,
		"approver": approver,
		"approver_name": _user_full_name(approver),
	}


def lock_open_shift_for_settlement(pos_profile: str):
	"""Acquire a FOR UPDATE lock on the active POS Opening Entry for ``pos_profile`` so
	concurrent bill settlement and till closing serialize cleanly without losing consolidation."""
	if not pos_profile:
		return None
	open_names = frappe.get_all(
		"POS Opening Entry",
		filters={"pos_profile": pos_profile, "docstatus": 1, "status": "Open"},
		pluck="name",
		order_by="period_start_date desc",
		limit_page_length=1,
	)
	if not open_names:
		return None
	return frappe.db.get_value(
		"POS Opening Entry",
		open_names[0],
		["name", "status", "period_start_date"],
		as_dict=True,
		for_update=True,
	)


def validate_offline_settlement_shift(
	pos_profile: str,
	offline_captured_at: Any = None,
	client_order_id: str | None = None,
) -> None:
	"""Refuse offline settlement replay if the shift it belongs to has already closed."""
	if not client_order_id and not offline_captured_at:
		return
	open_shift = lock_open_shift_for_settlement(pos_profile)
	if not open_shift or open_shift.get("status") != "Open":
		frappe.throw(
			coded_message(
				"OFFLINE-AFTER-CLOSE",
				_(
					"Cannot replay offline settlement: the shift on {0} has already closed. "
					"Reopen the till or review this offline check with a manager."
				).format(pos_profile),
			)
		)
	if not offline_captured_at:
		return
	captured_dt = get_datetime(offline_captured_at)
	closed_rows = frappe.get_all(
		"POS Closing Entry",
		filters={"pos_profile": pos_profile, "docstatus": 1},
		fields=["name", "period_end_date"],
		order_by="period_end_date desc",
		limit_page_length=5,
	)
	for closed in closed_rows:
		if closed.get("period_end_date") and get_datetime(closed.period_end_date) >= captured_dt:
			frappe.throw(
				coded_message(
					"OFFLINE-AFTER-CLOSE",
					_(
						"Offline check was captured before shift closing {0} ({1}). "
						"Reopen that till or settle under manager review."
					).format(closed.name, closed.period_end_date),
				)
			)


def over_halo_tenders_for_invoice(doc) -> list[dict[str, Any]]:
	"""Return sorted list of ``{mode_of_payment, amount, halo}`` for tenders exceeding Mode of Payment HALO.

	Sums multiple rows of the same ``mode_of_payment`` so splitting a single mistyped tender
	across duplicate rows cannot evade HALO.
	"""
	if doc.get("is_return"):
		return []
	currency = doc.get("currency")
	if not currency and doc.get("pos_profile"):
		currency = frappe.db.get_value("POS Profile", doc.pos_profile, "currency")
	precision = currency_precision(currency)

	totals_by_mode: dict[str, float] = {}
	for row in doc.get("payments") or []:
		mode = (row.get("mode_of_payment") or "").strip()
		if not mode:
			continue
		amt = flt(row.get("amount"))
		if amt <= 0:
			continue
		totals_by_mode[mode] = flt(totals_by_mode.get(mode, 0.0) + amt, precision)

	exceeded = []
	for mode in sorted(totals_by_mode):
		total_amt = totals_by_mode[mode]
		halo = flt(frappe.db.get_value("Mode of Payment", mode, "custom_halo_max_amount"), precision)
		if halo > 0 and total_amt > halo + 1e-9:
			clean_amt = int(total_amt) if precision == 0 else total_amt
			clean_halo = int(halo) if precision == 0 else halo
			exceeded.append({"mode_of_payment": mode, "amount": clean_amt, "halo": clean_halo})
	return exceeded


def apply_halo_approval_token(doc, approval_token: str | None, reason: str | None = None):
	"""Consume a single-use `over_halo_tender` Manager PIN token and stamp server-only
	``doc.flags.over_halo_approval`` so ``ury_pos_tender.before_validate`` permits the tenders."""
	if not approval_token:
		return None
	exceeded = over_halo_tenders_for_invoice(doc)
	if not exceeded:
		return None
	pos_profile = doc.get("pos_profile")
	if not pos_profile or not frappe.db.exists("POS Profile", pos_profile):
		frappe.throw(coded_message("AUTH-REQUIRED", _("A valid POS Profile is required for HALO approval.")), frappe.PermissionError)
	profile = frappe.get_doc("POS Profile", pos_profile)
	claim_tenders = [{"mode_of_payment": r["mode_of_payment"], "amount": r["amount"]} for r in exceeded]
	claim = {
		"action": "over_halo_tender",
		"invoice": doc.get("name") or "",
		"pos_profile": profile.name,
		"tenders": claim_tenders,
	}
	clean_reason = (reason or "").strip()
	if clean_reason:
		claim["reason"] = clean_reason
	approval = _require_till_privilege(
		profile,
		"over_halo_tender",
		doc.get("name") or "",
		claim,
		token=approval_token,
		amount=max(r["amount"] for r in exceeded),
		allow_cashier_default=False,
	)
	if not hasattr(doc, "flags") or doc.flags is None:
		doc.flags = frappe._dict()
	doc.flags.over_halo_approval = {
		"approver": approval.approver,
		"token_consumed": True,
		"tenders": {r["mode_of_payment"]: r["amount"] for r in exceeded},
	}
	return approval


