"""Real native documents for offline POS regression tests; no runtime hooks."""

from types import SimpleNamespace

import frappe
from frappe.utils import now_datetime


def _ensure_masters():
    # A blank test site (setup wizard not run) has no tree roots or UOMs.
    if not frappe.db.exists("UOM", "Nos"):
        frappe.get_doc({"doctype": "UOM", "uom_name": "Nos"}).insert()
    for doctype, root, leaf, field in (
        ("Item Group", "All Item Groups", "Offline Products", "item_group_name"),
        ("Customer Group", "All Customer Groups", "Offline Individual", "customer_group_name"),
        ("Territory", "All Territories", "Offline Territory", "territory_name"),
    ):
        if not frappe.db.exists(doctype, root):
            frappe.get_doc({"doctype": doctype, field: root, "is_group": 1}).insert()
        if not frappe.db.get_value(doctype, {"is_group": 0}):
            frappe.get_doc({"doctype": doctype, field: leaf, "is_group": 0,
                frappe.scrub("parent " + doctype): root}).insert()


def _ensure_company_fiscal_year(company, today):
    """Ensure `company` belongs to an active Fiscal Year covering `today`, even on a
    populated multi-company site whose active Fiscal Year lists only other companies."""
    active_fys = frappe.get_all(
        "Fiscal Year",
        filters={
            "year_start_date": ["<=", today],
            "year_end_date": [">=", today],
            "disabled": 0,
        },
        pluck="name",
    )
    for fy_name in active_fys or []:
        fy_companies = frappe.get_all(
            "Fiscal Year Company",
            filters={"parent": fy_name, "parenttype": "Fiscal Year"},
            pluck="company",
        )
        if not fy_companies or company in fy_companies:
            return
    if active_fys:
        fy = frappe.get_doc("Fiscal Year", active_fys[0])
        fy.append("companies", {"company": company})
        fy.save(ignore_permissions=True)
    else:
        frappe.get_doc(
            {
                "doctype": "Fiscal Year",
                "year": f"Offline FY {today.year}",
                "year_start_date": today.replace(month=1, day=1),
                "year_end_date": today.replace(month=12, day=31),
                "companies": [{"company": company}],
            }
        ).insert()


def make_offline_pos():
    frappe.set_user("Administrator")
    _ensure_masters()
    frappe.db.set_single_value("POS Settings", "invoice_type", "POS Invoice")
    suffix = frappe.generate_hash(length=8)
    company = "URY"
    if not frappe.db.exists("Warehouse Type", "Transit"):
        frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert()
    if not frappe.db.exists("Company", company):
        frappe.get_doc({
            "doctype": "Company", "company_name": company, "abbr": "URY",
            "country": "India", "default_currency": "INR",
        }).insert()
    today = now_datetime().date()
    _ensure_company_fiscal_year(company, today)
    user = frappe.get_doc({
        "doctype": "User", "email": f"offline-{suffix}@example.com",
        "first_name": "Offline Test Cashier", "send_welcome_email": 0,
    }).insert()
    user.add_roles("URY Cashier")
    branch = frappe.get_doc({
        "doctype": "Branch", "branch": f"Offline Branch {suffix}", "company": company,
        "user": [{"user": user.name}],
    }).insert()
    room = frappe.get_doc({
        "doctype": "URY Room", "name": f"Offline Room {suffix}", "branch": branch.name,
    }).insert()
    restaurant = frappe.get_doc({
        "doctype": "URY Restaurant", "name": f"Offline Restaurant {suffix}",
        "company": company, "branch": branch.name, "default_room": room.name,
        "invoice_series_prefix": f"OFF-{suffix}-.#####",
    }).insert()
    group = frappe.db.get_value("Item Group", {"is_group": 0})
    uom = frappe.db.get_value("UOM", {"enabled": 1}) or "Nos"
    item = frappe.get_doc({
        "doctype": "Item", "item_code": f"Offline Tea {suffix}",
        "item_name": "Offline Tea", "item_group": group, "stock_uom": uom,
        "is_stock_item": 0,
    }).insert()
    menu = frappe.get_doc({
        "doctype": "URY Menu", "name": f"Offline Menu {suffix}", "branch": branch.name,
        "items": [{"item": item.name, "item_name": item.item_name, "rate": 100}],
    }).insert()
    restaurant.active_menu = menu.name
    restaurant.save()
    cost_center = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0})
    warehouse = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0})
    cash_account = frappe.db.get_value("Account", {
        "company": company, "is_group": 0, "account_type": "Cash",
    })
    if not frappe.db.exists("Mode of Payment", "Cash"):
        frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": "Cash", "type": "Cash"}).insert()
    payment = frappe.get_doc("Mode of Payment", "Cash")
    if not any(row.company == company for row in payment.accounts):
        payment.append("accounts", {"company": company, "default_account": cash_account})
        payment.save()
    profile = frappe.get_doc({
        "doctype": "POS Profile", "name": f"Offline Till {suffix}", "company": company,
        "currency": "INR", "customer": None, "warehouse": warehouse,
        "cost_center": cost_center, "write_off_cost_center": cost_center,
        "write_off_account": frappe.db.get_value("Account", {
            "company": company, "is_group": 0, "account_name": ["like", "%Write Off%"],
        }),
        "selling_price_list": menu.price_list, "branch": branch.name,
        "restaurant": restaurant.name, "custom_kot_naming_series": f"KOT-{suffix}-.#####",
        "role_allowed_for_billing": [{"role": "URY Cashier"}], "remove_items": 1,
        "ury_offline_capture": 1,
        "applicable_for_users": [{"user": user.name}],
        "payments": [{"mode_of_payment": "Cash", "default": 1}],
    }).insert()
    customer = frappe.get_doc({
        "doctype": "Customer", "customer_name": f"Offline Guest {suffix}",
        "customer_type": "Individual",
        "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}),
        "territory": frappe.db.get_value("Territory", {"is_group": 0}),
    }).insert()
    profile.customer = customer.name
    profile.save()
    department = frappe.get_doc({
        "doctype": "URY Production Department", "department_name": f"Offline Dept {suffix}",
        "company": company, "branch": branch.name, "department_warehouse": warehouse,
        "cost_center": cost_center, "enabled": 1,
    }).insert()
    production = frappe.get_doc({
        "doctype": "URY Production Unit", "production": f"Offline Kitchen {suffix}",
        "department": department.name, "company": company, "enabled": 1,
        "pos_profile": profile.name, "branch": branch.name, "warehouse": warehouse,
        "item_groups": [{"item_group": group}],
    }).insert()
    # The URY POS stock gate on a version-16 bench without ERPNext's batched
    # check also refuses non-stock items; the native "Always Available"
    # configuration bypasses it.
    frappe.get_doc({
        "doctype": "URY Item Production Configuration", "item": item.name,
        "branch": branch.name, "availability_mode": "Always Available", "active": 1,
        "department": department.name, "production_unit": production.name,
        "controlled_by_sales_plan": 0,
    }).insert()
    opening = frappe.get_doc({
        "doctype": "POS Opening Entry", "company": company, "pos_profile": profile.name,
        "user": user.name, "branch": branch.name, "restaurant": restaurant.name,
        "period_start_date": now_datetime().replace(hour=0, minute=0, second=0, microsecond=0),
        "balance_details": [{"mode_of_payment": "Cash", "opening_amount": 0}],
    }).insert()
    opening.submit()
    frappe.set_user(user.name)
    return SimpleNamespace(
        user=user, branch=branch, room=room, restaurant=restaurant, item=item,
        menu=menu, profile=profile, customer=customer, production=production, opening=opening,
    )


