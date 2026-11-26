import frappe

from frappe.utils import get_datetime
from ury.ury.api.ury_dashboard import _business_day_bounds
from ury.ury.api.kitchen_ticket_history import owned_kots
from ury.ury.api.seating_membership import table_checks
from ury.ury.report_api.utils import require_dashboard_user


def _other_check_in_kitchen(table, current):
	names = table_checks(frappe, table) - {current}
	if not names:
		return False
	checks = frappe.get_all(
		"POS Invoice", filters={"name": ("in", sorted(names)), "docstatus": ("in", [0, 1])},
		fields=["name", "ury_applied_rounds"],
	)
	return any(ticket.order_status != "Served" for check in checks
	           for ticket in owned_kots(frappe, check, active=True, fields=["order_status"]))


@frappe.whitelist(methods=["GET"])
def get_service_line(branch=None):
	require_dashboard_user()
	cache_key = f"ury_dashboard_service_line:{branch}"
	cached = frappe.cache().get_value(cache_key)
	if cached:
		return cached

	table_filters = {"branch": branch} if branch else {}
	tables = frappe.get_all(
		"URY Table",
		filters=table_filters,
		fields=["name", "occupied", "is_take_away", "seating_checks"],
		order_by="name",
	)

	now = get_datetime()
	result = []
	for t in tables:
		if t.is_take_away:
			continue

		if not t.occupied:
			result.append({"table": t.name, "stage": "open", "minutes": None})
			continue

		minutes = None
		invoice = frappe.db.sql(
			"""
			SELECT name, creation FROM `tabPOS Invoice`
			WHERE restaurant_table = %(table)s AND docstatus = 0
			ORDER BY creation DESC LIMIT 1
			""",
			{"table": t.name},
			as_dict=True,
		)

		stage = "seated"
		if invoice:
			minutes = max(0, int((now - get_datetime(invoice[0].creation)).total_seconds() // 60))
			kot = owned_kots(frappe, invoice[0].name, active=True, fields=["order_status"])
			if kot:
				stage = "fired" if any(row.order_status != "Served" for row in kot) else "served"

		if stage != "fired" and _other_check_in_kitchen(t, invoice[0].name if invoice else None):
			stage = "fired"

		if minutes is not None and minutes > 75:
			stage = "over"

		result.append({"table": t.name, "stage": stage, "minutes": minutes})

	frappe.cache().set_value(cache_key, result, expires_in_sec=15)
	return result


def _bin_actual_qty(item_code, warehouse=None):
	"""Total Bin actual_qty for an item, optionally scoped to one warehouse.

	Uses get_all + Python sum (valid on Frappe v15 and v16; v16 rejects
	aggregate expressions as get_value/get_all fields).
	"""
	filters = {"item_code": item_code}
	if warehouse:
		filters["warehouse"] = warehouse
	return float(sum(frappe.get_all("Bin", filters=filters, pluck="actual_qty")))


@frappe.whitelist(methods=["GET"])
def get_running_low(branch=None):
	require_dashboard_user()
	cache_key = f"ury_dashboard_running_low:{branch}"
	cached = frappe.cache().get_value(cache_key)
	if cached:
		return cached

	warehouse = None
	if branch:
		pos_profile = frappe.db.get_value("POS Profile", {"branch": branch}, "warehouse")
		warehouse = pos_profile

	now = get_datetime()
	shift_start, shift_end = _business_day_bounds(branch, now=now)
	hours_elapsed = max((now - shift_start).total_seconds() / 3600, 0.5)

	sold_conditions = "inv.`docstatus` = 1 AND TIMESTAMP(inv.`posting_date`, inv.`posting_time`) >= %(start)s AND TIMESTAMP(inv.`posting_date`, inv.`posting_time`) < %(end)s AND item.`is_stock_item` = 1"
	sold_params = {"start": shift_start, "end": shift_end}
	if branch:
		sold_conditions += " AND inv.`branch` = %(branch)s"
		sold_params["branch"] = branch

	sold_rows = frappe.db.sql(
		f"""
		SELECT ii.`item_code` AS item_code, ii.`item_name` AS item_name, SUM(ii.`qty`) AS qty_sold
		FROM `tabPOS Invoice Item` ii
		JOIN `tabPOS Invoice` inv ON inv.`name` = ii.`parent`
		JOIN `tabItem` item ON item.`name` = ii.`item_code`
		WHERE {sold_conditions}
		GROUP BY ii.`item_code`, ii.`item_name`
		HAVING qty_sold > 0
		ORDER BY qty_sold DESC
		LIMIT 20
		""",
		sold_params,
		as_dict=True,
	)

	result = []
	for row in sold_rows:
		actual_qty = _bin_actual_qty(row.item_code, warehouse)

		data_quality_issue = actual_qty < 0
		remaining = max(actual_qty, 0)

		sell_rate_per_hour = row.qty_sold / hours_elapsed
		eta_minutes = round((remaining / sell_rate_per_hour) * 60) if sell_rate_per_hour > 0 else None

		result.append({
			"item_code": row.item_code,
			"item_name": row.item_name,
			"remaining": remaining,
			"qty_sold_today": row.qty_sold,
			"eta_minutes": eta_minutes,
			"data_quality_issue": data_quality_issue,
		})

	result.sort(key=lambda r: (r["eta_minutes"] is None, r["eta_minutes"]))

	frappe.cache().set_value(cache_key, result[:6], expires_in_sec=60)
	return result[:6]
