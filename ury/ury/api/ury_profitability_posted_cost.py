"""Read complete fulfilment posting evidence for sold invoice items."""

from collections import Counter, defaultdict

import frappe
from frappe.utils import flt
from ury.ury.api.kitchen_ticket_history import ticket_owner_query


def load_posted_costs(company, branch, invoice_lines):
	if not invoice_lines:
		return {}
	postings = _read_postings(company, branch, {line["parent"] for line in invoice_lines})
	if not postings:
		return {}

	# A shared posting cannot be charged in full to several fulfilments.
	references = {row["posting_reference"] for row in postings}
	linked_records = frappe.get_all(
		"URY Fulfilment Record",
		filters={"posting_reference": ["in", list(references)]},
		fields=["posting_reference"],
	)
	reference_counts = Counter(row["posting_reference"] for row in linked_records)
	groups = defaultdict(list)
	for posting in postings:
		groups[(posting["invoice"], posting["item_code"])].append(posting)
	costs = {}
	for line in invoice_lines:
		key = (line["parent"], line["item_code"])
		rows = groups.get(key, [])
		if not rows or any(reference_counts[row["posting_reference"]] != 1 for row in rows):
			continue
		if any(flt(row["qty"]) <= 0 for row in rows):
			continue
		posted_qty = sum(flt(row["qty"]) for row in rows)
		if flt(line["qty"]) <= 0 or abs(posted_qty - flt(line["qty"])) > 0.000001:
			continue
		costs[key] = sum(flt(row["posted_cost"]) for row in rows)
	return costs


def _read_postings(company, branch, invoices):
	query, params = ticket_owner_query(frappe, """
        SELECT owner.invoice, f.item_code, f.qty, f.posting_reference, e.total_outgoing_value AS posted_cost
        FROM `tabURY Fulfilment Record` f
        JOIN `tabURY KOT` k ON k.name = f.kot
        JOIN {ticket_owners} owner ON owner.ticket = k.name
        JOIN `tabPOS Invoice` i ON i.name = owner.invoice
        JOIN `tabStock Entry` e ON e.name = f.posting_reference
        WHERE f.company = %(company)s AND f.branch = k.branch AND f.posted_to_erpnext = 1
          AND i.company = %(company)s AND i.branch = %(branch)s AND k.docstatus = 1
          AND owner.invoice IN %(checks)s AND e.company = %(company)s AND e.docstatus = 1
          AND e.purpose IN ('Manufacture', 'Material Issue')
    """, dict(company=company, branch=branch, checks=tuple(invoices)), invoices=invoices)
	return frappe.db.sql(query, params, as_dict=True)
