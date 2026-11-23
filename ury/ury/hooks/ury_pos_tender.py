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

OVERTENDER_TO_TIP = "Post to Charged Tip"


def before_validate(doc, method=None):
	"""Runs before the native POS Invoice validate computes paid and change
	amounts, so the native change can only ever come from cash received."""
	if doc.get("is_return"):
		return
	sync_charged_tip(doc)
	doc.calculate_taxes_and_totals()
	over, non_cash_modes = _non_cash_overtender(doc)
	if over <= 0:
		return
	refusing = [mode for mode in non_cash_modes if _overtender_rule(mode) != OVERTENDER_TO_TIP]
	if refusing:
		frappe.throw(
			_(
				"[URY-TENDER-OVERTENDER] {0} cannot be over-tendered: only cash gives change. "
				"Reduce it to the balance due."
			).format(", ".join(refusing))
		)
	doc.custom_charged_tip = flt(doc.get("custom_charged_tip")) + over
	sync_charged_tip(doc)
	doc.calculate_taxes_and_totals()


def sync_charged_tip(doc):
	"""Make the bill's Tips Payable row match ``custom_charged_tip``."""
	tip = flt(doc.get("custom_charged_tip"), doc.precision("grand_total"))
	if tip < 0:
		frappe.throw(_("[URY-TIP-NEGATIVE] A tip cannot be negative."))
	account = (
		frappe.db.get_value("POS Profile", doc.pos_profile, "custom_tips_payable_account")
		if doc.get("pos_profile")
		else None
	)
	if account:
		kept = [row for row in doc.get("taxes") or [] if row.account_head != account]
		doc.set("taxes", kept)
		for position, row in enumerate(kept, start=1):
			row.idx = position
	if not tip:
		return
	_check_tips_account(doc, account)
	doc.custom_tip_server = doc.get("custom_tip_server") or doc.get("waiter") or doc.get("owner") or frappe.session.user
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


def _check_tips_account(doc, account):
	ledger = (
		frappe.db.get_value("Account", account, ["company", "root_type", "is_group", "disabled"], as_dict=True)
		if account
		else None
	)
	if not ledger or ledger.company != doc.company or ledger.root_type != "Liability" or ledger.is_group or ledger.disabled:
		frappe.throw(
			_(
				"[URY-TIP-ACCOUNT] Set the POS Profile's Tips Payable Account to an enabled "
				"liability account of {0} before taking a tip."
			).format(doc.company)
		)


def _non_cash_overtender(doc):
	"""The part of the over-payment that cash received cannot return as change."""
	precision = doc.precision("grand_total")
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
