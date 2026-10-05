import frappe
import frappe.sessions
from ury.brand import get_brand_context

no_cache = 1


def get_context(context):
	context.update(get_brand_context())
	csrf_token = frappe.sessions.get_csrf_token()
	# Persist a newly generated token before the page is returned.
	frappe.db.commit()  # nosemgrep
	context["csrf_token"] = csrf_token
	return context
