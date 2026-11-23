"""Read-only URY Table Reservation annotations for the waiter table grid."""

from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

from ury.ury.api.table_reservation import reservations_enabled
from ury.ury.report.average_table_time.average_table_time import get_branch_reservation_duration


@frappe.whitelist(methods=["GET"])
def get_upcoming_bookings(branch):
	if not branch or not reservations_enabled(branch):
		return {}
	if not frappe.has_permission("URY Table Reservation", "read"):
		return {}

	now = now_datetime()  # Frappe's site-local clock, not UTC or the client clock.
	window = timedelta(minutes=float(get_branch_reservation_duration(branch)))

	try:
		rows = frappe.get_list(
			"URY Table Reservation",
			filters={
				"branch": branch,
				"status": ["in", ["Confirmed", "Seated"]],
				"reserved_at": ["between", [now - window, now + window]],
			},
			fields=["reserved_table", "reserved_at", "no_of_pax", "customer_name", "status"],
			order_by="reserved_at asc, name asc",
			limit_page_length=0,
		)
	except frappe.PermissionError:
		return {}

	result = {}
	distances = {}
	for row in rows:
		table = row["reserved_table"]
		scheduled = get_datetime(row["reserved_at"])
		distance = abs(scheduled - now)
		if table not in distances or distance < distances[table]:
			distances[table] = distance
			result[table] = {
				"scheduled_time": scheduled.isoformat(sep=" ", timespec="seconds"),
				"party_size": row.get("no_of_pax"),
				"customer_name": row.get("customer_name") or "",
				"status": row["status"],
				"server_now": now.isoformat(sep=" ", timespec="seconds"),
			}
	return result
