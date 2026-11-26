import json
from hashlib import sha256

import frappe
import frappe.sessions
from frappe import _
from frappe.utils.telemetry import capture

from ury.brand import get_brand, get_brand_context, get_theme_context, json_for_script
from ury.ury_pos.floor_pin import get_floor_pad

no_cache = 1

def _receipt_copy_session(csrf_token):
	return sha256(("receipt-copy:" + csrf_token).encode()).hexdigest()


def get_context(context):
	context.update(get_brand_context(surface="staff"))
	context.update(get_theme_context())
	csrf_token = frappe.sessions.get_csrf_token()
	# Manually commit the CSRF token here
	frappe.db.commit()  # nosemgrep

	if frappe.session.user == "Guest":
		boot = frappe.website.utils.get_boot_data()
	else:
		try:
			boot = frappe.sessions.get()
		except Exception as e:
			raise frappe.SessionBootFailed from e

	# add server_script_enabled in boot
	if "server_script_enabled" in frappe.conf:
		enabled = frappe.conf.server_script_enabled
	else:
		enabled = True
	boot["server_script_enabled"] = enabled

	# Bind reloadable copy requests to this login without storing a session secret in the browser queue.
	boot["receipt_copy_session"] = _receipt_copy_session(csrf_token)
	boot["ury_brand"] = context["brand"]
	boot_json = json.dumps(json_for_script(boot))

	context.update(
		{"build_version": frappe.utils.get_build_version(), "boot": boot_json, "csrf_token": csrf_token}
	)
	context["floor_pad"] = get_floor_pad()
	return context


@frappe.whitelist(methods=["POST"])
def get_context_for_dev():
	if not frappe.conf.developer_mode:
		frappe.throw(_("This method is only meant for developer mode"))
	if "System Manager" not in frappe.get_roles():
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return json.loads(get_boot())


def get_boot():
	try:
		boot = frappe.sessions.get()
	except Exception as e:
		raise frappe.SessionBootFailed from e

	boot["receipt_copy_session"] = _receipt_copy_session(frappe.sessions.get_csrf_token())
	boot["push_relay_server_url"] = frappe.conf.get("push_relay_server_url")
	boot["ury_brand"] = get_brand(surface="staff")
	boot_json = json.dumps(json_for_script(boot))

	return boot_json
