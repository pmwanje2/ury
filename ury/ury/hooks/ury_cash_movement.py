"""Till cash management: pickup, loan, paid-out and the closing Over/Short.

Oracle Simphony Cash Management moves cash in and out of a till during the
shift: a Pickup (safe drop) takes drawer cash to the safe, a Loan brings float
from the safe, and a Paid-Out pays an expense, or a tip payout (optionally
attributed to a server via ``custom_tip_server``), from the drawer with a
reason and, above the tender's HALO, a second person. At close the
counted-against-expected variance posts to a Cash Over/Short account, with an
optional configurable reason above tolerance.

Every movement is a native Journal Entry (``Cash Entry``) linked to its POS
Opening Entry, with exactly two lines: the till's cash drawer account (the
company account of a Cash-type till tender) and its counter account. For
foreign-currency tills (POS Profile currency != Company currency), movements
and closing Over/Short entries enable ``multi_currency`` with the posting
date's exchange rate so drawer movements and closing expected/counted/variance
stay in POS Profile currency while GL postings balance in Company currency.

Accounts, the paid-out threshold, the approver role and the Over/Short reason
rule are POS Profile settings: ``custom_cash_safe_account``,
``custom_paid_out_account``, ``custom_tips_payable_account``,
``custom_cash_over_short_account``, ``custom_paid_out_approval_threshold``
(``0`` = every paid-out needs a second person; blank = no limit),
``custom_cash_movement_approver_role``, ``custom_require_over_short_reason``
and ``custom_over_short_reason_tolerance``.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from ury.brand import coded_message

# Whether each movement takes cash out of the drawer (True) or brings it in.
LEAVES_DRAWER = {"Pickup": True, "Loan": False, "Paid Out": True}


def paid_out_threshold(profile):
	"""Return the paid-out second-person threshold in POS Profile currency, or None for no limit.

	``0`` (the default) means every paid-out needs a second person; blank (``None``
	or whitespace-only string) means no limit.
	"""
	raw = profile.get("custom_paid_out_approval_threshold")
	if raw is None or (isinstance(raw, str) and not raw.strip()):
		return None
	return flt(raw)


def account_currency(account, company_currency):
	return frappe.db.get_value("Account", account, "account_currency") or company_currency


def resolve_exchange_rate(from_currency, to_currency, posting_date=None):
	if not from_currency or not to_currency or from_currency == to_currency:
		return 1.0
	from erpnext.setup.utils import get_exchange_rate

	rate = flt(get_exchange_rate(from_currency, to_currency, posting_date or frappe.utils.nowdate()))
	if rate <= 0:
		frappe.throw(
			coded_message(
				"CASH-EXCHANGE-RATE",
				_("Set a Currency Exchange rate from {0} to {1} before posting a foreign-currency till entry.").format(
					from_currency, to_currency
				),
			)
		)
	return rate


def build_movement_je_accounts(
	drawer,
	counter,
	drawer_leaves,
	profile_amount,
	profile_currency,
	company_currency,
	posting_date,
	cost_center,
):
	"""Build balanced 2-line Journal Entry accounts for a single- or multi-currency till."""
	company_precision = frappe.get_precision("Journal Entry Account", "debit") or 2
	acct_precision = frappe.get_precision("Journal Entry Account", "debit_in_account_currency") or 2
	profile_to_company = resolve_exchange_rate(profile_currency, company_currency, posting_date)
	company_amount = flt(flt(profile_amount) * profile_to_company, company_precision)

	rows = []
	for acct, is_debit in ((drawer, not drawer_leaves), (counter, drawer_leaves)):
		curr = account_currency(acct, company_currency)
		if curr == profile_currency:
			acct_amount = flt(profile_amount, acct_precision)
			rate = (
				1.0
				if curr == company_currency
				else (flt(company_amount / acct_amount, 9) if acct_amount else profile_to_company)
			)
		elif curr == company_currency:
			acct_amount = company_amount
			rate = 1.0
		else:
			fx = resolve_exchange_rate(curr, company_currency, posting_date)
			acct_amount = flt(company_amount / fx, acct_precision)
			rate = flt(company_amount / acct_amount, 9) if acct_amount else fx

		rows.append(
			{
				"account": acct,
				"account_currency": curr,
				"exchange_rate": rate,
				"debit_in_account_currency": acct_amount if is_debit else 0,
				"credit_in_account_currency": 0 if is_debit else acct_amount,
				"debit": company_amount if is_debit else 0,
				"credit": 0 if is_debit else company_amount,
				"cost_center": cost_center,
			}
		)

	multi_currency = 1 if any(row["account_currency"] != company_currency for row in rows) else 0
	return multi_currency, rows


def line_amount_in_profile_currency(line, leaves, profile_currency, company_currency, posting_date=None):
	"""Read the positive movement amount from a drawer line in POS Profile currency."""
	acct_curr = line.get("account_currency") if isinstance(line, dict) else getattr(line, "account_currency", None)
	if not profile_currency or not acct_curr or acct_curr == profile_currency:
		raw_acct = (
			(line.get("credit_in_account_currency") if isinstance(line, dict) else getattr(line, "credit_in_account_currency", 0))
			if leaves
			else (line.get("debit_in_account_currency") if isinstance(line, dict) else getattr(line, "debit_in_account_currency", 0))
		)
		if flt(raw_acct):
			return flt(raw_acct)
	raw_base = (
		(line.get("credit") if isinstance(line, dict) else getattr(line, "credit", 0))
		if leaves
		else (line.get("debit") if isinstance(line, dict) else getattr(line, "debit", 0))
	)
	if profile_currency and company_currency and profile_currency != company_currency:
		rate = resolve_exchange_rate(profile_currency, company_currency, posting_date)
		return flt(flt(raw_base) / rate)
	return flt(raw_base)


def validate(doc, method=None):
	"""Journal Entry validate: the shape of a till cash movement."""
	kind = doc.get("custom_cash_movement_type")
	if not doc.get("custom_pos_opening_entry"):
		if kind:
			frappe.throw(coded_message("CASH-SHIFT", _("A till cash movement needs its POS shift.")))
		return
	if kind not in LEAVES_DRAWER:
		frappe.throw(coded_message("CASH-TYPE", _("Choose Pickup, Loan or Paid Out for a till cash movement.")))
	profile = frappe.get_doc("POS Profile", _open_shift(doc).pos_profile)
	drawer, counter = _movement_lines(doc, profile)
	_check_direction(drawer, kind)
	_check_counter(profile, kind, counter)
	if kind == "Paid Out" and not (doc.get("custom_cash_movement_reason") or "").strip():
		frappe.throw(coded_message("CASH-REASON", _("A paid-out needs a reason.")))
	tips = profile.get("custom_tips_payable_account")
	if not (kind == "Paid Out" and tips and counter.account == tips):
		doc.custom_tip_server = None
	if doc.docstatus == 0:
		doc.custom_cash_movement_approver = None
	doc.voucher_type = "Cash Entry"


def before_submit(doc, method=None):
	"""Journal Entry before_submit: a paid-out above the till's threshold is
	submitted by a second person who holds the approver role."""
	if not doc.get("custom_pos_opening_entry"):
		return
	if doc.get("custom_cash_movement_type") != "Paid Out":
		doc.custom_cash_movement_approver = None
		return
	profile = frappe.get_doc(
		"POS Profile", frappe.db.get_value("POS Opening Entry", doc.custom_pos_opening_entry, "pos_profile")
	)
	threshold = paid_out_threshold(profile)
	if threshold is None:
		doc.custom_cash_movement_approver = None
		return
	company_currency = frappe.get_cached_value("Company", doc.company, "default_currency") or profile.currency
	drawer, _counter = _movement_lines(doc, profile)
	movement_amount = line_amount_in_profile_currency(
		drawer, True, profile.currency, company_currency, doc.posting_date
	)
	if flt(movement_amount) <= threshold:
		doc.custom_cash_movement_approver = None
		return
	role = profile.get("custom_cash_movement_approver_role")
	user = frappe.session.user
	if not role or user == doc.owner or role not in frappe.get_roles(user):
		frappe.throw(
			coded_message(
				"CASH-SECOND-PERSON",
				_(
					"A paid-out above {0} must be approved and submitted by a second authorized person, "
					"not by the person who recorded it."
				).format(threshold),
			)
		)
	doc.custom_cash_movement_approver = user


def before_cancel(doc, method=None):
	"""Journal Entry before_cancel: a movement counted by a closed shift stays."""
	if doc.get("custom_pos_opening_entry"):
		_open_shift(doc)


def till_cash_movements(opening_entry):
	"""Submitted movements of a POS shift: net debit per cash drawer account in POS Profile currency."""
	opening = frappe.db.get_value(
		"POS Opening Entry", opening_entry, ["pos_profile", "company"], as_dict=True
	)
	profile = frappe.get_doc("POS Profile", opening.pos_profile) if opening and opening.pos_profile else None
	drawers = _drawer_accounts(profile, opening.company) if profile else set()
	profile_currency = profile.currency if profile else None
	company_currency = (
		frappe.get_cached_value("Company", opening.company, "default_currency")
		if opening and opening.company
		else None
	)
	precision = frappe.get_precision("POS Closing Entry Detail", "expected_amount") or 2

	entry = frappe.qb.DocType("Journal Entry")
	line = frappe.qb.DocType("Journal Entry Account")
	rows = (
		frappe.qb.from_(line)
		.join(entry)
		.on(line.parent == entry.name)
		.select(
			line.account,
			line.account_currency,
			line.debit_in_account_currency,
			line.credit_in_account_currency,
			line.debit,
			line.credit,
			entry.posting_date,
		)
		.where(
			(entry.custom_pos_opening_entry == opening_entry)
			& (entry.docstatus == 1)
			& (line.parenttype == "Journal Entry")
		)
	).run(as_dict=True)

	totals = {}
	for row in rows:
		if drawers and row.account not in drawers:
			continue
		if not profile_currency or not row.account_currency or row.account_currency == profile_currency:
			if row.account_currency:
				delta = flt(row.debit_in_account_currency) - flt(row.credit_in_account_currency)
			else:
				delta = flt(row.debit) - flt(row.credit)
		else:
			rate = resolve_exchange_rate(profile_currency, company_currency, row.posting_date)
			delta = (flt(row.debit) - flt(row.credit)) / rate
		totals[row.account] = flt(totals.get(row.account, 0) + delta, precision)
	return totals


def validate_closing_cash(doc, method=None):
	"""POS Closing Entry before_submit: every movement of the shift is decided,
	a cash difference has an Over/Short account, and a variance above tolerance
	has a reason when required by the POS Profile."""
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
	differences = _cash_differences(doc)
	fx_variances = _fx_cash_variances(doc)
	if differences:
		_over_short_account(doc)
	if differences or fx_variances:
		require_reason, tolerance = frappe.db.get_value(
			"POS Profile",
			doc.pos_profile,
			["custom_require_over_short_reason", "custom_over_short_reason_tolerance"],
		) or (0, 0)
		if cint(require_reason):
			precision = frappe.get_precision("POS Closing Entry Detail", "difference") or 2
			tol = flt(tolerance, precision)
			exceeds = any(flt(abs(diff), precision) > tol for _mode, diff in differences) or any(
				flt(abs(diff), precision) > tol or (tol <= 0 and abs(fx_diff) > 0)
				for _mode, diff, fx_diff in fx_variances
			)
			if exceeds and not (doc.get("custom_over_short_reason") or "").strip():
				frappe.throw(
					coded_message(
						"CLOSE-OVER-SHORT-REASON",
						_("Enter a reason for the cash over/short variance above {0} before closing.").format(tol),
					)
				)


def post_cash_over_short(doc, method=None):
	"""POS Closing Entry on_submit: post each cash tender's counted-against-expected
	difference to the till's Cash Over/Short account (multi-currency when needed)."""
	differences = _cash_differences(doc)
	if not differences:
		return
	account = _over_short_account(doc)
	cost_center, profile_currency = frappe.db.get_value(
		"POS Profile", doc.pos_profile, ["cost_center", "currency"]
	) or (None, None)
	company_currency = frappe.get_cached_value("Company", doc.company, "default_currency") or profile_currency
	profile_currency = profile_currency or company_currency
	reason = (doc.get("custom_over_short_reason") or "").strip()

	for mode, difference in differences:
		drawer = _mode_account(mode, doc.company)
		short = difference < 0
		multi_currency, accounts = build_movement_je_accounts(
			drawer=drawer,
			counter=account,
			drawer_leaves=short,
			profile_amount=abs(difference),
			profile_currency=profile_currency,
			company_currency=company_currency,
			posting_date=doc.posting_date,
			cost_center=cost_center,
		)
		remark = _("Till {0} over/short at closing {1}").format(mode, doc.name)
		if reason:
			remark = f"{remark}: {reason}"
		entry = frappe.new_doc("Journal Entry")
		entry.voucher_type = "Cash Entry"
		entry.company = doc.company
		entry.posting_date = doc.posting_date
		entry.multi_currency = multi_currency
		entry.custom_pos_closing_entry = doc.name
		entry.user_remark = remark
		for row in accounts:
			entry.append("accounts", row)
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


