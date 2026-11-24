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
    limit = flt(
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
    if limit > 0:
        return limit
    group = frappe.db.get_value("Customer", customer, "customer_group")
    if isinstance(group, str) and group:
        return flt(
            frappe.db.get_value(
                "Customer Credit Limit",
                {
                    "parenttype": "Customer Group",
                    "parentfield": "credit_limits",
                    "parent": group,
                    "company": company,
                },
                "credit_limit",
            )
        )
    return limit


def _is_checkread_error(exc: Exception) -> bool:
    args = getattr(exc, "args", ())
    if args and args[0] == 1020:
        return True
    return "1020" in str(exc) and "changed since last read" in str(exc).lower()


def _committed_exposure(customer, company, exclude=""):
    """Read current committed exposure (GL Receivable + unconsolidated POS Invoices)
    on a fresh short-lived MariaDB connection so concurrent REPEATABLE READ
    transactions holding the Customer row lock see freshly committed invoices
    without hitting MariaDB 11.8 errno 1020 (ER_CHECKREAD)."""
    from frappe.database.database import Database

    if not isinstance(frappe.db, Database):
        return None
    create_conn = getattr(frappe.db, "create_connection", None)
    if not callable(create_conn):
        return None
    conn = None
    try:
        conn = create_conn()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT SUM((CASE WHEN is_return = 1 THEN
                     LEAST(0, COALESCE(NULLIF(rounded_total, 0), grand_total) - paid_amount)
                   ELSE outstanding_amount END) * conversion_rate)
                 FROM `tabPOS Invoice`
                 WHERE customer=%s AND company=%s AND docstatus=1
                   AND IFNULL(consolidated_invoice, '')='' AND status!='Consolidated' AND name!=%s""",
                (customer, company, exclude),
            )
            pending_row = cur.fetchone()
            pending_val = flt(pending_row[0]) if pending_row else 0.0

            cur.execute(
                """SELECT SUM(gle.debit - gle.credit)
                 FROM `tabGL Entry` gle
                 INNER JOIN `tabAccount` acc ON acc.name = gle.account
                 WHERE gle.party_type='Customer' AND gle.party=%s AND gle.company=%s
                   AND IFNULL(gle.is_cancelled, 0)=0 AND acc.account_type='Receivable'""",
                (customer, company),
            )
            gl_row = cur.fetchone()
            gl_val = flt(gl_row[0]) if gl_row else 0.0
            return gl_val + pending_val
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def balance(customer, company, exclude="", *, current=False):
    from erpnext.selling.doctype.customer.customer import get_customer_outstanding

    # Native POS clamps a return's outstanding to zero. Only its unrefunded
    # portion offsets AR; a cash-refunded return has no receivable effect.
    sql = """SELECT SUM((CASE WHEN is_return = 1 THEN
                 LEAST(0, COALESCE(NULLIF(rounded_total, 0), grand_total) - paid_amount)
               ELSE outstanding_amount END) * conversion_rate)
             FROM `tabPOS Invoice` WHERE customer=%s AND company=%s AND docstatus=1
               AND IFNULL(consolidated_invoice, '')='' AND status!='Consolidated' AND name!=%s"""
    try:
        pending = frappe.db.sql(
            sql + (" FOR UPDATE" if current else ""), (customer, company, exclude)
        )
        in_txn_total = get_customer_outstanding(
            customer, company, ignore_outstanding_sales_order=True
        ) + flt(pending[0][0])
    except Exception as exc:
        if not (current and _is_checkread_error(exc)):
            raise
        committed = _committed_exposure(customer, company, exclude)
        if committed is not None:
            return committed
        raise

    if current:
        committed = _committed_exposure(customer, company, exclude)
        if committed is not None:
            return max(in_txn_total, committed)
    return in_txn_total
