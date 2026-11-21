"""Read-only Appointment annotations for the waiter table grid."""

from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime


@frappe.whitelist(methods=["GET"])
def get_upcoming_bookings(branch):
	if not branch or not frappe.has_permission("Appointment", "read"):
		return {}
	meta = frappe.get_meta("Appointment")
	if not meta.has_field("custom_ury_table"):
		return {}

	now = now_datetime()  # Frappe's site-local clock, not UTC or the client clock.
	duration = frappe.db.get_single_value("Appointment Booking Settings", "appointment_duration") or 120
	window = timedelta(minutes=float(duration))
	fields = ["custom_ury_table", "scheduled_time", "customer_name"]
	if meta.has_field("custom_party_size"):
		fields.append("custom_party_size")

	try:
		tables = frappe.get_list(
			"URY Table", filters={"branch": branch}, pluck="name", limit_page_length=0,
		)
		if not tables:
			return {}
		rows = frappe.get_list(
			"Appointment",
			filters={
				"custom_ury_table": ["in", tables],
				"status": "Open",
				"scheduled_time": ["between", [now - window, now + window]],
			},
			fields=fields,
			order_by="scheduled_time asc, name asc",
			limit_page_length=0,
		)
	except frappe.PermissionError:
		return {}

	result = {}
	distances = {}
	for row in rows:
		table = row["custom_ury_table"]
		scheduled = get_datetime(row["scheduled_time"])
		distance = abs(scheduled - now)
		if table not in distances or distance < distances[table]:
			distances[table] = distance
			result[table] = {
				"scheduled_time": scheduled.isoformat(sep=" ", timespec="seconds"),
				"party_size": row.get("custom_party_size"),
				"customer_name": row.get("customer_name") or "",
				"server_now": now.isoformat(sep=" ", timespec="seconds"),
			}
	return result
