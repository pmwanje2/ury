"""Database-backed till fixtures for the tender, tip and cash-movement tests.

Same convention as ``ury/ury_pos/test_closing_two_cashiers.py``: real Branch,
POS Profile, Users and POS Opening Entry on ``_Test Company``. Each till gets
its own POS Profile, so a test can open and close a till without colliding
with another test's open shift (ERPNext allows one open entry per profile).
"""

import frappe

COMPANY = "_Test Company"
BRANCH = "_Test Till Money Branch"
OTHER_BRANCH = "_Test Till Money Other Branch"
ROOM = "_Test Till Money Room"
RESTAURANT = "_Test Till Money Restaurant"
CUSTOMER = "_Test Till Money Customer"
ITEM = "_Test Non Stock Item"
WAITER = "till-money-waiter@example.com"
MANAGER = "till-money-manager@example.com"
MANAGER_2 = "till-money-manager-2@example.com"
PURE_CASHIER = "till-money-pure-cashier@example.com"
PURE_MANAGER = "till-money-pure-manager@example.com"
PURE_WAITER = "till-money-pure-waiter@example.com"
OTHER_BRANCH_USER = "till-money-other-branch@example.com"
ROLES = ("Accounts Manager", "Accounts User", "Sales Manager", "Stock User", "Item Manager")

CASH = "Cash"
CARD = "Credit Card"
WIRE = "Wire Transfer"
CASH_ACCOUNT = "Cash - _TC"
CARD_ACCOUNT = "_Test Bank - _TC"
WIRE_ACCOUNT = "HDFC - _TC"
SAFE_ACCOUNT = "_Test Cash - _TC"
TIPS_ACCOUNT = "_Test Tips Payable - _TC"
OVER_SHORT_ACCOUNT = "_Test Cash Over Short - _TC"
PAID_OUT_ACCOUNT = "Miscellaneous Expenses - _TC"
VAT_ACCOUNT = "_Test Output VAT - _TC"
INCOME_ACCOUNT = "Sales - _TC"
COST_CENTER = "_Test Cost Center - _TC"
APPROVER_ROLE = "URY Manager"


def ensure_masters():
	frappe.set_user("Administrator")
	frappe.db.set_single_value("POS Settings", "invoice_type", "POS Invoice")
	for email in (WAITER, MANAGER, MANAGER_2):
		_make_user(email)
	frappe.get_doc("User", MANAGER).add_roles(APPROVER_ROLE)
	frappe.get_doc("User", MANAGER_2).add_roles(APPROVER_ROLE)
	_make_role_only_user(PURE_CASHIER, "URY Cashier")
	_make_role_only_user(PURE_MANAGER, APPROVER_ROLE)
	_make_role_only_user(PURE_WAITER, "URY Captain")
	_make_role_only_user(OTHER_BRANCH_USER, "URY Cashier")
	for branch_name in (BRANCH, OTHER_BRANCH):
		if not frappe.db.exists("Branch", branch_name):
			branch = frappe.get_doc({"doctype": "Branch", "branch": branch_name})
			branch.flags.ignore_mandatory = True
			branch.insert(ignore_permissions=True)
	for email in (PURE_CASHIER, PURE_MANAGER, PURE_WAITER, MANAGER, MANAGER_2):
		assign_branch_user(BRANCH, email)
	assign_branch_user(OTHER_BRANCH, OTHER_BRANCH_USER)
	if not frappe.db.exists("Customer", CUSTOMER):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": CUSTOMER,
				"customer_group": "Individual",
				"territory": "All Territories",
			}
		).insert(ignore_permissions=True, ignore_mandatory=True)
	if not frappe.db.exists("URY Room", ROOM):
		frappe.get_doc({"doctype": "URY Room", "name": ROOM, "branch": BRANCH}).insert(ignore_permissions=True)
	if not frappe.db.exists("URY Restaurant", RESTAURANT):
		frappe.get_doc(
			{
				"doctype": "URY Restaurant",
				"name": RESTAURANT,
				"company": COMPANY,
				"invoice_series_prefix": "TMF",
				"branch": BRANCH,
				"default_room": ROOM,
			}
		).insert(ignore_permissions=True)
	_leaf(TIPS_ACCOUNT, "Current Liabilities - _TC", "Liability")
	_leaf(OVER_SHORT_ACCOUNT, "Indirect Expenses - _TC", "Expense")
	_leaf(VAT_ACCOUNT, "Duties and Taxes - _TC", "Liability", account_type="Tax")
	for mode, account in ((CARD, CARD_ACCOUNT), (WIRE, WIRE_ACCOUNT), (CASH, CASH_ACCOUNT)):
		_map_mode(mode, account)
	set_overtender_rule(CARD, "Refuse")
	set_overtender_rule(WIRE, "Refuse")


