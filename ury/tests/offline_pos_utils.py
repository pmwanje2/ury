"""Real native documents for offline POS regression tests; no runtime hooks."""

from types import SimpleNamespace

import frappe
from frappe.utils import now_datetime


def make_offline_pos():
    frappe.set_user("Administrator")
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
    user = frappe.get_doc({
        "doctype": "User", "email": f"offline-{suffix}@example.com",
        "first_name": "Offline Test Cashier", "send_welcome_email": 0,
    }).insert()
    user.add_roles("URY Cashier")
    branch = frappe.get_doc({
        "doctype": "Branch", "branch": f"Offline Branch {suffix}",
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
    production = frappe.get_doc({
        "doctype": "URY Production Unit", "production": f"Offline Kitchen {suffix}",
        "pos_profile": profile.name, "branch": branch.name, "warehouse": warehouse,
        "item_groups": [{"item_group": group}],
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
