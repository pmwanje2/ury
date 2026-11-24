"""Till cash management: pickup, loan, paid-out, paid-in and the closing Over/Short.

Oracle Simphony Cash Management moves cash in and out of a till during the
shift: a Pickup (safe drop) takes drawer cash to the safe, a Loan brings float
from the safe, a Paid-Out pays an expense (or tip payout) from the drawer, and
a Paid-In brings miscellaneous cash into the drawer — each with a reason and,
above the till's limit, manager approval in place (PIN) or second-person Desk
submit. At close the counted-against-expected variance posts to a Cash
Over/Short account.

Every movement is a native Journal Entry (``Cash Entry``) linked to its POS
Opening Entry, with exactly two lines: the till's cash drawer account (the
company account of a Cash-type till tender) and its counter account. Closing
adds each submitted movement's drawer line to that tender's expected cash.
The Over/Short entry is a native Journal Entry the closing posts on submit.
"""

import frappe
from frappe import _
from frappe.utils import flt

from ury.brand import coded_message

# Whether each movement takes cash out of the drawer (True) or brings it in.
LEAVES_DRAWER = {"Pickup": True, "Loan": False, "Paid Out": True, "Paid In": False}


def validate(doc, method=None):
	"""Journal Entry validate: the shape of a till cash movement."""
	kind = doc.get("custom_cash_movement_type")
	if not doc.get("custom_pos_opening_entry"):
		if kind:
			frappe.throw(coded_message("CASH-SHIFT", _("A till cash movement needs its POS shift.")))
		return
	if kind not in LEAVES_DRAWER:
		frappe.throw(
			coded_message("CASH-TYPE", _("Choose Pickup, Loan, Paid Out or Paid In for a till cash movement."))
		)
	profile = frappe.get_doc("POS Profile", _open_shift(doc).pos_profile)
	drawer, counter = _movement_lines(doc, profile)
	_check_direction(drawer, kind)
	_check_counter(profile, kind, counter)
	if kind in ("Paid Out", "Paid In") and not (doc.get("custom_cash_movement_reason") or "").strip():
		frappe.throw(coded_message("CASH-REASON", _("A {0} needs a reason.").format(kind.lower())))
	if doc.docstatus == 0:
		doc.custom_cash_movement_approver = None
	doc.voucher_type = "Cash Entry"


def _movement_amount_in_profile_currency(doc, profile):
	"""Compare thresholds against the movement amount in the till's currency."""
	drawers = _drawer_accounts(profile, doc.company)
	for line in doc.get("accounts") or []:
		if line.get("account") in drawers:
			leaves = LEAVES_DRAWER.get(doc.get("custom_cash_movement_type"), True)
			acct_val = flt(line.get("credit_in_account_currency") if leaves else line.get("debit_in_account_currency"))
			if acct_val > 0:
				return acct_val
			base_val = flt(line.get("credit") if leaves else line.get("debit"))
			if base_val > 0:
				return base_val
	return flt(doc.get("total_debit"))


