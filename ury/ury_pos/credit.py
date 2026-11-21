"""Trade receivables for POS settlement, in the company's currency."""

import frappe
from frappe.utils import flt


CASHIER_ROLES = {"URY Cashier", "URY Manager"}


def default_accounts(profile):
    return {
        profile.get("customer"),
        *(row.customer for row in profile.get("custom_order_type_customers") or []),
    }


def require_credit_access(profile):
    from ury.ury_pos.api import getBranch

    roles = set(frappe.get_roles())
    assigned = {row.user for row in profile.get("applicable_for_users") or []}
    billing_roles = {row.role for row in profile.get("role_allowed_for_billing") or []}
    if (
        not CASHIER_ROLES.intersection(roles)
        or not profile.allow_partial_payment
        or profile.disabled
        or not profile.branch
        or profile.branch != getBranch()
        or (assigned and frappe.session.user not in assigned)
        or (billing_roles and not billing_roles.intersection(roles))
    ):
        frappe.throw(
            "Not permitted to charge accounts on this till.", frappe.PermissionError
        )


def credit_limit(customer, company):
    return flt(
        frappe.db.get_value(
            "Customer Credit Limit",
            {
                "parenttype": "Customer",
                "parentfield": "credit_limits",
                "parent": customer,
                "company": company,
            },
            "credit_limit",
            for_update=True,
        )
    )


def balance(customer, company, exclude="", *, current=False):
    from erpnext.selling.doctype.customer.customer import get_customer_outstanding

    # Native POS clamps a return's outstanding to zero. Only its unrefunded
    # portion offsets AR; a cash-refunded return has no receivable effect.
    sql = """SELECT SUM((CASE WHEN is_return = 1 THEN
                 LEAST(0, COALESCE(NULLIF(rounded_total, 0), grand_total) - paid_amount)
               ELSE outstanding_amount END) * conversion_rate)
             FROM `tabPOS Invoice` WHERE customer=%s AND company=%s AND docstatus=1
               AND IFNULL(consolidated_invoice, '')='' AND status!='Consolidated' AND name!=%s"""
    pending = frappe.db.sql(
        sql + (" FOR UPDATE" if current else ""), (customer, company, exclude)
    )
    return get_customer_outstanding(
        customer, company, ignore_outstanding_sales_order=True
    ) + flt(pending[0][0])
