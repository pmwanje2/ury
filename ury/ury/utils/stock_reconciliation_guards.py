import frappe

# Maximus fork: the guard is opt-in. SAP allows parallel physical-inventory documents, and the
# blind-count and kitchen-count flows keep more than one draft per branch.
SITE_CONFIG_GATE = "ury_stock_reconciliation_single_draft"


def validate(doc, method):
	"""Prevent creating multiple Draft Stock Reconciliations for the same branch.

	A Stock Reconciliation in Draft status represents work in progress for a branch.
	Only one draft per branch is allowed at a time to prevent reconciliation conflicts.
	Runs only when site config `ury_stock_reconciliation_single_draft` is truthy.
	"""
	if not frappe.conf.get(SITE_CONFIG_GATE):
		return
	draft_check = frappe.db.exists(
		"Stock Reconciliation",
		{"branch": doc.branch, "docstatus": 0}
	)
	if draft_check and draft_check != doc.name:
		frappe.throw(
			f"There is already a Stock Reconciliation in Draft status for branch {doc.branch}. "
			"Please complete or discard the existing reconciliation before creating a new one."
		)
