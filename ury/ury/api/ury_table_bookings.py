"""Read-only URY Table Reservation annotations for the waiter table grid."""

from datetime import timedelta

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from ury.ury.api.table_reservation import grace_period_minutes, non_negative_timing, reservations_enabled
from ury.ury.report.average_table_time.average_table_time import reservation_duration_from

CONFIRMED, SEATED = "Confirmed", "Seated"


@frappe.whitelist(methods=["GET"])
def get_upcoming_bookings(branch):
	if not branch or not reservations_enabled(branch):
		return {}
	if not frappe.has_permission("URY Table Reservation", "read"):
		return {}

	now = now_datetime()  # Frappe's site-local clock, not UTC or the client clock.
	# The averages a till close stored on the Branch: a floor poll never re-reads closed tills.
	# Grace and turn time resolve as check_table_reservation resolves them.
	settings = frappe.db.get_value("Branch", branch, [
		"custom_buffer_time", "custom_grace_period",
		"custom_target_turn_time", "custom_min_avg_table_time",
		"custom_avg_table_time_last_day", "custom_avg_table_time_last_week",
	], as_dict=True) or {}
	stay = timedelta(minutes=reservation_duration_from(
		settings.get("custom_avg_table_time_last_day"), settings.get("custom_avg_table_time_last_week"),
		target_turn_time=non_negative_timing(settings, "target_turn_time"),
		min_avg_table_time=non_negative_timing(settings, "min_avg_table_time")))
	buffer = timedelta(minutes=max(0, cint(settings.get("custom_buffer_time"))))
	grace = timedelta(minutes=grace_period_minutes(settings.get("custom_grace_period")))

	try:
		rows = frappe.get_list(
			"URY Table Reservation",
			filters={"branch": branch, "status": ["in", [CONFIRMED, SEATED]]},
			# A Seated party is at its table whatever its booking time. A Confirmed booking
			# shows within the stay either side of now, and always while it locks its table.
			or_filters={
				"status": SEATED,
				"reserved_at": ["between", [now - max(stay, grace), now + max(stay, buffer)]],
			},
			fields=["reserved_table", "reserved_at", "no_of_pax", "customer_name", "status"],
			order_by="reserved_at asc, name asc",
			limit_page_length=0,
		)
	except frappe.PermissionError:
		return {}

	# One badge per table: a booking locking its table, else the Seated party, else the nearest.
	result = {}
	ranks = {}
	for row in rows:
		table = row["reserved_table"]
		scheduled = get_datetime(row["reserved_at"])
		locking = row["status"] == CONFIRMED and scheduled - buffer <= now < scheduled + grace
		rank = (0 if locking else 1 if row["status"] == SEATED else 2, abs(scheduled - now))
		if table not in ranks or rank < ranks[table]:
			ranks[table] = rank
			result[table] = {
				"scheduled_time": scheduled.isoformat(sep=" ", timespec="seconds"),
				"party_size": row.get("no_of_pax"),
				"customer_name": row.get("customer_name") or "",
				"status": row["status"],
				"server_now": now.isoformat(sep=" ", timespec="seconds"),
			}
	return result
