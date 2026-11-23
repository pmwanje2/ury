"""Till cash management: pickup, loan, paid-out and the closing Over/Short.

Oracle Simphony Cash Management moves cash in and out of a till during the
shift: a Pickup (safe drop) takes drawer cash to the safe, a Loan brings float
from the safe, and a Paid-Out pays an expense, or a tip payout, from the
drawer with a reason and, above the tender's HALO, a second person. At close
the counted-against-expected variance posts to a Cash Over/Short account.

Every movement is a native Journal Entry (``Cash Entry``) linked to its POS
Opening Entry, with exactly two lines: the till's cash drawer account (the
company account of a Cash-type till tender) and its counter account. Closing
adds each submitted movement's drawer line to that tender's expected cash.
The Over/Short entry is a native Journal Entry the closing posts on submit.

Accounts, the paid-out threshold and the approver role are POS Profile
settings: ``custom_cash_safe_account``, ``custom_paid_out_account``,
``custom_tips_payable_account``, ``custom_cash_over_short_account``,
``custom_paid_out_approval_threshold`` and
``custom_cash_movement_approver_role``.
"""

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import flt

# Whether each movement takes cash out of the drawer (True) or brings it in.
LEAVES_DRAWER = {"Pickup": True, "Loan": False, "Paid Out": True}


def validate(doc, method=None):
	"""Journal Entry validate: the shape of a till cash movement."""
	kind = doc.get("custom_cash_movement_type")
	if not doc.get("custom_pos_opening_entry"):
		if kind:
			frappe.throw(_("[URY-CASH-SHIFT] A till cash movement needs its POS shift."))
		return
	if kind not in LEAVES_DRAWER:
		frappe.throw(_("[URY-CASH-TYPE] Choose Pickup, Loan or Paid Out for a till cash movement."))
	profile = frappe.get_doc("POS Profile", _open_shift(doc).pos_profile)
	drawer, counter = _movement_lines(doc, profile)
	_check_direction(drawer, kind)
	_check_counter(profile, kind, counter)
	if kind == "Paid Out" and not (doc.get("custom_cash_movement_reason") or "").strip():
		frappe.throw(_("[URY-CASH-REASON] A paid-out needs a reason."))
	doc.voucher_type = "Cash Entry"


def before_submit(doc, method=None):
	"""Journal Entry before_submit: a paid-out above the till's threshold is
	submitted by a second person who holds the approver role."""
	if doc.get("custom_cash_movement_type") != "Paid Out" or not doc.get("custom_pos_opening_entry"):
		return
	profile = frappe.get_doc(
		"POS Profile", frappe.db.get_value("POS Opening Entry", doc.custom_pos_opening_entry, "pos_profile")
	)
	threshold = flt(profile.get("custom_paid_out_approval_threshold"))
	if flt(doc.total_debit) <= threshold:
		return
	role = profile.get("custom_cash_movement_approver_role")
	user = frappe.session.user
	if not role or user == doc.owner or role not in frappe.get_roles(user):
		frappe.throw(
			_(
				"[URY-CASH-SECOND-PERSON] A paid-out above {0} is submitted by a second person "
				"holding the {1} role, not by the person who recorded it."
			).format(threshold, role or _("Cash Movement Approver"))
		)
	doc.custom_cash_movement_approver = user


def before_cancel(doc, method=None):
	"""Journal Entry before_cancel: a movement counted by a closed shift stays."""
	if doc.get("custom_pos_opening_entry"):
		_open_shift(doc)


def till_cash_movements(opening_entry):
	"""Submitted movements of a POS shift: net debit per account (company currency)."""
	entry = frappe.qb.DocType("Journal Entry")
	line = frappe.qb.DocType("Journal Entry Account")
	rows = (
		frappe.qb.from_(line)
		.join(entry)
		.on(line.parent == entry.name)
		.select(line.account, Sum(line.debit - line.credit).as_("amount"))
		.where(
			(entry.custom_pos_opening_entry == opening_entry)
			& (entry.docstatus == 1)
			& (line.parenttype == "Journal Entry")
		)
		.groupby(line.account)
	).run(as_dict=True)
	return {row.account: flt(row.amount) for row in rows}


def validate_closing_cash(doc, method=None):
	"""POS Closing Entry before_submit: every movement of the shift is decided,
	and a cash difference has an Over/Short account."""
	pending = frappe.get_all(
		"Journal Entry",
		filters={"custom_pos_opening_entry": doc.pos_opening_entry, "docstatus": 0},
		pluck="name",
	)
	if pending:
		frappe.throw(
			_(
				"[URY-CLOSE-PENDING-MOVEMENT] Submit or delete the shift's pending cash movements "
				"before closing: {0}."
			).format(", ".join(pending))
		)
	if _cash_differences(doc):
		_over_short_account(doc)


