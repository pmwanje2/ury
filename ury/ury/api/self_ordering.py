# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
#
# Customer-safe ordering API (QR web / kiosk / table tablet).
#
# This module is the trust boundary between anonymous customers/devices and
# the existing staff-facing POS domain. Every whitelisted function here is
# `allow_guest=True` and must derive branch/table/profile/permission purely
# from a server-issued, verified token (`session` or a QR `token`) — never
# from client-supplied branch/table/profile values.
#
# Internally it reuses the Phase 1 domain extractions rather than
# reimplementing menu resolution, pricing, or invoice lookup:
#   - resolve_restaurant_menu()        (ury/ury_pos/api.py)
#   - price_items_for_invoice()        (ury/ury/doctype/ury_order/ury_order.py)
#   - _resolve_or_create_pos_invoice() (ury/ury/doctype/ury_order/ury_order.py)
#   - kot_execute()                    (ury/ury/api/ury_kot_generate.py)
#
# Privileged internal reads/writes (loading/saving a POS Invoice, creating a
# service request) run under a brief `frappe.set_user("Administrator")`
# elevation *after* the caller's session/table token has already been
# verified — the token check is the real authorization boundary, not the
# Frappe user session. Elevation is always narrow and wrapped in try/finally.

import base64
import calendar
import hashlib
import hmac
import json
from datetime import timedelta

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_to_date, cint, flt, get_datetime, now_datetime

from ury.ury.api.ury_kot_generate import kot_execute
from ury.ury.doctype.ury_order.ury_order import (
    _resolve_or_create_pos_invoice,
    price_items_for_invoice,
)
from ury.ury_pos.api import resolve_restaurant_menu

SESSION_TOKEN_BYTES_HASH_LEN = 64  # frappe.generate_hash(length=..)
MAX_ITEMS_PER_REQUEST = 50
MAX_COMMENT_LEN = 140  # Data fields: POS Invoice Item.comment and URY KOT Items.comments.


# ---------------------------------------------------------------------------
# Elevation helper
# ---------------------------------------------------------------------------

class _elevated:
    """Context manager: run a narrow block as Administrator, then restore
    the caller's original (Guest) user. Never widen what happens inside."""

    def __enter__(self):
        self._previous_user = frappe.session.user
        frappe.set_user("Administrator")
        return self

    def __exit__(self, exc_type, exc, tb):
        frappe.set_user(self._previous_user)
        return False