def make_made_to_order_item(context):
    """A native made-to-order menu item with a BOM, raw stock and POS Stock Authority V2 on."""
    frappe.set_user("Administrator")
    suffix = frappe.generate_hash(length=8)
    group = context.item.item_group
    uom = context.item.stock_uom
    raw = frappe.get_doc({
        "doctype": "Item", "item_code": f"Offline Flour {suffix}", "item_name": "Offline Flour",
        "item_group": group, "stock_uom": uom, "is_stock_item": 1, "valuation_rate": 10,
    }).insert()
    dish = frappe.get_doc({
        "doctype": "Item", "item_code": f"Offline Chapati {suffix}", "item_name": "Offline Chapati",
        "item_group": group, "stock_uom": uom, "is_stock_item": 1, "valuation_rate": 20,
    }).insert()
    bom = frappe.get_doc({
        "doctype": "BOM", "item": dish.name, "quantity": 1, "company": context.restaurant.company,
        "is_active": 1, "is_default": 1, "with_operations": 0,
        "items": [{"item_code": raw.name, "qty": 1, "uom": uom, "rate": 10}],
    }).insert()
    bom.submit()
    department = frappe.db.get_value("URY Production Unit", context.production.name, "department")
    warehouse = frappe.db.get_value("URY Production Department", department, "department_warehouse")
    frappe.get_doc({
        "doctype": "URY Item Production Configuration", "item": dish.name, "branch": context.branch.name,
        "department": department, "production_unit": context.production.name, "active": 1,
        "production_policy": "MADE_TO_ORDER", "bom": bom.name,
        "availability_mode": "Always Available", "controlled_by_sales_plan": 0,
    }).insert()
    receipt = frappe.get_doc({
        "doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "purpose": "Material Receipt",
        "company": context.restaurant.company,
        "items": [{"item_code": raw.name, "qty": 10, "t_warehouse": warehouse, "basic_rate": 10,
                   "uom": uom, "conversion_factor": 1}],
    }).insert()
    receipt.submit()
    menu = frappe.get_doc("URY Menu", context.menu.name)
    menu.append("items", {"item": dish.name, "item_name": dish.item_name, "rate": 100})
    menu.save()
    frappe.get_doc({
        "doctype": "URY Branch Stock Policy", "branch": context.branch.name,
        "reservation_control_enabled": 1, "realtime_production_posting_enabled": 1,
    }).insert()
    # The resolver memoises per request; drop any value read before the policy existed.
    frappe.local.ury_branch_stock_policy_cache = {}
    frappe.set_user(context.user.name)
    return dish


def send_order(context, **kwargs):
    from ury.ury.doctype.ury_order.ury_order import sync_order

    payload = dict(
        items=[{"item": context.item.name, "item_name": context.item.item_name, "qty": 1, "rate": 1}],
        cashier=context.user.name, owner=context.user.name, waiter=context.user.name,
        mode_of_payment="Cash", customer=context.customer.name, no_of_pax=1,
        last_invoice=None, pos_profile=context.profile.name, order_type="Take Away",
    )
    payload.update(kwargs)
    # Frappe's real dispatch filters new optional arguments on the RED pin,
    # reproducing a retry rather than failing with an unexpected-keyword error.
    return frappe.call(sync_order, **payload)


def settle_order(context, name, **kwargs):
    from ury.ury.doctype.ury_order.ury_order import make_invoice

    payload = dict(
        customer=context.customer.name, payments=[{"mode_of_payment": "Cash", "amount": 100}],
        cashier=context.user.name, owner=context.user.name, pos_profile=context.profile.name,
        invoice=name,
    )
    payload.update(kwargs)
    return frappe.call(make_invoice, **payload)
