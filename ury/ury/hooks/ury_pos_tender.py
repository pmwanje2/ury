"""Tender rules on the POS Invoice: non-cash over-tender and charged tips.

Oracle Simphony gives change only from a cash tender. A non-cash tender
(card, mobile money, room charge) that is over-tendered is refused, or, when
its Tender/Media record posts the excess as a charged tip, the excess becomes
a charged tip. Charged and cash tips are owed to the server: they are a Tips
Payable liability, never sales revenue, never in the VAT base and never
drawer change.

Configuration is native-first:

- ``Mode of Payment.custom_overtender_rule``: ``Refuse`` (default) or
  ``Post to Charged Tip``. Cash-type tenders always give change.
- ``POS Profile.custom_tips_payable_account``: the Tips Payable liability,
  next to the native ``account_for_change_amount``.

A tip is one native Sales Taxes and Charges row (``Actual`` on the Tips
Payable account, after every tax row), so it stays out of the net total that
VAT is computed on, out of the discount base, and consolidates and posts to
the GL like any other charge. ``custom_charged_tip`` holds the bill's whole tip
and ``custom_tip_server`` the server it is owed to.
"""

import frappe
from frappe import _
from frappe.utils import flt

from ury.brand import coded_message

OVERTENDER_TO_TIP = "Post to Charged Tip"


def _grand_total_precision(doc):
	precision_fn = getattr(doc, "precision", None)
	return precision_fn("grand_total") if callable(precision_fn) else None


def validate_mode_of_payment_halo(doc, method=None):
	"""Mode of Payment validate: High Amount Lockout (HALO) ceiling cannot be negative."""
	if flt(doc.get("custom_halo_max_amount")) < 0:
		frappe.throw(
			coded_message(
				"TENDER-HALO-INVALID",
				_("High Amount Lockout (HALO) on {0} cannot be negative.").format(
					doc.get("name") or _("Mode of Payment")
				),
			)
		)


def _enforce_tender_sanity_and_halo(doc):
	"""Enforce non-negative tenders, currency decimal precision, and per-tender HALO ceiling
	at the mutation itself (so POS UI, Desk, REST, and offline replay meet the same rule)."""
	from ury.ury_pos.till_controls import (
		_may_perform_till_privilege,
		currency_precision,
		over_halo_tenders_for_invoice,
	)

	currency = doc.get("currency")
	if not currency and doc.get("pos_profile"):
		currency = frappe.db.get_value("POS Profile", doc.pos_profile, "currency")
	precision = currency_precision(currency)

	for row in doc.get("payments") or []:
		raw_amt = row.get("amount")
		if raw_amt is None or raw_amt == "":
			continue
		amt = flt(raw_amt)
		if amt < 0:
			frappe.throw(
				coded_message(
					"TENDER-NEGATIVE",
					_("Negative payment amount for {0} is not allowed.").format(
						row.get("mode_of_payment") or _("tender")
					),
				)
			)
		if amt > 0 and abs(amt - flt(amt, precision)) > 1e-6:
			if precision == 0:
				msg = _("{0} tender amounts must be whole numbers without decimals.").format(
					currency or _("Currency")
				)
			else:
				msg = _("{0} tender amounts allow at most {1} decimal places.").format(
					currency or _("Currency"), precision
				)
			frappe.throw(coded_message("CASH-PRECISION", msg))

	exceeded = over_halo_tenders_for_invoice(doc)
	if not exceeded:
		return

	flags = getattr(doc, "flags", None)
	halo_approval = getattr(flags, "over_halo_approval", None)
	if isinstance(halo_approval, dict) and halo_approval.get("token_consumed") and halo_approval.get("approver"):
		approver = str(halo_approval["approver"]).strip()
		approved_tenders = halo_approval.get("tenders") or {}
		if approver.casefold() == (frappe.session.user or "").casefold():
			frappe.throw(
				coded_message("AUTH-SELF", _("You cannot approve your own over-limit tender. Ask another manager.")),
				frappe.PermissionError,
			)
		all_covered = all(
			r["mode_of_payment"] in approved_tenders
			and abs(flt(approved_tenders[r["mode_of_payment"]], precision) - flt(r["amount"], precision)) <= 1e-6
			for r in exceeded
		)
		if all_covered:
			doc.custom_halo_approver = approver
			return

	pos_profile = doc.get("pos_profile")
	if pos_profile and frappe.db.exists("POS Profile", pos_profile):
		profile = frappe.get_doc("POS Profile", pos_profile)
		if _may_perform_till_privilege(
			frappe.session.user,
			profile,
			"over_halo_tender",
			amount=max(r["approval_amount"] for r in exceeded),
			allow_cashier_default=False,
		):
			return

	first = exceeded[0]
	frappe.throw(
		coded_message(
			"TENDER-HALO",
			_(
				"{0} tender of {1} exceeds the High Amount Lockout limit ({2}). "
				"Check for a mistyped amount or ask a manager to approve with their PIN."
			).format(first["mode_of_payment"], first["amount"], first["halo"]),
		),
		frappe.PermissionError,
	)


