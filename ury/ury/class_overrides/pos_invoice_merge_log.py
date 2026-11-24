import frappe
from frappe import _
from frappe.utils import cint, getdate, get_time
from erpnext.accounts.doctype.pos_invoice_merge_log.pos_invoice_merge_log import POSInvoiceMergeLog
from erpnext.accounts.party import get_party_gle_currency

from ury.brand import coded_message

class CustomPOSInvoiceMergeLog(POSInvoiceMergeLog):
	def validate(self):
		super().validate()
		self.set_sales_closing_date_and_time()

	def set_sales_closing_date_and_time(self):
		closing_time, closing_date = self.get_sales_closing_time_and_date()
		if closing_time:
			self.posting_time = closing_time
		if closing_date:
			self.posting_date = closing_date

	def get_sales_closing_time_and_date(self):
		"""Returns (closing_time, closing_date) from POS Profile/POS Closing Entry if configured,
		otherwise (None, None).
		"""
		pos_profile = None
		period_start_date = None

		if self.pos_closing_entry:
			closing_data = frappe.db.get_value(
				"POS Closing Entry",
				self.pos_closing_entry,
				["pos_profile", "period_start_date"],
				as_dict=True,
			)
			if closing_data:
				pos_profile = closing_data.get("pos_profile")
				period_start_date = closing_data.get("period_start_date")

		if not pos_profile and self.pos_invoices:
			first_inv = self.pos_invoices[0].pos_invoice
			if first_inv:
				pos_profile = frappe.db.get_value("POS Invoice", first_inv, "pos_profile")
				if not period_start_date:
					pos_opening = frappe.db.get_value("POS Invoice", first_inv, "pos_opening_entry")
					if pos_opening:
						period_start_date = frappe.db.get_value(
							"POS Opening Entry", pos_opening, "period_start_date"
						)

		if not pos_profile:
			return None, None

		profile_fields = []
		if frappe.db.has_column("POS Profile", "custom_sales_closing_time"):
			profile_fields.append("custom_sales_closing_time")
		if frappe.db.has_column("POS Profile", "custom_enable_opening_date"):
			profile_fields.append("custom_enable_opening_date")

		if not profile_fields:
			return None, None

		profile_data = frappe.db.get_value(
			"POS Profile",
			pos_profile,
			profile_fields,
			as_dict=True,
		)
		if not profile_data:
			return None, None

		closing_time = None
		sales_closing_time = profile_data.get("custom_sales_closing_time")
		if sales_closing_time:
			closing_time = get_time(sales_closing_time)

		closing_date = None
		if profile_data.get("custom_enable_opening_date") and period_start_date:
			closing_date = getdate(period_start_date)

		return closing_time, closing_date

	def _resolve_consolidated_receivable_account(self, sales_invoice, data):
		"""Respect ERPNext multi-currency receivable accounting when consolidating foreign-currency tills.

		If the consolidated Sales Invoice's currency differs from Company default currency:
		1. Keep a matching-currency Receivable ``debit_to`` already on the Sales Invoice or source POS Invoices.
		2. Otherwise resolve a matching-currency Receivable ``Party Account`` from Customer or Customer Group.
		3. Otherwise, if neither ``Accounts Settings.allow_multi_currency_invoices_against_single_party_account``
		   nor an existing party GLE currency permits single-account multi-currency posting, fail cleanly with
		   an actionable coded configuration message instead of crashing cryptically during ``save()``.
		"""
		if not data:
			return
		company = sales_invoice.get("company") or self.get("company")
		invoice_currency = sales_invoice.get("currency") or next(
			(doc.get("currency") for doc in data if doc.get("currency")), None
		)
		if not company or not invoice_currency:
			return
		company_currency = frappe.get_cached_value("Company", company, "default_currency")
		if not company_currency or invoice_currency == company_currency:
			return

		def _is_matching_receivable(account):
			if not account:
				return False
			acct = frappe.db.get_value(
				"Account",
				account,
				["company", "account_type", "account_currency", "is_group", "disabled"],
				as_dict=True,
			)
			return bool(
				acct
				and acct.get("company") == company
				and acct.get("account_type") == "Receivable"
				and acct.get("account_currency") == invoice_currency
				and not cint(acct.get("is_group"))
				and not cint(acct.get("disabled"))
			)

		if _is_matching_receivable(sales_invoice.get("debit_to")):
			sales_invoice.party_account_currency = invoice_currency
			return

		for pos_inv in data:
			candidate = pos_inv.get("debit_to")
			if _is_matching_receivable(candidate):
				sales_invoice.debit_to = candidate
				sales_invoice.party_account_currency = invoice_currency
				return

		customer = sales_invoice.get("customer") or self.get("customer")
		if customer:
			for acct in frappe.get_all(
				"Party Account",
				filters={"parenttype": "Customer", "parent": customer, "company": company},
				pluck="account",
			):
				if _is_matching_receivable(acct):
					sales_invoice.debit_to = acct
					sales_invoice.party_account_currency = invoice_currency
					return
			customer_group = frappe.db.get_value("Customer", customer, "customer_group")
			if customer_group:
				for acct in frappe.get_all(
					"Party Account",
					filters={"parenttype": "Customer Group", "parent": customer_group, "company": company},
					pluck="account",
				):
					if _is_matching_receivable(acct):
						sales_invoice.debit_to = acct
						sales_invoice.party_account_currency = invoice_currency
						return

		allow_single_party = cint(
			frappe.db.get_single_value(
				"Accounts Settings", "allow_multi_currency_invoices_against_single_party_account"
			)
		)
		if allow_single_party:
			return
		party_gle_currency = get_party_gle_currency("Customer", customer, company) if customer else None
		if party_gle_currency:
			return

		debit_to = sales_invoice.get("debit_to")
		party_acct_currency = (
			(frappe.db.get_value("Account", debit_to, "account_currency") if debit_to else None)
			or sales_invoice.get("party_account_currency")
			or company_currency
		)
		frappe.throw(
			coded_message(
				"CLOSE-FOREIGN-RECEIVABLE",
				_(
					"Cannot consolidate {0} POS invoices for {1} against {2} receivable account {3}. "
					"Configure a {0} Receivable Party Account for {1} (or its Customer Group) in {4}, "
					"or enable 'Allow Multi-Currency Invoices against Single Party Account' in Accounts Settings."
				).format(
					invoice_currency,
					customer or _("Customer"),
					party_acct_currency,
					debit_to or _("default Debtors"),
					company,
				),
			)
		)

	def process_merging_into_sales_invoice(self, data):
		sales_invoice = self.get_new_sales_invoice()
		sales_invoice = self.merge_pos_invoice_into(sales_invoice, data)
		self._resolve_consolidated_receivable_account(sales_invoice, data)

		closing_time, closing_date = self.get_sales_closing_time_and_date()
		posting_time = closing_time or get_time(self.posting_time)
		posting_date = closing_date or getdate(self.posting_date)

		sales_invoice.is_consolidated = 1
		if getattr(sales_invoice, "flags", None) is not None:
			sales_invoice.flags.is_consolidated = True
		sales_invoice.set_posting_time = 1

		sales_invoice.posting_date = posting_date
		sales_invoice.posting_time = posting_time

		sales_invoice.save()
		sales_invoice.submit()
		self.consolidated_invoice = sales_invoice.name

		return sales_invoice

	def process_merging_into_credit_notes(self, data):
		closing_time, closing_date = self.get_sales_closing_time_and_date()
		credit_notes = {}
		for key, value in data.items():
			if not value:
				continue

			credit_note = self.get_new_sales_invoice()
			credit_note.is_return = 1

			credit_note = self.merge_pos_invoice_into(credit_note, value)
			self._resolve_consolidated_receivable_account(credit_note, value)
			credit_note.return_against = key

			credit_note.is_consolidated = 1
			if getattr(credit_note, "flags", None) is not None:
				credit_note.flags.is_consolidated = True
			credit_note.set_posting_time = 1
			credit_note.posting_date = closing_date or getdate(self.posting_date)
			credit_note.posting_time = closing_time or get_time(self.posting_time)
			credit_note.save()
			credit_note.submit()
			credit_notes[key] = credit_note.name

		return credit_notes