def _till_currency(profile, company):
	profile_curr = profile.get("currency") if profile else None
	if isinstance(profile_curr, str) and profile_curr.strip():
		return profile_curr.strip().upper()
	comp_curr = frappe.get_cached_value("Company", company, "default_currency") if company else None
	return comp_curr.strip().upper() if isinstance(comp_curr, str) and comp_curr.strip() else ""


def _is_fx_mode(mode, till_currency):
	"""Return True when Mode of Payment has a foreign custom_tender_currency != till_currency."""
	if not mode or not till_currency:
		return False
	try:
		curr = frappe.db.get_value("Mode of Payment", mode, "custom_tender_currency")
	except Exception:
		curr = None
	if not isinstance(curr, str) or not curr.strip():
		return False
	return curr.strip().upper() != till_currency


def _drawer_accounts(profile, company):
	accounts = set()
	till_curr = _till_currency(profile, company)
	for row in profile.get("payments") or []:
		if frappe.get_cached_value("Mode of Payment", row.mode_of_payment, "type") == "Cash":
			if _is_fx_mode(row.mode_of_payment, till_curr):
				continue
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
	credit_val = flt(drawer.credit) or flt(drawer.credit_in_account_currency)
	debit_val = flt(drawer.debit) or flt(drawer.debit_in_account_currency)
	amount, other = (credit_val, debit_val) if leaves else (debit_val, credit_val)
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