def before_validate(doc, method=None):
	"""Runs before the native POS Invoice validate computes paid and change
	amounts, so the native change can only ever come from cash received."""
	if doc.get("is_return"):
		_sync_return_tip(doc)
		doc.calculate_taxes_and_totals()
		return
	_enforce_tender_sanity_and_halo(doc)
	sync_charged_tip(doc)
	doc.calculate_taxes_and_totals()
	over, non_cash_modes = _non_cash_overtender(doc)
	if over <= 0:
		return
	refusing = [mode for mode in non_cash_modes if _overtender_rule(mode) != OVERTENDER_TO_TIP]
	if refusing:
		frappe.throw(
			coded_message(
				"TENDER-OVERTENDER",
				_(
					"{0} cannot be over-tendered: only cash gives change. "
					"Reduce it to the balance due."
				).format(", ".join(refusing)),
			)
		)
	doc.custom_charged_tip = flt(doc.get("custom_charged_tip")) + over
	sync_charged_tip(doc)
	doc.calculate_taxes_and_totals()


def calculate_taxes_and_totals_excluding_tip(doc, calculate_fn):
	"""Compute bill-level discounts on the pre-tip bill, never on Tips Payable.

	ERPNext's ``calculate_discounted_totals()`` derives ``discount_amount`` from
	``grand_total`` (which already includes ``Actual`` charges like Tips Payable)
	and distributes that inflated discount across item net amounts. Detaching
	the Tips Payable row during pass 1 yields the exact pre-tip discount on both
	``Grand Total`` and ``Net Total`` bases (exclusive or inclusive VAT, rounded
	or unrounded, returns, and unsaved previews) and prevents a flat discount
	from exceeding the pre-tip bill and eating into the tip.
	"""
	if getattr(doc, "_ury_tip_discount_guard", False):
		return calculate_fn()

	if doc.get("is_return") and doc.is_new() and doc.get("custom_charged_tip") is None:
		_adopt_return_tip_if_unset(doc)

	account = _tips_account(doc)
	has_discount = bool(flt(doc.get("additional_discount_percentage")) or flt(doc.get("discount_amount")))
	if not account or not has_discount:
		return calculate_fn()

	taxes = list(doc.get("taxes") or [])
	tip_rows = [row for row in taxes if row.account_head == account]
	if not tip_rows:
		return calculate_fn()

	kept = [row for row in taxes if row.account_head != account]
	saved_pct = doc.get("additional_discount_percentage")
	precision = _grand_total_precision(doc)
	reattached = False
	doc._ury_tip_discount_guard = True
	try:
		doc.set("taxes", kept)
		for position, row in enumerate(kept, start=1):
			row.idx = position
		calculate_fn()

		if not doc.get("is_return") and flt(doc.get("grand_total"), precision) < 0:
			frappe.throw(
				coded_message(
					"TIP-DISCOUNT-EXCEEDS-BILL",
					_("Bill discount cannot exceed the pre-tip bill total."),
				)
			)

		computed_discount = doc.get("discount_amount")
		combined = list(doc.get("taxes") or []) + tip_rows
		doc.set("taxes", combined)
		for position, row in enumerate(combined, start=1):
			row.idx = position
		reattached = True

		doc.additional_discount_percentage = 0
		doc.discount_amount = computed_discount
		return calculate_fn()
	finally:
		if not reattached:
			combined = list(doc.get("taxes") or []) + tip_rows
			doc.set("taxes", combined)
			for position, row in enumerate(combined, start=1):
				row.idx = position
		doc.additional_discount_percentage = saved_pct
		doc._ury_tip_discount_guard = False


def sync_charged_tip(doc):
	"""Make the bill's Tips Payable row match ``custom_charged_tip``."""
	tip = flt(doc.get("custom_charged_tip"), _grand_total_precision(doc))
	if tip < 0:
		frappe.throw(coded_message("TIP-NEGATIVE", _("A tip cannot be negative.")))
	account = _tips_account(doc)
	if account:
		kept = [row for row in doc.get("taxes") or [] if row.account_head != account]
		doc.set("taxes", kept)
		for position, row in enumerate(kept, start=1):
			row.idx = position
	if not tip:
		return
	_check_tips_account(doc, account)
	if doc.get("custom_ordering_session"):
		from ury.ury.api.guest_tip import self_order_tip_server

		doc.custom_tip_server = self_order_tip_server(doc.get("custom_tip_server"), doc.get("waiter"))
	else:
		doc.custom_tip_server = doc.get("custom_tip_server") or doc.get("waiter") or doc.get("owner") or frappe.session.user
	_append_tip_row(doc, account, tip)