def _make_user(email):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	frappe.get_doc("User", email).add_roles(*ROLES)


def _make_role_only_user(email, *roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	user.set("roles", [{"role": role} for role in roles])
	user.save(ignore_permissions=True)


def assign_branch_user(branch_name, email):
	branch = frappe.get_doc("Branch", branch_name)
	if not any(row.user == email for row in (branch.get("user") or [])):
		branch.append("user", {"user": email})
		branch.flags.ignore_mandatory = True
		branch.save(ignore_permissions=True)


def _leaf(name, parent, root_type, account_type=""):
	if frappe.db.exists("Account", name):
		return
	frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": name.rsplit(" - ", 1)[0],
			"parent_account": parent,
			"company": COMPANY,
			"is_group": 0,
			"root_type": root_type,
			"account_type": account_type,
		}
	).insert(ignore_permissions=True)


def _map_mode(mode, account):
	doc = frappe.get_doc("Mode of Payment", mode)
	if not any(row.company == COMPANY for row in doc.accounts):
		doc.append("accounts", {"company": COMPANY, "default_account": account})
		doc.save(ignore_permissions=True)


def set_overtender_rule(mode, rule):
	doc = frappe.get_doc("Mode of Payment", mode)
	doc.custom_overtender_rule = rule
	doc.save(ignore_permissions=True)


def make_till(name, holder=None, applicable_users=None, **settings):
	"""A POS Profile named ``name`` with Cash, Card and Wire tenders, plus an
	open POS Opening Entry held by its own holder (URY lets a cashier hold one
	open till at a time). ``settings`` are POS Profile fields."""
	frappe.set_user("Administrator")
	if holder is None:
		holder = frappe.scrub(name).strip("_").replace("_", "-") + "@example.com"
		_make_user(holder)
	assign_branch_user(BRANCH, holder)
	defaults = {
		"custom_tips_payable_account": TIPS_ACCOUNT,
		"custom_cash_safe_account": SAFE_ACCOUNT,
		"custom_paid_out_account": PAID_OUT_ACCOUNT,
		"custom_cash_over_short_account": OVER_SHORT_ACCOUNT,
		"custom_paid_out_approval_threshold": 500,
		"custom_cash_movement_approver_role": APPROVER_ROLE,
	}
	defaults.update(settings)
	if frappe.db.exists("POS Profile", name):
		frappe.delete_doc("POS Profile", name, ignore_permissions=True, force=1)
	profile = frappe.get_doc(
		{
			"doctype": "POS Profile",
			"name": name,
			"company": COMPANY,
			"currency": "INR",
			"warehouse": "_Test Warehouse - _TC",
			"cost_center": COST_CENTER,
			"income_account": INCOME_ACCOUNT,
			"expense_account": "Cost of Goods Sold - _TC",
			"write_off_account": "_Test Write Off - _TC",
			"write_off_cost_center": COST_CENTER,
			"account_for_change_amount": CASH_ACCOUNT,
			"selling_price_list": "Standard Selling",
			"branch": BRANCH,
			"restaurant": RESTAURANT,
			**defaults,
		}
	)
	profile.append("payments", {"mode_of_payment": CASH, "default": 1})
	profile.append("payments", {"mode_of_payment": CARD})
	profile.append("payments", {"mode_of_payment": WIRE})
	users = list(dict.fromkeys([holder, WAITER, *(applicable_users or [])]))
	for email in users:
		profile.append("applicable_for_users", {"user": email})
	profile.insert(ignore_permissions=True)

	opening = frappe.new_doc("POS Opening Entry")
	opening.pos_profile = name
	opening.user = holder
	opening.company = COMPANY
	opening.period_start_date = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-5)
	opening.posting_date = frappe.utils.nowdate()
	opening.branch = BRANCH
	opening.restaurant = RESTAURANT
	opening.set("balance_details", [{"mode_of_payment": CASH, "opening_amount": 1000}])
	opening.insert(ignore_permissions=True)
	opening.submit()
	return frappe._dict(profile=profile, opening=opening, name=name, holder=holder)