def before_submit(doc, method=None):
	"""Journal Entry before_submit: a paid-out or paid-in above the till's threshold is
	approved by a second authorized person — either in place via Manager PIN
	(server-only ``doc.flags.cash_movement_approval``) or via second-person Desk submit."""
	if not doc.get("custom_pos_opening_entry"):
		return
	kind = doc.get("custom_cash_movement_type")
	if kind not in ("Paid Out", "Paid In"):
		doc.custom_cash_movement_approver = None
		return
	profile = frappe.get_doc(
		"POS Profile", frappe.db.get_value("POS Opening Entry", doc.custom_pos_opening_entry, "pos_profile")
	)
	threshold_field = (
		"custom_paid_out_approval_threshold" if kind == "Paid Out" else "custom_paid_in_approval_threshold"
	)
	threshold = flt(profile.get(threshold_field))
	movement_amount = _movement_amount_in_profile_currency(doc, profile)
	if movement_amount <= threshold:
		doc.custom_cash_movement_approver = None
		return

	# In-place Manager PIN approval: only set server-side after override.consume()
	pin_approval = getattr(getattr(doc, "flags", None), "cash_movement_approval", None)
	if isinstance(pin_approval, dict) and pin_approval.get("token_consumed") and pin_approval.get("approver"):
		approver = str(pin_approval["approver"]).strip()
		if approver.casefold() in {(doc.get("owner") or "").casefold(), (frappe.session.user or "").casefold()}:
			frappe.throw(
				coded_message("AUTH-SELF", _("You cannot approve your own cash movement. Ask another manager.")),
				frappe.PermissionError,
			)
		doc.custom_cash_movement_approver = approver
		return

	role = profile.get("custom_cash_movement_approver_role")
	user = frappe.session.user
	if not role or user == doc.owner or role not in frappe.get_roles(user):
		frappe.throw(
			coded_message(
				"CASH-SECOND-PERSON",
				_(
					"A {0} above {1} must be approved by a second authorized person, "
					"not by the person who recorded it."
				).format(kind.lower(), threshold),
			)
		)
	doc.custom_cash_movement_approver = user


def before_cancel(doc, method=None):
	"""Journal Entry before_cancel: a movement counted by a closed shift stays."""
	if doc.get("custom_pos_opening_entry"):
		_open_shift(doc)


