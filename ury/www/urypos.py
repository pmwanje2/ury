import frappe
import frappe.sessions
from ury.brand import get_brand_context
from ury.ury_pos.floor_pin import get_floor_pad

no_cache = 1


def get_context(context):
	context.update(get_brand_context(surface="staff"))
	csrf_token = frappe.sessions.get_csrf_token()
	# Persist a newly generated token before the page is returned.
	frappe.db.commit()  # nosemgrep
	context["csrf_token"] = csrf_token
	context["floor_pad"] = get_floor_pad()
	return context