def bill(
	till,
	rate,
	payments,
	tip=None,
	waiter=WAITER,
	vat_rate=None,
	submit=True,
	additional_discount_percentage=None,
	discount_amount=None,
	apply_discount_on=None,
	vat_inclusive=False,
	disable_rounded_total=None,
):
	"""A POS Invoice for one ``rate`` item on ``till``, paid by ``payments``
	(``[(mode, amount), ...]``), created by the till's holder."""
	frappe.set_user(till.holder)
	try:
		invoice = frappe.new_doc("POS Invoice")
		invoice.pos_profile = till.name
		invoice.company = COMPANY
		invoice.customer = CUSTOMER
		invoice.is_pos = 1
		invoice.update_stock = 0
		invoice.waiter = waiter
		if apply_discount_on:
			invoice.apply_discount_on = apply_discount_on
		if additional_discount_percentage is not None:
			invoice.additional_discount_percentage = additional_discount_percentage
		if discount_amount is not None:
			invoice.discount_amount = discount_amount
		if additional_discount_percentage is not None or discount_amount is not None:
			invoice.custom_discount_reason = "Test Discount"
			approver = MANAGER if till.holder != MANAGER else MANAGER_2
			invoice.flags.discount_approval = {
				"approver": approver,
				"percent": additional_discount_percentage or 0,
			}
		if disable_rounded_total is not None:
			invoice.disable_rounded_total = disable_rounded_total
		invoice.append(
			"items",
			{
				"item_code": ITEM,
				"qty": 1,
				"rate": rate,
				"income_account": INCOME_ACCOUNT,
				"cost_center": COST_CENTER,
			},
		)
		if vat_rate:
			invoice.append(
				"taxes",
				{
					"charge_type": "On Net Total",
					"account_head": VAT_ACCOUNT,
					"description": "VAT",
					"rate": vat_rate,
					"cost_center": COST_CENTER,
					"included_in_print_rate": 1 if vat_inclusive else 0,
				},
			)
		if tip is not None:
			invoice.custom_charged_tip = tip
		for mode, amount in payments:
			invoice.append("payments", {"mode_of_payment": mode, "amount": amount})
		invoice.insert(ignore_permissions=True)
		if submit:
			invoice.submit()
	finally:
		frappe.set_user("Administrator")
	return invoice


def close_till(till, counted):
	"""Submit a POS Closing Entry for ``till`` with ``counted`` cash, as its holder."""
	frappe.set_user(till.holder)
	try:
		closing = frappe.new_doc("POS Closing Entry")
		closing.pos_opening_entry = till.opening.name
		closing.pos_profile = till.name
		closing.company = COMPANY
		closing.user = till.holder
		closing.period_start_date = till.opening.period_start_date
		closing.period_end_date = frappe.utils.now_datetime()
		closing.posting_date = frappe.utils.nowdate()
		closing.posting_time = frappe.utils.nowtime()
		closing.append(
			"payment_reconciliation",
			{"mode_of_payment": CASH, "opening_amount": 1000, "closing_amount": counted},
		)
		closing.insert(ignore_permissions=True)
		closing.submit()
	finally:
		frappe.set_user("Administrator")
	closing.reload()
	return closing


def gl_by_account(voucher_type, voucher_no):
	rows = frappe.get_all(
		"GL Entry",
		filters={"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
		fields=["account", "debit", "credit"],
	)
	totals = {}
	for row in rows:
		debit, credit = totals.get(row.account, (0, 0))
		totals[row.account] = (debit + row.debit, credit + row.credit)
	return totals