def till_cash_movements(opening_entry, currency=None):
	"""Submitted movements of a POS shift: net debit per account (account currency when it
	matches ``currency``, else company currency)."""
	from frappe.query_builder.functions import Sum
	entry = frappe.qb.DocType("Journal Entry")
	line = frappe.qb.DocType("Journal Entry Account")
	rows = (
		frappe.qb.from_(line)
		.join(entry)
		.on(line.parent == entry.name)
		.select(
			line.account,
			line.account_currency,
			Sum(line.debit - line.credit).as_("base_amount"),
			Sum(line.debit_in_account_currency - line.credit_in_account_currency).as_("acct_amount"),
		)
		.where(
			(entry.custom_pos_opening_entry == opening_entry)
			& (entry.docstatus == 1)
			& (line.parenttype == "Journal Entry")
		)
		.groupby(line.account, line.account_currency)
	).run(as_dict=True)
	totals = {}
	for row in rows:
		use_acct = bool(currency and row.get("account_currency") and row.get("account_currency") == currency)
		amount = flt(row.acct_amount if use_acct else row.base_amount)
		totals[row.account] = flt(totals.get(row.account, 0) + amount)
	return totals


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
			coded_message(
				"CLOSE-PENDING-MOVEMENT",
				_(
					"Submit or delete the shift's pending cash movements "
					"before closing: {0}."
				).format(", ".join(pending)),
			)
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
	company_currency = frappe.db.get_value("Company", doc.company, "default_currency")
	over_short_currency = frappe.db.get_value("Account", account, "account_currency") or company_currency
	for mode, difference in differences:
		drawer = _mode_account(mode, doc.company)
		drawer_currency = frappe.db.get_value("Account", drawer, "account_currency") or company_currency
		short = difference < 0
		amount = abs(difference)
		is_multi_currency = bool(
			company_currency
			and (drawer_currency != company_currency or over_short_currency != company_currency)
		)
		exchange_rate = 1.0
		if is_multi_currency and drawer_currency != company_currency:
			from erpnext.setup.utils import get_exchange_rate

			exchange_rate = flt(get_exchange_rate(drawer_currency, company_currency, doc.posting_date)) or 1.0
		base_amount = flt(amount * exchange_rate)
		entry = frappe.new_doc("Journal Entry")
		entry.voucher_type = "Cash Entry"
		entry.company = doc.company
		entry.posting_date = doc.posting_date
		if is_multi_currency:
			entry.multi_currency = 1
		entry.custom_pos_closing_entry = doc.name
		reason = (doc.get("custom_over_short_reason") or "").strip()
		entry.user_remark = (
			_("Till {0} over/short at closing {1}: {2}").format(mode, doc.name, reason)
			if reason
			else _("Till {0} over/short at closing {1}").format(mode, doc.name)
		)
		over_short_amt = base_amount if over_short_currency == company_currency else amount
		over_short_rate = 1.0 if over_short_currency == company_currency else exchange_rate
		entry.append(
			"accounts",
			{
				"account": account,
				"account_currency": over_short_currency,
				"exchange_rate": over_short_rate,
				"debit_in_account_currency": over_short_amt if short else 0,
				"credit_in_account_currency": 0 if short else over_short_amt,
				"cost_center": cost_center,
			},
		)
		entry.append(
			"accounts",
			{
				"account": drawer,
				"account_currency": drawer_currency,
				"exchange_rate": exchange_rate if drawer_currency != company_currency else 1.0,
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
		frappe.throw(
			coded_message("CASH-SHIFT", _("Till cash moves only on an open POS shift of {0}.").format(doc.company))
		)
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
			coded_message(
				"CASH-DRAWER",
				_(
					"A till cash movement has two lines: the till's cash drawer "
					"account ({0}) and its counter account."
				).format(", ".join(sorted(drawers)) or _("none configured")),
			)
		)
	drawer = drawer_lines[0]
	counter = next(line for line in lines if line is not drawer)
	return drawer, counter


def _check_direction(drawer, kind):
	leaves = LEAVES_DRAWER[kind]
	amount, other = (drawer.credit, drawer.debit) if leaves else (drawer.debit, drawer.credit)
	if flt(amount) <= 0 or flt(other):
		frappe.throw(
			coded_message(
				"CASH-DIRECTION",
				_("A {0} {1} the till's cash drawer.").format(kind, _("credits") if leaves else _("debits")),
			)
		)


def _check_counter(profile, kind, counter):
	if kind in ("Pickup", "Loan"):
		safe = profile.get("custom_cash_safe_account")
		if not safe or counter.account != safe:
			frappe.throw(
				coded_message(
					"CASH-COUNTER",
					_("A {0} moves cash between the drawer and the till's Safe Account ({1}).").format(
						kind, safe or _("not set")
					),
				)
			)
		return
	if kind == "Paid In":
		paid_in = profile.get("custom_paid_in_account")
		if paid_in:
			if counter.account != paid_in:
				frappe.throw(
					coded_message(
						"CASH-COUNTER",
						_("A paid-in uses the till's Paid In Account ({0}).").format(paid_in),
					)
				)
			return
		root_type = frappe.db.get_value("Account", counter.account, "root_type")
		if root_type not in ("Income", "Liability"):
			frappe.throw(
				coded_message(
					"CASH-COUNTER",
					_("Set the POS Profile's Paid In Account to an income or liability account before recording a paid-in."),
				)
			)
		return
	tips = profile.get("custom_tips_payable_account")
	if counter.account != tips and frappe.db.get_value("Account", counter.account, "root_type") != "Expense":
		frappe.throw(
			coded_message(
				"CASH-COUNTER",
				_(
					"A paid-out goes to an expense account, or to the Tips Payable "
					"account for a tip payout."
				),
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
			coded_message(
				"CLOSE-OVER-SHORT-ACCOUNT",
				_(
					"Set the POS Profile's Cash Over/Short Account to an "
					"enabled income or expense account of {0} before closing a till with a cash difference."
				).format(doc.company),
			)
		)
	return account


def _mode_account(mode, company, required=True):
	account = frappe.db.get_value(
		"Mode of Payment Account", {"parent": mode, "company": company}, "default_account"
	)
	if not account and required:
		frappe.throw(
			coded_message("CASH-DRAWER", _("Set {0}'s company account for {1}.").format(mode, company))
		)
	return account