def post_cash_over_short(doc, method=None):
	"""POS Closing Entry on_submit: post each cash tender's counted-against-expected
	difference to the till's Cash Over/Short account."""
	differences = _cash_differences(doc)
	if not differences:
		return
	account = _over_short_account(doc)
	cost_center = frappe.db.get_value("POS Profile", doc.pos_profile, "cost_center")
	for mode, difference in differences:
		drawer = _mode_account(mode, doc.company)
		short = difference < 0
		amount = abs(difference)
		entry = frappe.new_doc("Journal Entry")
		entry.voucher_type = "Cash Entry"
		entry.company = doc.company
		entry.posting_date = doc.posting_date
		entry.custom_pos_closing_entry = doc.name
		entry.user_remark = _("Till {0} over/short at closing {1}").format(mode, doc.name)
		entry.append(
			"accounts",
			{
				"account": account,
				"debit_in_account_currency": amount if short else 0,
				"credit_in_account_currency": 0 if short else amount,
				"cost_center": cost_center,
			},
		)
		entry.append(
			"accounts",
			{
				"account": drawer,
				"debit_in_account_currency": 0 if short else amount,
				"credit_in_account_currency": amount if short else 0,
				"cost_center": cost_center,
			},
		)
		# A system posting of the closing, like its consolidated invoice.
		entry.flags.ignore_permissions = True
		entry.insert()
		entry.submit()


def cancel_cash_over_short(doc, method=None):
	"""POS Closing Entry on_cancel: the closing's Over/Short journals go with it."""
	for name in frappe.get_all(
		"Journal Entry", filters={"custom_pos_closing_entry": doc.name, "docstatus": 1}, pluck="name"
	):
		entry = frappe.get_doc("Journal Entry", name)
		entry.flags.ignore_permissions = True
		entry.cancel()


def _open_shift(doc):
	opening = frappe.db.get_value(
		"POS Opening Entry",
		doc.custom_pos_opening_entry,
		["docstatus", "status", "company", "pos_profile"],
		as_dict=True,
	)
	if not opening or opening.docstatus != 1 or opening.status != "Open" or opening.company != doc.company:
		frappe.throw(_("[URY-CASH-SHIFT] Till cash moves only on an open POS shift of {0}.").format(doc.company))
	return opening


def _drawer_accounts(profile, company):
	accounts = set()
	for row in profile.get("payments") or []:
		if frappe.get_cached_value("Mode of Payment", row.mode_of_payment, "type") == "Cash":
			account = _mode_account(row.mode_of_payment, company, required=False)
			if account:
				accounts.add(account)
	return accounts


def _movement_lines(doc, profile):
	drawers = _drawer_accounts(profile, doc.company)
	lines = list(doc.get("accounts") or [])
	drawer_lines = [line for line in lines if line.account in drawers]
	if len(lines) != 2 or len(drawer_lines) != 1:
		frappe.throw(
			_(
				"[URY-CASH-DRAWER] A till cash movement has two lines: the till's cash drawer "
				"account ({0}) and its counter account."
			).format(", ".join(sorted(drawers)) or _("none configured"))
		)
	drawer = drawer_lines[0]
	counter = next(line for line in lines if line is not drawer)
	return drawer, counter


def _check_direction(drawer, kind):
	leaves = LEAVES_DRAWER[kind]
	amount, other = (drawer.credit, drawer.debit) if leaves else (drawer.debit, drawer.credit)
	if flt(amount) <= 0 or flt(other):
		frappe.throw(
			_("[URY-CASH-DIRECTION] A {0} {1} the till's cash drawer.").format(
				kind, _("credits") if leaves else _("debits")
			)
		)


def _check_counter(profile, kind, counter):
	if kind in ("Pickup", "Loan"):
		safe = profile.get("custom_cash_safe_account")
		if not safe or counter.account != safe:
			frappe.throw(
				_("[URY-CASH-COUNTER] A {0} moves cash between the drawer and the till's Safe Account ({1}).").format(
					kind, safe or _("not set")
				)
			)
		return
	tips = profile.get("custom_tips_payable_account")
	if counter.account != tips and frappe.db.get_value("Account", counter.account, "root_type") != "Expense":
		frappe.throw(
			_(
				"[URY-CASH-COUNTER] A paid-out goes to an expense account, or to the Tips Payable "
				"account for a tip payout."
			)
		)


def _cash_differences(doc):
	precision = frappe.get_precision("POS Closing Entry Detail", "difference")
	differences = []
	for row in doc.get("payment_reconciliation") or []:
		difference = flt(row.difference, precision)
		if difference and frappe.get_cached_value("Mode of Payment", row.mode_of_payment, "type") == "Cash":
			differences.append((row.mode_of_payment, difference))
	return differences


def _over_short_account(doc):
	account = frappe.db.get_value("POS Profile", doc.pos_profile, "custom_cash_over_short_account")
	ledger = (
		frappe.db.get_value("Account", account, ["company", "root_type", "is_group", "disabled"], as_dict=True)
		if account
		else None
	)
	if (
		not ledger
		or ledger.company != doc.company
		or ledger.root_type not in ("Expense", "Income")
		or ledger.is_group
		or ledger.disabled
	):
		frappe.throw(
			_(
				"[URY-CLOSE-OVER-SHORT-ACCOUNT] Set the POS Profile's Cash Over/Short Account to an "
				"enabled income or expense account of {0} before closing a till with a cash difference."
			).format(doc.company)
		)
	return account


def _mode_account(mode, company, required=True):
	account = frappe.db.get_value(
		"Mode of Payment Account", {"parent": mode, "company": company}, "default_account"
	)
	if not account and required:
		frappe.throw(
			_("[URY-CASH-DRAWER] Set {0}'s company account for {1}.").format(mode, company)
		)
	return account