def _adopt_return_tip_if_unset(doc):
	"""Populate custom_charged_tip from a newly mapped sales return's copied Tips Payable row.

	Only runs while ``custom_charged_tip`` is still ``None`` (unset by ``no_copy=1`` during
	``make_sales_return``). Once set — including explicitly zeroed to ``0`` for a partial return —
	the caller's value is authoritative and never overwritten.
	"""
	account = _tips_account(doc)
	if not account:
		return
	existing = [row for row in doc.get("taxes") or [] if row.account_head == account]
	if not existing:
		return
	precision = _grand_total_precision(doc)
	existing_total = flt(sum(flt(row.tax_amount) for row in existing), precision)
	if existing_total < 0:
		doc.custom_charged_tip = existing_total
		doc.custom_tip_server = _resolve_return_tip_server(doc)


def _sync_return_tip(doc):
	"""Keep the tip reversal visible on sales returns, and honour explicit zeroing on partial returns."""
	precision = _grand_total_precision(doc)
	if doc.is_new() and doc.get("custom_charged_tip") is None:
		_adopt_return_tip_if_unset(doc)
	tip = flt(doc.get("custom_charged_tip"), precision)
	if tip > 0:
		frappe.throw(coded_message("TIP-NEGATIVE", _("A sales return tip cannot be positive.")))
	account = _tips_account(doc)
	if not account:
		return
	kept = [row for row in doc.get("taxes") or [] if row.account_head != account]
	if tip == 0:
		doc.set("taxes", kept)
		for position, row in enumerate(kept, start=1):
			row.idx = position
		doc.custom_charged_tip = 0
		doc.custom_tip_server = None
		return
	_check_tips_account(doc, account)
	doc.set("taxes", kept)
	for position, row in enumerate(kept, start=1):
		row.idx = position
	_append_tip_row(doc, account, tip)
	doc.custom_tip_server = _resolve_return_tip_server(doc)


def _resolve_return_tip_server(doc):
	if doc.get("custom_tip_server"):
		return doc.custom_tip_server
	if doc.get("return_against"):
		original_server = frappe.db.get_value("POS Invoice", doc.return_against, "custom_tip_server")
		if original_server:
			return original_server
	return doc.get("waiter") or doc.get("owner") or frappe.session.user


def _append_tip_row(doc, account, tip):
	doc.append(
		"taxes",
		{
			"charge_type": "Actual",
			"account_head": account,
			"description": _("Charged Tip"),
			"tax_amount": tip,
			"cost_center": frappe.db.get_value("POS Profile", doc.pos_profile, "cost_center") or doc.get("cost_center"),
			"included_in_print_rate": 0,
		},
	)


def _tips_account(doc):
	if not doc.get("pos_profile"):
		return None
	return frappe.db.get_value("POS Profile", doc.pos_profile, "custom_tips_payable_account")


def usable_tips_account(company, account):
	ledger = (
		frappe.db.get_value("Account", account, ["company", "root_type", "is_group", "disabled"], as_dict=True)
		if account
		else None
	)
	return bool(company and ledger and ledger.company == company and ledger.root_type == "Liability"
		and not ledger.is_group and not ledger.disabled)


def profile_accepts_guest_tips(pos_profile):
	till = frappe.db.get_value("POS Profile", pos_profile, ["company", "custom_tips_payable_account"], as_dict=True)
	return bool(till and usable_tips_account(till.company, till.custom_tips_payable_account))


def _check_tips_account(doc, account):
	if not usable_tips_account(doc.company, account):
		frappe.throw(
			coded_message(
				"TIP-ACCOUNT",
				_(
					"Set the POS Profile's Tips Payable Account to an enabled "
					"liability account of {0} before taking a tip."
				).format(doc.company),
			)
		)


def _non_cash_overtender(doc):
	"""The part of the over-payment that cash received cannot return as change."""
	precision = _grand_total_precision(doc)
	bill = flt(doc.get("rounded_total")) or flt(doc.get("grand_total"))
	paid = cash = 0
	non_cash_modes = []
	for row in doc.get("payments") or []:
		amount = flt(row.amount)
		if not amount:
			continue
		paid += amount
		if frappe.get_cached_value("Mode of Payment", row.mode_of_payment, "type") == "Cash":
			cash += amount
		elif row.mode_of_payment not in non_cash_modes:
			non_cash_modes.append(row.mode_of_payment)
	excess = paid + flt(doc.get("write_off_amount")) - bill
	return flt(excess - cash, precision), non_cash_modes


def _overtender_rule(mode):
	return frappe.db.get_value("Mode of Payment", mode, "custom_overtender_rule") or "Refuse"