def _ensure_admin_branch_mapping(branch):
    """`getBranch()` (ury/ury_pos/api.py) — used deep inside the shared KOT
    pipeline (`create_kot_doc`) that `kot_execute()` calls — resolves branch
    from a `URY User` child-table row on `Branch` matching
    `frappe.session.user`. Under `_elevated()` that user is "Administrator",
    which has no such row by default, so KOT creation for a self-ordered
    item fails with "User is not Associated with any Branch" even though
    the invoice itself saves fine (kot_execute's caller already tolerates
    KOT failure without surfacing it to the customer, which is exactly why
    this was silent instead of loud — caught only by an actual end-to-end
    order placement, not by the mocked unit tests).

    Idempotently register Administrator against `branch` so `getBranch()`
    resolves during elevation, without touching the shared KOT/getBranch
    code itself. Administrator is used (not a new technical user) because
    kot_execute()/create_kot_doc() also perform frappe.has_permission()
    checks that a fresh no-role user would fail.
    """
    if not branch:
        return
    already_mapped = frappe.db.exists("URY User", {"parent": branch, "parenttype": "Branch", "user": "Administrator"})
    if already_mapped:
        return
    branch_doc = frappe.get_doc("Branch", branch)
    branch_doc.append("user", {"user": "Administrator"})
    branch_doc.save(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Stateless QR token (table / pickup) — signed, not stored per-table
# ---------------------------------------------------------------------------

def _get_profile_secret(profile_name):
    secret = frappe.db.get_value("URY Self Ordering Profile", profile_name, "qr_signing_secret")
    if not secret:
        frappe.throw(_("Self ordering is not configured for this restaurant"), frappe.ValidationError)
    return secret


def _sign(payload, secret):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


@frappe.whitelist()
def generate_qr_token(profile, table=None):
    """Staff-only: mint an opaque QR token for a table (or a pickup token
    when table is None) belonging to `profile`. Called from the desk / a
    future QR-management UI — never called by customers."""

    if not frappe.has_permission("URY Self Ordering Profile", "write", frappe.get_doc("URY Self Ordering Profile", profile)):
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    secret = _get_profile_secret(profile)
    payload = f"{profile}|{table or 'PICKUP'}"
    signature = _sign(payload, secret)
    raw = f"{payload}|{signature}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _verify_qr_token(token):
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        profile, table, signature = raw.rsplit("|", 2)
    except Exception:
        frappe.throw(_("Invalid ordering link"), frappe.PermissionError)

    secret = _get_profile_secret(profile)
    expected = _sign(f"{profile}|{table}", secret)
    if not hmac.compare_digest(expected, signature):
        frappe.throw(_("Invalid ordering link"), frappe.PermissionError)

    profile_doc = frappe.get_doc("URY Self Ordering Profile", profile)
    if not profile_doc.enabled:
        frappe.throw(_("Ordering is currently unavailable for this restaurant"), frappe.ValidationError)

    if table == "PICKUP":
        if not profile_doc.enable_qr_pickup_ordering:
            frappe.throw(_("Pickup ordering is not enabled"), frappe.ValidationError)
        return profile_doc, None, "QR Pickup"

    if not profile_doc.enable_qr_table_ordering:
        frappe.throw(_("Table ordering is not enabled"), frappe.ValidationError)
    if not frappe.db.exists("URY Table", table):
        frappe.throw(_("Invalid table"), frappe.ValidationError)
    return profile_doc, table, "QR Table"


# ---------------------------------------------------------------------------
# Device authentication (kiosk / table tablet)
# ---------------------------------------------------------------------------

def _hash_credential(raw_credential):
    return hashlib.sha256(raw_credential.encode()).hexdigest()


@frappe.whitelist()
def enroll_device(device):
    """Staff-only: (re)issue a device credential for a provisioned
    URY Ordering Device. The raw credential is shown once and must be
    stored securely on the device; only its hash is persisted."""

    doc = frappe.get_doc("URY Ordering Device", device)
    if not frappe.has_permission("URY Ordering Device", "write", doc):
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    raw_credential = frappe.generate_hash(length=40)
    with _elevated():
        frappe.db.set_value("URY Ordering Device", device, "credential_hash", _hash_credential(raw_credential))
    return {"device_id": doc.device_id, "credential": raw_credential}


def _resolve_device(device_id, credential):
    credential_hash = _hash_credential(credential)
    device_name = frappe.db.get_value(
        "URY Ordering Device",
        {"device_id": device_id, "credential_hash": credential_hash, "enabled": 1},
        "name",
    )
    if not device_name:
        frappe.throw(_("Device not recognized"), frappe.PermissionError)

    device = frappe.get_doc("URY Ordering Device", device_name)
    profile = frappe.get_doc("URY Self Ordering Profile", device.ordering_profile)
    if not profile.enabled or not profile.enable_kiosk_ordering:
        frappe.throw(_("Kiosk ordering is currently unavailable"), frappe.ValidationError)

    table = device.fixed_table if device.table_mode == "Fixed" else None
    source = "Table Tablet" if device.device_type == "Table Tablet" else "Kiosk"

    with _elevated():
        frappe.db.set_value("URY Ordering Device", device.name, "last_seen", now_datetime())

    return profile, table, source, device.name, device.layout


# ---------------------------------------------------------------------------
# Ordering session lifecycle
# ---------------------------------------------------------------------------

def _open_session(profile, source, table, device=None, invoice=None, granted_by=None):
    raw_session_token = frappe.generate_hash(length=SESSION_TOKEN_BYTES_HASH_LEN)
    token_hash = hashlib.sha256(raw_session_token.encode()).hexdigest()
    timeout_minutes = profile.session_idle_timeout_minutes or 30
    if invoice:
        timeout_minutes = max(1, min(cint(timeout_minutes), 30))

    with _elevated():
        session = frappe.get_doc({
            "doctype": "URY Ordering Session",
            "ordering_profile": profile.name,
            "source": source,
            "device": device,
            "table": table,
            "token_hash": token_hash,
            "status": "Active",
            "opened_at": now_datetime(),
            "expires_at": add_to_date(now_datetime(), minutes=timeout_minutes),
            "last_activity": now_datetime(),
            "invoice": invoice.name if invoice else None,
            "is_granted": int(invoice is not None),
            "grant_fingerprint": _grant_fingerprint(invoice) if invoice else None,
            "granted_by": granted_by,
        })
        session.insert(ignore_permissions=True)
        # The native successful POST boundary commits this insert. Grant
        # revocation and replacement must be one transaction, never an early commit.
        if invoice is None:
            # Preserve existing GET menu/device bootstrap durability only.
            # A staff-issued invoice grant must use the native POST transaction.
            frappe.db.commit()

    return raw_session_token, session


_GRANT_FIELDS = (
    "name", "branch", "pos_profile", "docstatus", "restaurant_table",
    "custom_merged_tables", "custom_merged_pos_invoice", "custom_split_from",
    "custom_split_group", "waiter", "customer", "is_return",
)


def _guest_refused():
    frappe.throw(_("URY_GUEST_GRANT_REQUIRED: Please ask staff for a new ordering link."), frappe.PermissionError)


def _guest_retry():
    frappe.throw(_("URY_GUEST_RETRY: Another request is finishing. Please try again."), frappe.ValidationError)


def _sorted_tables(value):
    return sorted({table.strip() for table in (value or "").split(",") if table.strip()})


def _valid_fingerprint(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _requires_guest_invoice_grant(profile):
    """Opt into exact-invoice grants without changing existing guest ordering."""
    return getattr(profile, "require_guest_invoice_grant", 0) in (True, 1, "1")


def _is_staff_grant(session):
    return bool(getattr(session, "invoice", None)) and bool(getattr(session, "is_granted", 0)) \
        and getattr(session, "granted_by", None) not in (None, "Guest")


def _grant_row(invoice):
    row = {field: invoice.get(field) or None for field in _GRANT_FIELDS}
    row["custom_merged_tables"] = _sorted_tables(invoice.get("custom_merged_tables"))
    return row


def _grant_fingerprint(invoice):
    """Actual lifecycle values, including all split members, never modified/items/print.

    Native current locking reads also see direct merge updates. Group rows are
    private fingerprint material and are never returned to a guest.
    """
    members = {}
    filters = [{"custom_split_from": invoice.name}, {"custom_merged_pos_invoice": invoice.name}]
    group = invoice.get("custom_split_group")
    if not group and invoice.get("custom_split_from"):
        group = frappe.db.get_value("POS Invoice", invoice.custom_split_from, "custom_split_group", for_update=True)
    if group:
        filters.append({"custom_split_group": group})
    for scope in filters:
        for row in frappe.db.get_values(
            "POS Invoice", {**scope, "docstatus": ["<", 2]}, list(_GRANT_FIELDS),
            as_dict=True, for_update=True, order_by="name",
        ):
            members[row.name] = _grant_row(row)
    table = None
    if invoice.restaurant_table:
        table = frappe.db.get_value(
            "URY Table", invoice.restaurant_table, ["branch", "restaurant_room", "merged_with"],
            as_dict=True, for_update=True,
        )
        if not table or table.branch != invoice.branch:
            _guest_refused()
        table["merged_with"] = _sorted_tables(table.merged_with)
    snapshot = [_grant_row(invoice), [members[name] for name in sorted(members)], table]
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _checked_guest_session(session_name):
    try:
        with _elevated():
            session = frappe.get_doc("URY Ordering Session", session_name)
            profile = frappe.get_doc("URY Self Ordering Profile", session.ordering_profile)
            if not profile.enabled or profile.name != session.ordering_profile:
                _guest_refused()
            requires_grant = _requires_guest_invoice_grant(profile)
            # Invoice first, then session: issuers and guest mutations use the same
            # native lock order. No alternative invoice is ever looked up. The
            # legacy path intentionally does not lock or fingerprint an invoice.
            invoice = frappe.get_doc("POS Invoice", session.invoice, for_update=True) \
                if requires_grant and session.invoice else None
            session = frappe.get_doc("URY Ordering Session", session_name, for_update=True)
            if session.status != "Active" or not session.expires_at or get_datetime(session.expires_at) <= now_datetime():
                _guest_refused()
            if requires_grant and session.invoice:
                if (not invoice or invoice.name != session.invoice or not cint(session.get("is_granted"))
                        or cint(invoice.docstatus) != 0 or cint(invoice.get("is_return"))
                        or invoice.branch != profile.branch or invoice.pos_profile != profile.pos_profile
                        or invoice.restaurant_table != session.table
                        or not _valid_fingerprint(session.get("grant_fingerprint"))
                        or not hmac.compare_digest(session.get("grant_fingerprint") or "", _grant_fingerprint(invoice))):
                    _guest_refused()
            # Flag OFF: every session slides exactly as before G1. Flag ON:
            # only pickup/kiosk slide; table and staff-grant expiry is fixed.
            if not requires_grant or not (session.table or _is_staff_grant(session)):
                session.last_activity = now_datetime()
                session.expires_at = add_to_date(
                    now_datetime(), minutes=profile.session_idle_timeout_minutes or 30,
                )
                session.save(ignore_permissions=True)
            return session
    except frappe.DoesNotExistError:
        _guest_refused()
    except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
        _guest_retry()


def _resolve_session(session_token):
    if not isinstance(session_token, str) or not session_token or len(session_token) > 512:
        _guest_refused()
    session_name = frappe.db.get_value(
        "URY Ordering Session", {"token_hash": _hash_credential(session_token), "status": "Active"}, "name",
    )
    if not session_name:
        _guest_refused()
    return _checked_guest_session(session_name)


@frappe.whitelist(methods=["POST"])
def issue_table_grant(invoice):
    """Staff: replace this exact draft invoice's grant; native authority stays ERP-side."""
    if frappe.session.user == "Guest":
        _guest_refused()
    from ury.ury.doctype.ury_order.ury_order import _enforce_order_access
    from ury.ury_pos.api import getBranch

    try:
        invoice_doc = frappe.get_doc("POS Invoice", invoice, for_update=True)
        invoice_doc.check_permission("write")
        if invoice_doc.branch != getBranch():
            _guest_refused()
        _enforce_order_access(invoice_doc, require_modify=True)
        if not invoice_doc.restaurant_table or cint(invoice_doc.docstatus) != 0 or cint(invoice_doc.get("is_return")):
            _guest_refused()
        profiles = frappe.get_all(
            "URY Self Ordering Profile",
            filters={"branch": invoice_doc.branch, "pos_profile": invoice_doc.pos_profile,
                     "enabled": 1, "enable_qr_table_ordering": 1}, pluck="name",
        )
        if len(profiles) != 1:
            _guest_refused()
        profile = frappe.get_doc("URY Self Ordering Profile", profiles[0])
        # A grant on a flag-OFF profile would never be fingerprint-rechecked.
        if not profile.enabled or not profile.enable_qr_table_ordering or not _requires_guest_invoice_grant(profile):
            _guest_refused()
        _grant_fingerprint(invoice_doc)
        issuer = frappe.session.user
        previous = frappe.db.get_values(
            "URY Ordering Session", {"invoice": invoice_doc.name, "status": "Active"},
            ["name"], as_dict=True, for_update=True, order_by="name",
        )
        for row in previous:
            frappe.db.set_value("URY Ordering Session", row.name, "status", "Closed")
        raw, session = _open_session(profile, "QR Table", invoice_doc.restaurant_table,
                                     invoice=invoice_doc, granted_by=issuer)
        return {"session": raw, "expires_at": session.expires_at}
    except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
        _guest_retry()


def _require_table_grant(session):
    if session.table and (not session.invoice or not cint(session.get("is_granted"))):
        _guest_refused()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def _ordering_context_response(raw_session_token, source, profile, table, layout):
    """Shared response shape for every bootstrap-style entry point
    (get_ordering_context, assign_device_table) — never raw branch/table
    names the client didn't already have a right to see."""
    return {
        "session": raw_session_token,
        "source": source,
        "restaurant": profile.restaurant,
        "table": table,
        # "Mobile" for QR sessions (no device involved); otherwise the
        # provisioned URY Ordering Device's configured layout (Tablet /
        # Landscape Kiosk / Portrait Kiosk) — the frontend layout shell
        # selector uses this, never a client-guessed value.
        "layout": layout,
        "capabilities": {
            "product_detail_enabled": bool(profile.enable_product_detail_page),
            "show_item_images": bool(profile.show_item_images),
            "show_item_descriptions": bool(profile.show_item_descriptions),
            "item_notes_enabled": bool(profile.enable_item_notes),
            "request_bill_enabled": bool(profile.enable_request_bill) and bool(table),
            "customer_payment_enabled": bool(profile.enable_customer_payment),
            "payment_link_enabled": bool(profile.enable_payment_link),
            "pay_at_counter_enabled": bool(profile.enable_pay_at_counter),
            "add_to_running_table_enabled": bool(profile.allow_add_to_running_table),
        },
    }


@frappe.whitelist(allow_guest=True)
def get_ordering_context(token=None, device_id=None, device_credential=None):
    """Entry point for every customer-facing surface. Resolves a QR token
    or a device credential into a fresh ordering session and returns only
    customer-safe channel configuration — never raw branch/table names the
    client didn't already have a right to see."""

    layout = "Mobile"
    if token:
        profile, table, source = _verify_qr_token(token)
        device_name = None
    elif device_id and device_credential:
        profile, table, source, device_name, layout = _resolve_device(device_id, device_credential)
    else:
        frappe.throw(_("Missing ordering token or device credentials"), frappe.PermissionError)

    raw_session_token, session = _open_session(profile, source, table, device_name)

    return _ordering_context_response(raw_session_token, source, profile, table, layout)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def get_granted_ordering_context(session):
    """Return the context for an existing staff-issued invoice grant.

    Unlike ``get_ordering_context`` this endpoint never opens a session. The
    opaque value is already the raw token returned by ``issue_table_grant``;
    it is accepted once for resolution and is returned unchanged so the
    frontend can persist the same session context after consuming ``#g=``.
    """
    resolved = _resolve_session(session)
    _require_table_grant(resolved)
    profile = frappe.get_doc("URY Self Ordering Profile", resolved.ordering_profile)
    if (not _requires_guest_invoice_grant(profile) or resolved.source != "QR Table"
            or not resolved.table or not _is_staff_grant(resolved)):
        _guest_refused()
    return _ordering_context_response(session, resolved.source, profile, resolved.table, "Mobile")


@frappe.whitelist(allow_guest=True)
def assign_device_table(device_id, device_credential, staff_pin, table):
    """Bind a shared/portable tablet (`table_mode = "Selectable"`) to a
    table for the duration of a session. Used by `PortableTabletAssignment`
    when staff hand the tablet to a table — the tablet has no fixed table
    of its own, so a staff member authorizes the binding with a shared PIN
    rather than a full desk login on a customer-facing device.

    MVP note: `staff_pin` is one shared PIN per ordering profile (see
    `URY Self Ordering Profile.staff_pin`), not per-staff-member. A natural
    next iteration would link PINs to individual `Employee` records for
    per-staff attribution/audit — out of scope for this pass.
    """
    profile, existing_table, source, device_name, layout = _resolve_device(device_id, device_credential)

    table_mode = frappe.db.get_value("URY Ordering Device", device_name, "table_mode")
    if table_mode != "Selectable":
        frappe.throw(_("This device is not configured for table selection"), frappe.ValidationError)

    configured_pin = frappe.db.get_value("URY Self Ordering Profile", profile.name, "staff_pin")
    # Shared-secret equality check, not a password hash — acceptable for
    # this MVP (see class/module docstring conventions elsewhere in this
    # file for the same "don't over-engineer" bar). hmac.compare_digest
    # avoids trivial short-circuit timing leaks at negligible cost.
    if not configured_pin or not hmac.compare_digest(str(configured_pin), str(staff_pin or "")):
        frappe.throw(_("Incorrect staff PIN"), frappe.PermissionError)

    if not frappe.db.exists("URY Table", table):
        frappe.throw(_("Invalid table"), frappe.ValidationError)
    table_branch = frappe.db.get_value("URY Table", table, "branch")
    if table_branch != profile.branch:
        frappe.throw(_("Invalid table"), frappe.ValidationError)

    raw_session_token, session = _open_session(profile, source, table, device_name)

    return _ordering_context_response(raw_session_token, source, profile, table, layout)


# ---------------------------------------------------------------------------
# Menu / product (customer-safe DTOs)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST"])
def get_customer_menu(session):
    session = _resolve_session(session)
    order_type = "Dine In" if session.table else "Take Away"

    with _elevated():
        menu = resolve_restaurant_menu(
            branch=frappe.db.get_value("URY Self Ordering Profile", session.ordering_profile, "branch"),
            room=None,
            order_type=order_type,
            cashier=False,
        )

    # resolve_restaurant_menu() already returns a sanitized item list
    # (item/item_name/rate/course/image) — no raw Item doc fields leak here.
    return menu


@frappe.whitelist(allow_guest=True, methods=["POST"])
def get_customer_product(session, item_code):
    session = _resolve_session(session)
    profile = frappe.get_doc("URY Self Ordering Profile", session.ordering_profile)

    with _elevated():
        item = frappe.db.get_value(
            "Item", item_code,
            ["item_code", "item_name", "description", "image"],
            as_dict=True,
        )
        if not item:
            frappe.throw(_("Item not found"), frappe.DoesNotExistError)

        # Both `POS Item Variants` and `Item Add On` are child-table doctypes
        # whose only field is a Link to another Item (the variant/add-on is
        # itself a priced Item) — see ury/ury/doctype/{pos_item_variants,
        # item_add_on}/*.json. Resolve each linked item's own price rather
        # than trusting any client-supplied amount.
        variants = _linked_item_options("POS Item Variants", item_code, invoice_price_list=None)
        addons = _linked_item_options("Item Add On", item_code, invoice_price_list=None)

    return {
        "item_code": item.item_code,
        "item_name": item.item_name,
        "description": item.description if profile.show_item_descriptions else None,
        "image": item.image if profile.show_item_images else None,
        "variants": variants,
        "addons": addons,
    }


def _linked_item_options(child_doctype, parent_item_code, invoice_price_list):
    if not frappe.db.exists("DocType", child_doctype):
        return []

    rows = frappe.get_all(
        child_doctype,
        filters={"parenttype": "Item", "parent": parent_item_code},
        fields=["item"],
    )

    options = []
    for row in rows:
        linked = frappe.db.get_value("Item", row.item, ["item_code", "item_name", "image"], as_dict=True)
        if not linked:
            continue
        price = None
        if invoice_price_list:
            price = frappe.db.get_value(
                "Item Price", {"item_code": linked.item_code, "price_list": invoice_price_list}, "price_list_rate"
            )
        options.append({
            "item_code": linked.item_code,
            "item_name": linked.item_name,
            "image": linked.image,
            "rate": price,
        })
    return options


# ---------------------------------------------------------------------------
# Current order / append-only mutation
# ---------------------------------------------------------------------------

def _sanitize_invoice_for_customer(invoice):
    return {
        "invoice": invoice.name,
        "table": invoice.restaurant_table,
        "order_type": invoice.order_type,
        # No table-specific bill to hand a pickup customer, so the invoice
        # name itself doubles as their pickup reference — reusing the
        # existing invoice identity rather than minting a new field/value.
        "pickup_code": invoice.name,
        "items": [
            {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "qty": row.qty,
                # The native line comment has no guest provenance: it may
                # contain internal kitchen/staff instructions. Keep it on the
                # invoice/KOT, never in guest reads or append responses.
                "comment": None,
                "rate": row.rate,
                "amount": row.amount,
            }
            for row in invoice.items
        ],
        "grand_total": invoice.grand_total,
        "billed": bool(invoice.invoice_printed),
    }


@frappe.whitelist(allow_guest=True, methods=["POST"])
def get_customer_order(session):
    session = _resolve_session(session)
    if not session.invoice:
        return {"invoice": None, "pickup_code": None, "items": [], "grand_total": 0, "billed": False}

    with _elevated():
        invoice = frappe.get_doc("POS Invoice", session.invoice)

    return _sanitize_invoice_for_customer(invoice)


@frappe.whitelist(allow_guest=True)
def add_customer_items(session, items, request_id=None):
    """Append-only customer order mutation. `items` is a list of
    {"item": <item_code>, "qty": <number>, "comment": <optional str>} —
    same shape sync_order()/price_items_for_invoice() already expect.

    Never trusts client-supplied price/tax/discount/warehouse/cost-center —
    price_items_for_invoice() re-derives price server-side from Item Price,
    exactly as it does for staff orders.
    """
    session = _resolve_session(session)
    profile = frappe.get_doc("URY Self Ordering Profile", session.ordering_profile)
    requires_grant = _requires_guest_invoice_grant(profile)
    if requires_grant:
        _require_table_grant(session)

    if not profile.enabled:
        frappe.throw(_("Ordering is currently unavailable"), frappe.ValidationError)
    if session.invoice and not profile.allow_add_to_running_table:
        frappe.throw(_("Adding further items to this order is not allowed"), frappe.ValidationError)

    # Preserve the existing table-first ordering guard while the exact-invoice
    # grant requirement remains disabled. A fresh legacy table session must not
    # join a staff or earlier guest bill when running-table additions are off.
    if not requires_grant and session.table and not session.invoice and not profile.allow_add_to_running_table:
        with _elevated():
            existing_invoice = frappe.get_all(
                "POS Invoice",
                filters={"docstatus": 0, "invoice_printed": 0},
                or_filters={
                    "restaurant_table": session.table,
                    "custom_merged_tables": ["like", f"%{session.table}%"],
                },
                limit=1,
            )
        if existing_invoice:
            frappe.throw(
                _("This table already has an order in progress. Please ask staff for assistance."),
                frappe.ValidationError,
            )

    if isinstance(items, str):
        items = json.loads(items)
    if not items or not isinstance(items, list):
        frappe.throw(_("No items provided"), frappe.ValidationError)
    if len(items) > MAX_ITEMS_PER_REQUEST:
        frappe.throw(_("Too many items in a single request"), frappe.ValidationError)

    with _elevated():
        menu_names = {
            m.get("item")
            for m in resolve_restaurant_menu(
                branch=profile.branch,
                room=None,
                order_type="Dine In" if session.table else "Take Away",
                cashier=False,
            )["items"]
        }

    clean_items = []
    for raw in items:
        item_code = raw.get("item")
        qty = raw.get("qty")
        comment = (raw.get("comment") or "")[:MAX_COMMENT_LEN]

        if not item_code or item_code not in menu_names:
            frappe.throw(_("Item {0} is not available on this menu").format(item_code), frappe.ValidationError)
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            frappe.throw(_("Invalid quantity for item {0}").format(item_code), frappe.ValidationError)

        clean_items.append({"item": item_code, "item_name": item_code, "qty": qty, "comment": comment})

    cache_key = payload_hash = None
    if request_id is not None:
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            frappe.throw(_("Invalid request ID"), frappe.ValidationError)
        cache_key = "ury:guest:round:" + _hash_credential(f"{session.name}:{request_id}")
        payload_hash = _hash_credential(json.dumps(clean_items, sort_keys=True, separators=(",", ":")))
        previous = frappe.cache.get_value(cache_key, use_local_cache=False)
        if previous:
            if previous["payload_hash"] != payload_hash:
                frappe.throw(_("Request ID already used for different items"), frappe.ValidationError)
            if "response" not in previous:
                _guest_retry()
            return previous["response"]

    order_type = "Dine In" if session.table else "Take Away"

    with _elevated():
        _ensure_admin_branch_mapping(profile.branch)

        if session.invoice and requires_grant:
            # Never use the native staff discovery/create helper for a grant.
            # The exact native row is already locked and authorized by resolution.
            invoice = frappe.get_doc("POS Invoice", session.invoice, for_update=True)
        else:
            invoice, _invoice_name = _resolve_or_create_pos_invoice(
                table=session.table, invoiceNo=session.invoice, order_type=order_type, is_payment=None,
                check_permission=False, override_branch=profile.branch,
            )

        # _resolve_or_create_pos_invoice() never sets restaurant_table on a
        # brand-new invoice — sync_order() does that itself after the call
        # (its own caller-side responsibility, same pattern here). Missing
        # this meant the SECOND add_customer_items() call for the same
        # table couldn't find the first call's invoice by name at all:
        # _resolve_or_create_pos_invoice's table-path query AND-combines an
        # exact `name` match with an OR-group requiring restaurant_table (or
        # custom_merged_tables) to equal the table — with restaurant_table
        # still null, that OR-group is false and the whole query returns
        # nothing, so a second invoice got created instead of the running
        # order being updated. Confirmed live: two separate invoices for
        # the same table/session instead of one.
        if session.table and not invoice.restaurant_table:
            invoice.restaurant_table = session.table

        # Same gap for order_type on the pickup (no-table) path:
        # _resolve_or_create_pos_invoice() only derives order_type from the
        # table's is_take_away flag in the table branch — the non-table
        # branch leaves it untouched, expecting the caller to set it
        # (sync_order() does `if order_type: invoice.order_type =
        # order_type` itself). Confirmed live: a pickup order's invoice had
        # an empty order_type instead of "Take Away" before this.
        if not session.table:
            invoice.order_type = order_type

        if not invoice.customer:
            if not profile.default_customer:
                frappe.throw(_("Self ordering profile has no default customer configured"), frappe.ValidationError)
            invoice.customer = profile.default_customer

        invoice.pos_profile = profile.pos_profile
        invoice.custom_order_source = session.source
        invoice.custom_ordering_session = session.name
        if session.device:
            invoice.custom_ordering_device = session.device

        # Aggregate both snapshots by item AND note. Guest rounds append
        # rows, so the diff needs one total per variant on each side.
        past_qty_by_item = {}
        past_name_by_item = {}
        for row in invoice.items:
            key = (row.item_code, row.comment or "")
            past_qty_by_item[key] = past_qty_by_item.get(key, 0) + row.qty
            past_name_by_item[key] = row.item_name
        past_item = [
            {"item_code": code, "item_name": past_name_by_item[(code, note)],
             "qty": past_qty_by_item[(code, note)], "comments": note}
            for code, note in past_qty_by_item
        ]

        menu = frappe.db.get_value("URY Menu", {"branch": invoice.branch}, "name")
        priced_items = price_items_for_invoice(
            clean_items, invoice.selling_price_list, profile.pos_profile, invoice.branch, menu,
        )
        for item_dict in priced_items:
            invoice.append("items", item_dict)

        if invoice.invoice_created == 0:
            posprofile = frappe.get_doc("POS Profile", profile.pos_profile)
            default_mode = posprofile.payments[0].mode_of_payment if posprofile.payments else None
            if not default_mode:
                frappe.throw(_("POS Profile has no mode of payment configured"), frappe.ValidationError)
            invoice.append("payments", dict(mode_of_payment=default_mode, amount=invoice.grand_total))
            invoice.invoice_created = 1

        try:
            if cache_key:
                # This is an idempotency record, not a lock. The native invoice
                # (or new pickup's Session) is locked first. Keep a fail-closed
                # marker across the SQL COMMIT / after_commit callback window.
                cache_ttl = max(1800, int((get_datetime(session.expires_at) - now_datetime()).total_seconds()) + 60)
                frappe.cache.set_value(cache_key, {"payload_hash": payload_hash}, expires_in_sec=cache_ttl)
                frappe.db.after_rollback.add(lambda: frappe.cache.delete_value(cache_key))
            invoice.save(ignore_permissions=True)
        except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
            _guest_retry()
        except Exception as e:
            frappe.throw(_("Error while placing order: {0}").format(e))

        if not session.invoice:
            session.invoice = invoice.name
            if requires_grant:
                session.is_granted = 1
                session.grant_fingerprint = _grant_fingerprint(invoice)
                session.granted_by = "Guest"
            session.save(ignore_permissions=True)

        if invoice.invoice_printed == 0 and invoice.restaurant_table:
            frappe.db.set_value(
                "URY Table", invoice.restaurant_table,
                {"occupied": 1, "latest_invoice_time": invoice.creation},
            )

        try:
            # Pass the complete post-append state, preserving each note;
            # passing only the new rows would cancel existing dishes.
            qty_by_item = {}
            name_by_item = {}
            for row in invoice.items:
                key = (row.item_code, row.comment or "")
                qty_by_item[key] = qty_by_item.get(key, 0) + row.qty
                name_by_item[key] = row.item_name
            current_items_for_kot = [
                {"item_code": code, "item_name": name_by_item[(code, note)],
                 "qty": qty_by_item[(code, note)], "comments": note}
                for code, note in qty_by_item
            ]
            kot_execute(invoice.name, invoice.customer, invoice.restaurant_table, current_items_for_kot, past_item, None)
        except Exception as e:
            frappe.log_error(f"Self-order KOT creation failed: {e}", "KOT Error")

    response = _sanitize_invoice_for_customer(invoice)
    if cache_key:
        # Failed/rolled-back writes must never poison replay. The exact invoice
        # lock serializes requests through the native request commit boundary.
        frappe.db.after_commit.add(lambda: frappe.cache.set_value(
            cache_key, {"payload_hash": payload_hash, "response": response}, expires_in_sec=cache_ttl,
        ))
    return response


# ---------------------------------------------------------------------------
# Request bill
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def request_bill(session):
    session = _resolve_session(session)
    profile = frappe.get_doc("URY Self Ordering Profile", session.ordering_profile)

    if not profile.enable_request_bill:
        frappe.throw(_("Requesting the bill is not enabled"), frappe.ValidationError)
    if not session.table or not session.invoice:
        frappe.throw(_("No active order for this table"), frappe.ValidationError)

    with _elevated():
        existing = frappe.db.exists(
            "URY Service Request",
            {"table": session.table, "invoice": session.invoice, "request_type": "Bill", "status": ["!=", "Resolved"]},
        )
        if existing:
            return {"status": "Already Requested", "request": existing}

        req = frappe.get_doc({
            "doctype": "URY Service Request",
            "request_type": "Bill",
            "table": session.table,
            "invoice": session.invoice,
            "session": session.name,
            "status": "Open",
            "requested_at": now_datetime(),
        })
        req.insert(ignore_permissions=True)

    return {"status": "Requested", "request": req.name}


@frappe.whitelist(allow_guest=True)
def request_assistance(session):
    session = _resolve_session(session)
    profile = frappe.get_doc("URY Self Ordering Profile", session.ordering_profile)

    if not profile.enable_request_bill:
        frappe.throw(_("Requesting staff assistance is not enabled"), frappe.ValidationError)
    if not session.table or not session.invoice:
        frappe.throw(_("No active order for this table"), frappe.ValidationError)

    with _elevated():
        existing = frappe.db.exists(
            "URY Service Request",
            {"table": session.table, "invoice": session.invoice, "request_type": "Assistance", "status": ["!=", "Resolved"]},
        )
        if existing:
            return {"status": "Already Requested", "request": existing}

        req = frappe.get_doc({
            "doctype": "URY Service Request",
            "request_type": "Assistance",
            "table": session.table,
            "invoice": session.invoice,
            "session": session.name,
            "status": "Open",
            "requested_at": now_datetime(),
        })
        req.insert(ignore_permissions=True)

    return {"status": "Requested", "request": req.name}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST"])
def get_order_status(session):
    session = _resolve_session(session)
    result = {"session_status": session.status, "invoice": session.invoice}

    if session.invoice:
        with _elevated():
            invoice_fields = frappe.db.get_value(
                "POS Invoice", session.invoice, ["docstatus", "invoice_printed"], as_dict=True,
            )
            open_requests = frappe.get_all(
                "URY Service Request",
                filters={"invoice": session.invoice, "status": ["!=", "Resolved"]},
                fields=["name", "request_type", "status"],
            )
        result["billed"] = bool(invoice_fields and invoice_fields.invoice_printed)
        result["submitted"] = bool(invoice_fields and invoice_fields.docstatus == 1)
        result["open_requests"] = open_requests

    return result


# ---------------------------------------------------------------------------
# Payment (Phase 6)
#
# Reuses ERPNext's own Payment Request doctype/flow rather than building a
# parallel payments implementation — "POS Invoice" is already in its
# ALLOWED_DOCTYPES_FOR_PAYMENT_REQUEST list. A gateway-redirect checkout and
# a shareable/QR "payment link" are the SAME Payment Request underneath;
# they only differ in how the frontend presents payment_url (auto-redirect
# vs. copyable/QR link) — no need for two backend code paths.
#
# The gateway verifies payment; the Phone request becoming Paid does not
# submit the POS Invoice. The cashier settles through URY make_invoice,
# selecting the same gateway mode. No provider webhook logic belongs here.
# ---------------------------------------------------------------------------

def _payment_url(pr):
    try:
        return pr.get_payment_url()
    except Exception as e:
        # The Payment Request is real; only the gateway link is unavailable.
        frappe.log_error(f"Payment Request has no payment URL available: {e}", "Self-Order Payment")
        return None


@frappe.whitelist(allow_guest=True)
def create_payment_request(session):
    session = _resolve_session(session)
    profile = frappe.get_doc("URY Self Ordering Profile", session.ordering_profile)

    if not (profile.enable_customer_payment or profile.enable_payment_link):
        frappe.throw(_("Online payment is not enabled"), frappe.ValidationError)
    if not session.invoice:
        frappe.throw(_("No active order for this session"), frappe.ValidationError)

    with _elevated():
        from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

        invoice = frappe.get_doc("POS Invoice", session.invoice)
        if invoice.docstatus == 1:
            frappe.throw(_("This order is already settled"), frappe.ValidationError)

        # POS Invoice payment requests use the Phone payment row's account as
        # the amount source in ERPNext v16. Use the gateway account of that
        # row's own account; a company default gateway only breaks ties.
        pos_profile = frappe.get_cached_doc("POS Profile", profile.pos_profile)
        gateway_account = mode_of_payment = payment_account = None
        for profile_payment in pos_profile.payments:
            row_mode = profile_payment.mode_of_payment
            if frappe.db.get_value("Mode of Payment", row_mode, "type") != "Phone":
                continue

            row_account = frappe.db.get_value(
                "Mode of Payment Account",
                {"parent": row_mode, "company": invoice.company},
                "default_account",
            )
            row_gateway = row_account and frappe.db.get_value(
                "Payment Gateway Account",
                {"company": invoice.company, "payment_account": row_account, "payment_channel": "Phone"},
                ["name", "payment_account", "is_default"],
                as_dict=True,
                order_by="is_default desc",
            )
            if row_gateway and (not gateway_account or (row_gateway.is_default and not gateway_account.is_default)):
                gateway_account, mode_of_payment, payment_account = row_gateway, row_mode, row_account

        if not mode_of_payment:
            frappe.throw(
                _(
                    "[URY-PAYMENT-NOT-CONFIGURED] Configure a Phone payment method "
                    "with a Payment Gateway Account for this POS Profile."
                ),
                frappe.ValidationError,
            )

        # ERPNext populates outstanding_amount only when submitting POS
        # invoices; an unpaid draft normally still has zero in that field.
        outstanding_amount = (
            invoice.get("outstanding_amount")
            or invoice.get("rounded_total")
            or invoice.get("grand_total")
        )

        # ERPNext validates each Phone row against ONE Paid request, not a
        # sum of requests. After phone payment, any growth is paid at the
        # counter; changing the default gateway must not open another link.
        request_filters = {
            "reference_doctype": "POS Invoice",
            "reference_name": invoice.name,
            "docstatus": 1,
        }
        paid_requests = frappe.get_all(
            "Payment Request",
            filters={**request_filters, "payment_channel": "Phone", "payment_request_type": "Inward",
                "company": invoice.company, "currency": invoice.currency,
                "status": ["in", ["Paid", "Partially Paid"]]},
            fields=["name", "currency", "grand_total", "outstanding_amount"],
            order_by="creation desc",
        )
        paid_amount = flt(sum(flt(p.grand_total) - flt(p.outstanding_amount) for p in paid_requests))
        remaining_amount = flt(flt(outstanding_amount) - paid_amount, invoice.precision("grand_total"))
        if paid_requests:
            return {
                "payment_request": paid_requests[0].name,
                "amount": paid_amount,
                "currency": paid_requests[0].currency,
                "payment_url": None,
                "status": "Paid",
                "paid_amount": paid_amount,
                "remaining_amount": max(0, remaining_amount),
            }

        # Re-tap: return the request already open for this exact amount.
        existing = frappe.db.get_value(
            "Payment Request",
            {
                **request_filters,
                "payment_gateway_account": gateway_account.name,
                "grand_total": remaining_amount,
                "status": ["not in", ["Paid", "Partially Paid", "Cancelled", "Failed"]],
            },
            "name",
            order_by="creation desc",
        )
        if existing:
            pr = frappe.get_doc("Payment Request", existing)
            return _payment_request_response(pr, _payment_url(pr), paid_amount)

        # A stale unpaid guest link is replaced only if its checkout never
        # started (no Integration Request). Any checkout, even one marked
        # Failed, may still be paid at the provider, so keep its link; the
        # gateway app cancels requests that truly failed or expired, and the
        # next tap then creates a new one. Requests made elsewhere (e.g. a
        # cashier) are never cancelled here.
        busy = _("A payment for this order is already in progress. Please ask staff for help.")
        open_requests = frappe.get_all(
            "Payment Request",
            filters={**request_filters, "status": ["not in", ["Paid", "Partially Paid", "Cancelled", "Failed"]]},
            fields=["name", "owner", "payment_gateway_account", "payment_channel", "payment_request_type"],
            order_by="creation desc",
        )
        for open_request in open_requests:
            # The guest path creates requests while elevated to Administrator.
            if (
                open_request.owner != "Administrator"
                or open_request.payment_gateway_account != gateway_account.name
                or open_request.payment_channel != "Phone"
                or open_request.payment_request_type != "Inward"
            ):
                frappe.throw(busy, frappe.ValidationError)
            checkout = set(frappe.get_all(
                "Integration Request",
                filters={"reference_doctype": "Payment Request", "reference_docname": open_request.name},
                pluck="status",
            ))
            if checkout & {"Authorized", "Completed"}:
                frappe.throw(busy, frappe.ValidationError)
            if checkout:
                pr = frappe.get_doc("Payment Request", open_request.name)
                return _payment_request_response(pr, _payment_url(pr), paid_amount)
        for open_request in open_requests:
            pr = frappe.get_doc("Payment Request", open_request.name)
            pr.flags.ignore_permissions = True
            pr.cancel()

        phone_payment = next(
            (payment for payment in invoice.payments if payment.mode_of_payment == mode_of_payment),
            None,
        )
        if not phone_payment:
            phone_payment = invoice.append("payments", {})
        phone_payment.mode_of_payment = mode_of_payment
        phone_payment.account = payment_account
        phone_payment.type = "Phone"
        phone_payment.amount = outstanding_amount
        invoice.save(ignore_permissions=True)

        # make_payment_request() derives its own amount from the reference
        # doc server-side (get_amount()) — the client never supplies one.
        # Configuration is refused above; any error here is real and is not
        # relabelled as missing configuration.
        pr = make_payment_request(
            dt="POS Invoice",
            dn=invoice.name,
            submit_doc=1,
            mute_email=1,
            payment_gateway_account=gateway_account.name,
            mode_of_payment=mode_of_payment,
            return_doc=1,
        )
        payment_url = _payment_url(pr)

    return _payment_request_response(pr, payment_url, paid_amount)


def _payment_request_response(pr, payment_url, paid_amount):
    return {
        "payment_request": pr.name,
        "amount": pr.grand_total,
        "currency": pr.currency,
        "payment_url": payment_url,
        "status": pr.status,
        "paid_amount": paid_amount,
        "remaining_amount": pr.grand_total,
    }


@frappe.whitelist(allow_guest=True, methods=["POST"])
def get_payment_status(session):
    session = _resolve_session(session)
    if not session.invoice:
        return {"status": None}

    with _elevated():
        pr = frappe.db.get_value(
            "Payment Request",
            {"reference_doctype": "POS Invoice", "reference_name": session.invoice},
            ["name", "status", "grand_total"],
            as_dict=True,
            order_by="creation desc",
        )
        payment_url = (
            _payment_url(frappe.get_doc("Payment Request", pr.name))
            if pr and pr.status not in ("Paid", "Partially Paid", "Cancelled", "Failed")
            else None
        )

    if not pr:
        return {"status": None}
    return {
        "payment_request": pr.name,
        "status": pr.status,
        "amount": pr.grand_total,
        "payment_url": payment_url,
    }


# ---------------------------------------------------------------------------
# Communication provider (Phase 6.5) — extensible, not hardcoded to any one
# channel. No messaging integration (WhatsApp/SMS/email) is installed in
# this app today, so the default provider only logs; a future installed
# app/plugin can register a real one instead of this module hardcoding it.
# ---------------------------------------------------------------------------

def _default_communication_provider(recipient, message):
    frappe.log_error(f"[self-ordering communication stub] to={recipient}: {message}", "Self-Order Notify")


_communication_provider = _default_communication_provider


def register_communication_provider(fn):
    """Install a real communication provider: fn(recipient: str, message: str) -> None.
    Call this from a future installed app's hooks.py (e.g. a WhatsApp
    integration) rather than editing this module directly."""
    global _communication_provider
    _communication_provider = fn


@frappe.whitelist(allow_guest=True)
def share_payment_link(session, recipient):
    """Send the current Payment Request's link to `recipient` (whatever
    format the active communication provider expects — a phone number for
    an SMS/WhatsApp provider, an email address for an email provider) via
    the registered provider. The default provider only logs — this is a
    genuine extension point (register_communication_provider), not a
    real send, since no messaging integration is installed in this app.
    `recipient` is customer-supplied input, not derived from the session,
    so validate it's at least non-empty before handing it to the provider.
    """
    session = _resolve_session(session)
    if not session.invoice:
        _guest_refused()
    profile = frappe.get_doc("URY Self Ordering Profile", session.ordering_profile)
    if not profile.enable_payment_link:
        frappe.throw(_("Payment links are not enabled"), frappe.ValidationError)
    if not recipient or not isinstance(recipient, str):
        frappe.throw(_("A valid recipient is required"), frappe.ValidationError)

    with _elevated():
        pr = frappe.db.get_value(
            "Payment Request",
            {"reference_doctype": "POS Invoice", "reference_name": session.invoice},
            ["name", "grand_total"],
            as_dict=True,
            order_by="creation desc",
        )
        if not pr:
            frappe.throw(_("No payment request found for this order — create one first"), frappe.ValidationError)

        pr_doc = frappe.get_doc("Payment Request", pr.name)
        try:
            payment_url = pr_doc.get_payment_url()
        except Exception:
            payment_url = None

    message = _(
        "Your order total is {0}. {1}"
    ).format(pr.grand_total, payment_url or _("A staff member will assist with payment shortly."))
    _communication_provider(recipient, message)

    return {"status": "Sent", "payment_request": pr.name}


# ---------------------------------------------------------------------------
# Guest feedback
#
# Native Quality Feedback.document_type is only User or Customer, so the bill
# is stored in the tenant field custom_pos_invoice (S1). This module does not
# create that field. A receipt link is minted with the profile QR secret while
# the bill is rendered; URY marks the bill printed only after rendering, and
# submit_feedback rechecks the live printed-or-paid state.
# mint_signed_invoice_grant / read_signed_invoice_grant are purpose-scoped so
# a later bill grant can reuse the same secret without accepting a feedback QR.
# ---------------------------------------------------------------------------

FEEDBACK_BILL_FIELD = "custom_pos_invoice"
INVOICE_GRANT_LIFETIME_SECONDS = 7 * 24 * 60 * 60
_FEEDBACK_RATINGS = {"1", "2", "3", "4", "5"}


def _utc_epoch(moment):
    return calendar.timegm(moment.utctimetuple())


def mint_signed_invoice_grant(profile_name, invoice_name, purpose, expires_at):
    """Sign an invoice-scoped grant with the profile QR secret. Not guest-callable."""
    secret = _get_profile_secret(profile_name)
    expiry_ts = _utc_epoch(expires_at)
    payload = f"{profile_name}|{purpose}|{invoice_name}|{expiry_ts}"
    signature = _sign(payload, secret)
    raw = f"{payload}|{signature}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def read_signed_invoice_grant(token, purpose):
    """Return (profile_name, invoice_name) or refuse. Purpose is part of the signature."""
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        body, signature = raw.rsplit("|", 1)
        if len(signature) != 64 or any(character not in "0123456789abcdef" for character in signature):
            raise ValueError("Invalid signature")
        profile_name, token_purpose, invoice_name, expiry_text = body.rsplit("|", 3)
        expiry_ts = int(expiry_text)
    except Exception:
        frappe.throw(_("Invalid feedback link"), frappe.PermissionError)

    if not profile_name or not invoice_name or token_purpose != purpose:
        frappe.throw(_("Invalid feedback link"), frappe.PermissionError)

    secret = _get_profile_secret(profile_name)
    expected = _sign(f"{profile_name}|{token_purpose}|{invoice_name}|{expiry_ts}", secret)
    if not hmac.compare_digest(expected, signature):
        frappe.throw(_("Invalid feedback link"), frappe.PermissionError)

    now_ts = _utc_epoch(now_datetime())
    if expiry_ts <= now_ts or expiry_ts > now_ts + INVOICE_GRANT_LIFETIME_SECONDS:
        frappe.throw(_("Invalid feedback link"), frappe.PermissionError)
    return profile_name, invoice_name


def feedback_receipt_link(invoice):
    """Opaque feedback grant for a print format. Empty when it must not be printed."""
    invoice_doc = frappe.get_doc("POS Invoice", invoice)
    invoice_doc.check_permission("print")
    if cint(getattr(invoice_doc, "docstatus", 0)) == 2 or cint(getattr(invoice_doc, "is_return", 0)) == 1:
        return ""
    if not invoice_doc.branch or not invoice_doc.pos_profile:
        return ""

    profiles = frappe.get_all(
        "URY Self Ordering Profile",
        filters={
            "branch": invoice_doc.branch,
            "pos_profile": invoice_doc.pos_profile,
            "enabled": 1,
            "enable_feedback": 1,
        },
        pluck="name",
    )
    if len(profiles) != 1:
        return ""

    expires_at = now_datetime() + timedelta(seconds=INVOICE_GRANT_LIFETIME_SECONDS)
    return mint_signed_invoice_grant(profiles[0], invoice_doc.name, "feedback", expires_at)


def _invoice_accepts_feedback(invoice):
    if cint(getattr(invoice, "docstatus", 0)) == 2 or cint(getattr(invoice, "is_return", 0)) == 1:
        return False
    if cint(getattr(invoice, "invoice_printed", 0)) == 1:
        return True
    return cint(getattr(invoice, "docstatus", 0)) == 1 and flt(getattr(invoice, "outstanding_amount", 1)) <= 0


def _feedback_rating(value):
    if isinstance(value, bool) or isinstance(value, float):
        frappe.throw(_("URY_FEEDBACK_RATINGS"), frappe.ValidationError)
    if isinstance(value, int) and value in (1, 2, 3, 4, 5):
        return str(value)
    if isinstance(value, str) and value in _FEEDBACK_RATINGS:
        return value
    frappe.throw(_("URY_FEEDBACK_RATINGS"), frappe.ValidationError)


def _feedback_parameters(ratings, expected):
    if isinstance(ratings, str):
        try:
            ratings = json.loads(ratings)
        except Exception:
            frappe.throw(_("URY_FEEDBACK_RATINGS"), frappe.ValidationError)
    if not isinstance(ratings, list):
        frappe.throw(_("URY_FEEDBACK_RATINGS"), frappe.ValidationError)

    supplied = {}
    allowed = set(expected)
    for row in ratings:
        if not isinstance(row, dict) or set(row) != {"parameter", "rating"}:
            frappe.throw(_("URY_FEEDBACK_RATINGS"), frappe.ValidationError)
        parameter = row.get("parameter")
        if not isinstance(parameter, str) or parameter not in allowed or parameter in supplied:
            frappe.throw(_("URY_FEEDBACK_RATINGS"), frappe.ValidationError)
        supplied[parameter] = _feedback_rating(row.get("rating"))
    if set(supplied) != allowed:
        frappe.throw(_("URY_FEEDBACK_RATINGS"), frappe.ValidationError)
    return [{"parameter": parameter, "rating": supplied[parameter]} for parameter in expected]


def _limit_feedback_credential(kind, identity):
    """Charge the native limiter using verified scope, independently of IP."""
    if not frappe.request:
        return
    original_form = frappe.local.form_dict
    try:
        frappe.local.form_dict = frappe._dict(original_form)
        frappe.form_dict._ury_feedback_budget = f"feedback:{kind}:{_hash_credential(identity)}"
        rate_limit(key="_ury_feedback_budget", limit=5, seconds=600, ip_based=False)(lambda: None)()
    finally:
        frappe.local.form_dict = original_form


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=5, seconds=600)
def submit_feedback(session=None, receipt_token=None, ratings=None, invoice=None, template=None):
    """Store one Quality Feedback for this session bill, or for a signed receipt.

    `invoice` and `template` are accepted and discarded. The bill is the
    session invoice, or the invoice inside a feedback grant. Never a guest value.
    """
    del invoice, template

    if session:
        resolved = _resolve_session(session)
        profile = frappe.get_doc("URY Self Ordering Profile", resolved.ordering_profile)
        invoice_name = resolved.invoice
        if not invoice_name:
            frappe.throw(_("URY_FEEDBACK_NOT_ELIGIBLE"), frappe.ValidationError)
        _limit_feedback_credential("session", resolved.name)
    elif receipt_token:
        profile_name, invoice_name = read_signed_invoice_grant(receipt_token, "feedback")
        profile = frappe.get_doc("URY Self Ordering Profile", profile_name)
        _limit_feedback_credential("receipt", json.dumps([profile_name, invoice_name]))
    else:
        frappe.throw(_("Invalid feedback link"), frappe.PermissionError)

    if not cint(getattr(profile, "enabled", 0)) or not cint(getattr(profile, "enable_feedback", 0)):
        frappe.throw(_("URY_FEEDBACK_DISABLED"), frappe.ValidationError)
    if not getattr(profile, "feedback_template", None):
        frappe.throw(_("URY_FEEDBACK_TEMPLATE"), frappe.ValidationError)
    if not frappe.get_meta("Quality Feedback").has_field(FEEDBACK_BILL_FIELD):
        frappe.throw(_("URY_FEEDBACK_BILL_LINK_MISSING"), frappe.ValidationError)

    with _elevated():
        try:
            invoice_doc = frappe.get_doc("POS Invoice", invoice_name, for_update=True)
            if invoice_doc.branch != profile.branch or invoice_doc.pos_profile != profile.pos_profile:
                frappe.throw(_("Invalid feedback link"), frappe.PermissionError)
            if not _invoice_accepts_feedback(invoice_doc):
                frappe.throw(_("URY_FEEDBACK_NOT_ELIGIBLE"), frappe.ValidationError)
            if not getattr(invoice_doc, "customer", None):
                frappe.throw(_("URY_FEEDBACK_NOT_ELIGIBLE"), frappe.ValidationError)

            # Current locking reads serialize the exact bill. MariaDB can
            # refuse a stale transaction snapshot rather than expose the winner.
            existing = frappe.db.get_value(
                "Quality Feedback", {FEEDBACK_BILL_FIELD: invoice_doc.name}, "name", for_update=True,
            )
        except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
            # Let the native request boundary roll back; never reset its
            # isolation, commit unrelated writes, or guess that feedback exists.
            frappe.throw(_("URY_FEEDBACK_RETRY: Another request is finishing. Please try again."), frappe.ValidationError)
        if existing:
            return {"status": "Already Received"}

        template_doc = frappe.get_doc("Quality Feedback Template", profile.feedback_template)
        parameters = _feedback_parameters(ratings, [row.parameter for row in template_doc.parameters])
        feedback = frappe.get_doc({
            "doctype": "Quality Feedback",
            "template": profile.feedback_template,
            "document_type": "Customer",
            "document_name": invoice_doc.customer,
            FEEDBACK_BILL_FIELD: invoice_doc.name,
            "parameters": parameters,
        })
        feedback.insert(ignore_permissions=True)

    return {"status": "Received"}