def _is_fx_reconciliation_row(doc, row):
	curr = (row.get("custom_tender_currency") or "").strip().upper()
	if not curr:
		return False
	home = (
		(frappe.db.get_value("POS Profile", doc.pos_profile, "currency") if doc.get("pos_profile") else None)
		or (frappe.get_cached_value("Company", doc.company, "default_currency") if doc.get("company") else None)
		or ""
	).strip().upper()
	return bool(home and curr != home)


def _cash_differences(doc):
	precision = frappe.get_precision("POS Closing Entry Detail", "difference")
	differences = []
	for row in doc.get("payment_reconciliation") or []:
		if _is_fx_reconciliation_row(doc, row):
			# FX cash variance is posted once inside post_fx_vault_sweep_journal_entry
			# so FX Till Clearing nets to 0 without double-posting Over/Short.
			continue
		difference = flt(row.difference, precision)
		if difference and frappe.get_cached_value("Mode of Payment", row.mode_of_payment, "type") == "Cash":
			differences.append((row.mode_of_payment, difference))
	return differences


def _fx_cash_variances(doc):
	"""Return (mode, base_difference, foreign_difference) for FX rows with non-zero variance.

	Used by validate_closing_cash() so custom_require_over_short_reason is enforced
	even when the shift's only drawer variance is on a foreign-currency tender row.
	"""
	precision = frappe.get_precision("POS Closing Entry Detail", "difference") or 2
	variances = []
	for row in doc.get("payment_reconciliation") or []:
		if not _is_fx_reconciliation_row(doc, row):
			continue
		base_diff = flt(row.get("difference"), precision)
		fx_diff = flt(row.get("custom_foreign_difference"), 4)
		if base_diff or fx_diff:
			variances.append((row.mode_of_payment, base_diff, fx_diff))
	return variances


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
