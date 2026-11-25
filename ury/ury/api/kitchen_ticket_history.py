"""Replay submitted kitchen quantities in issue order, preserving ticket ownership."""

import json
from decimal import Decimal
from types import SimpleNamespace


CANCELLATIONS = ("Cancelled", "Partially cancelled")
TICKET_TYPES = ("New Order", "Order Modified", "Duplicate", *CANCELLATIONS)


def _ledger(invoice):
    """Read legacy round digests or the extended ledger with immutable ticket bindings."""
    raw = invoice.get("ury_applied_rounds") if isinstance(invoice, dict) else getattr(invoice, "ury_applied_rounds", None)
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(value, dict):
        return {}
    return value


def round_ledger(invoice):
    value = _ledger(invoice)
    if isinstance(value.get("applied_rounds"), dict):
        return value["applied_rounds"], value.get("issued_items", {})
    return value, {}


def ticket_ownership(invoice):
    ledger = _ledger(invoice)
    owners = ledger.get("ticket_ownership")
    return owners if isinstance(ledger.get("applied_rounds"), dict) and isinstance(owners, dict) else {}


# The ticket expression is always a fixed SQL identifier supplied by the caller.
WHOLE_COMPENSATION = """EXISTS (SELECT 1 FROM `tabURY KOT` c
    WHERE c.docstatus = 1 AND c.type = 'Cancelled'
      AND FIND_IN_SET({ticket}, REPLACE(c.original_kot, ', ', ',')))"""


def ticket_owner_query(frappe, query, filters=None, *, invoices=None):
    """Bind the ledger receipts as query data. No tenant-specific SQL objects are needed.

    Unscoped reports read only ledgers containing transfers. A scoped lookup also
    needs to suppress locally issued tickets that the requested checks handed away.
    Only the final receiver has a boolean receipt; a forwarding string is not a receipt.
    """
    params = dict(filters or {})
    read = "SELECT name, ury_applied_rounds FROM `tabPOS Invoice` WHERE ury_applied_rounds LIKE %(marker)s"
    lookup = {"marker": '%"ticket_ownership"%'}
    if invoices is not None:
        read += " AND name IN %(checks)s"
        lookup["checks"] = tuple(invoices)
    receipts = {}
    for row in frappe.db.sql(read, lookup, as_dict=True):
        for ticket, binding in ticket_ownership(row).items():
            if binding is True:
                receipts[ticket] = row.name
            elif invoices is not None and isinstance(binding, str):
                receipts.setdefault(ticket, None)
    params["kitchen_receipts"] = json.dumps(list(receipts.items()), separators=(",", ":"))
    # JSON_TABLE sees only Python-encoded data, including for legacy/malformed ledgers.
    relation = """(SELECT k.name AS ticket,
            CASE WHEN r.ticket IS NULL THEN k.invoice ELSE r.invoice END AS invoice,
            {cancelled} AS wholly_cancelled
        FROM `tabURY KOT` k
        LEFT JOIN JSON_TABLE(%(kitchen_receipts)s, '$[*]' COLUMNS(
            ticket VARCHAR(140) PATH '$[0]', invoice VARCHAR(140) PATH '$[1]'
        )) r ON r.ticket = k.name)""".format(cancelled=WHOLE_COMPENSATION.format(ticket="k.name"))
    return query.replace("{ticket_owners}", relation), params


def run_ticket_report(name, filters):
    """Run a standard report with its native column, filter and read-only SQL handling."""
    import frappe

    report = frappe.get_doc("Report", name)
    report.query, params = ticket_owner_query(frappe, report.query, filters)
    return report.execute_query_report(params)


def write_round_ledger(invoice, rounds, bindings, ownership=None):
    if ownership is None:
        ownership = ticket_ownership(invoice)
    data = {"applied_rounds": rounds, "issued_items": bindings} if bindings or ownership else rounds
    if ownership:
        data["ticket_ownership"] = ownership
    invoice.ury_applied_rounds = json.dumps(data, sort_keys=True, separators=(",", ":"))


def record_ticket_transfer(invoice, tickets, owner):
    """True marks tickets received here. A check name forwards released tickets to their next owner."""
    if tickets:
        ownership = ticket_ownership(invoice)
        ownership.update(dict.fromkeys(tickets, owner))
        write_round_ledger(invoice, *round_ledger(invoice), ownership)
        if getattr(invoice, "flags", None) is not None:
            invoice.flags.ury_issued_bindings = _SERVER_BINDINGS


def invoice_ticket_ledger(frappe, name):
    rows = frappe.db.get_values("POS Invoice", name, ["ury_applied_rounds"], as_dict=True)
    return SimpleNamespace(name=name, ury_applied_rounds=rows[0].get("ury_applied_rounds") if rows else None)


def current_ticket_owner(frappe, ticket, issuer, *, for_update=False, ledgers=None):
    """Follow the audited transfers starting at the permanent issuing invoice."""
    if not issuer:
        return SimpleNamespace(name=issuer)

    def load(name):
        if for_update:
            return frappe.get_doc("POS Invoice", name, for_update=True)
        if ledgers is None:
            return invoice_ticket_ledger(frappe, name)
        if name not in ledgers:
            ledgers[name] = invoice_ticket_ledger(frappe, name)
        return ledgers[name]

    invoice = load(issuer)
    visited = {issuer}
    while isinstance(owner := ticket_ownership(invoice).get(ticket), str):
        if owner in visited:
            from frappe import _
            from ury.brand import coded_message

            frappe.throw(coded_message("KOT-OWNERSHIP", _("Cannot identify the original kitchen ticket for this line.")))
        visited.add(owner)
        invoice = load(owner)
    return invoice


def owned_ticket_filters(invoice):
    """Disjoint indexed queries for locally issued tickets and tickets received by transfer."""
    ownership = ticket_ownership(invoice)
    local = {"invoice": invoice["name"] if isinstance(invoice, dict) else invoice.name}
    if ownership:
        local["name"] = ("not in", list(ownership))
    yield local
    received = [ticket for ticket, owner in ownership.items() if owner is True]
    if received:
        yield {"name": ("in", received)}


def owned_kots(frappe, invoice, *, fields=("name",), filters=None, names=None, active=False):
    """Read an owner's tickets in issue order, including receipts from previous checks.

    A submitted whole-check cancellation closes its originals immediately. Kitchen
    acknowledgement is a separate workflow and does not reopen those releases.
    """
    if isinstance(invoice, str):
        invoice = invoice_ticket_ledger(frappe, invoice)
    scopes = list(owned_ticket_filters(invoice))
    selected = set(names) if names is not None else None
    rows = []
    read_fields = list(dict.fromkeys([*fields, "name", "creation"]))
    closed = set()
    for scope in scopes:
        if active:
            cancels = frappe.get_all("URY KOT", filters=dict(scope, docstatus=1, type="Cancelled"),
                                     fields=["original_kot"])
            for cancel in cancels:
                closed.update(original_tickets(cancel.get("original_kot")))
        query = dict(filters or {}, **scope)
        if active:
            query.update(docstatus=1, type=("not in", CANCELLATIONS))
        if selected is not None:
            candidates = selected
            if "name" in scope:
                op, keys = scope["name"]
                candidates = selected.intersection(keys) if op == "in" else selected.difference(keys)
            if not candidates:
                continue
            query["name"] = ("in", sorted(candidates))
        rows.extend(frappe.get_all("URY KOT", filters=query,
                                   fields=read_fields,
                                   order_by="creation asc, name asc"))
    if len(scopes) > 1:
        rows.sort(key=lambda row: (row.creation, row.name))
    return [row for row in rows if row.name not in closed]


_SERVER_BINDINGS = object()


def set_issued_bindings(invoice, bindings):
    rounds, _ = round_ledger(invoice)
    write_round_ledger(invoice, rounds, bindings)
    invoice.flags.ury_issued_bindings = _SERVER_BINDINGS


def protect_issued_bindings(invoice):
    """Only the locked merge and split paths can assign issued items to billing lines."""
    if getattr(invoice.flags, "ury_issued_bindings", None) is _SERVER_BINDINGS:
        return
    previous = getattr(invoice, "_doc_before_save", None)
    rounds, requested = round_ledger(invoice)
    _, saved = round_ledger(previous)
    if requested != saved or ticket_ownership(invoice) != ticket_ownership(previous):
        write_round_ledger(invoice, rounds, saved, ticket_ownership(previous))


def bound_item(row, ticket_bindings):
    changes = ticket_bindings.get(row.get("name"))
    if not changes:
        return row
    return dict(row if isinstance(row, dict) else row.as_dict(), **changes)


def original_tickets(value):
    return {name.strip() for name in (value or "").split(",") if name.strip()}


def is_wholly_cancelled(frappe, ticket):
    """Check exact links, including historical comma-separated whole-check cancellations."""
    links = frappe.db.get_values("URY KOT", {"type": "Cancelled", "docstatus": 1,
                                 "original_kot": ("like", f"%{ticket}%")}, ["original_kot"], as_dict=True)
    return any(ticket in original_tickets(row.get("original_kot")) for row in links)


def matches_release(row, item, key, note, course=None, seat=0):
    if row.get("item") != item or (row.get("comments") or "") != (note or ""):
        return False
    if key and row.get("reservation_line_key"):
        return row.get("reservation_line_key") == key
    return (not course or not row.get("course") or course == row.get("course")) and int(row.get("seat_no") or 0) == seat


def legacy_line_matches(ticket_line, invoice_line):
    """Legacy tickets identify a dish by placement and notes until it acquires a durable key."""
    key = ticket_line.get("reservation_line_key")
    return (not key or key.startswith(("pos:", "ctx:"))) and matches_release(
        ticket_line, invoice_line.item_code, None, invoice_line.get("comment"),
        invoice_line.get("custom_course"), int(invoice_line.get("custom_seat_no") or 0),
    )


def issued_line_candidates(ticket_line, candidates):
    """Canonical references are explicit. Historical positions may also be literal raw keys."""
    from ury.ury.api.ury_order_reservation_service import _line_key

    key = ticket_line.get("reservation_line_key")
    if key and key.startswith("ref:"):
        canonical = [row for row in candidates if _line_key(row.item_code, row, 1) == key]
        if canonical:
            return canonical
    return [row for row in candidates if (key and key == (row.get("reservation_line_key") or row.name))
            or (not row.get("reservation_line_key") and legacy_line_matches(ticket_line, row))]


def take_releases(releases, item, key, note, qty, originals=None, *, course=None, seat=0):
    """Debit newest matching releases first and return the exact quantity taken from each."""
    taken = []
    for release in reversed(releases):
        name, row, remaining = release
        if originals is not None and name not in originals:
            continue
        if not matches_release(row, item, key, note, course, seat):
            continue
        take = min(qty, remaining)
        if take > 0:
            taken.append((name, take))
            release[2] -= take
            qty -= take
        if qty <= 0:
            break
    return taken


def submitted_tickets(frappe, invoice):
    # The invoice is already authorized and locked. Include every issuing outlet,
    # and take current reads of both parent and child rows before moving ownership.
    tickets = []
    for filters in owned_ticket_filters(invoice):
        tickets.extend(frappe.db.get_values(
            "URY KOT", dict(filters, docstatus=1, type=("in", TICKET_TYPES)),
            ["name", "creation", "type", "original_kot", "production", "pos_profile", "release_group"],
            order_by="creation asc, name asc", as_dict=True, for_update=True,
        ))
    if ticket_ownership(invoice):
        tickets.sort(key=lambda row: (row.creation, row.name))
    by_name = {ticket.name: ticket for ticket in tickets}
    if by_name:
        for ticket in tickets:
            ticket.kot_items = []
        rows = frappe.db.get_values(
            "URY KOT Items", {"parent": ("in", list(by_name)), "parenttype": "URY KOT"},
            ["name", "parent", "item", "comments", "course", "seat_no", "reservation_line_key",
             "parent_line_key", "quantity", "cancelled_qty"],
            order_by="parent, idx", as_dict=True, for_update=True,
        )
        for row in rows:
            by_name[row.parent].kot_items.append(row)
    return list(by_name.values())


def apply_cancellation(releases, rows, originals, *, full=False):
    if not originals:
        return False
    if full and not any(row.get("cancelled_qty") for row in rows):
        for release in releases:
            if release[0] in originals:
                release[2] = Decimal(0)
        return True
    complete = True
    for row in rows:
        qty = Decimal(str(row.get("cancelled_qty") or 0))
        taken = take_releases(releases, row.get("item"), row.get("reservation_line_key"), row.get("comments"),
                              qty, originals, course=row.get("course"), seat=int(row.get("seat_no") or 0))
        if sum(amount for _, amount in taken) != qty:
            complete = False
    return complete


def outstanding_releases(tickets):
    releases = []
    for ticket in tickets:
        if ticket.get("type") in CANCELLATIONS:
            apply_cancellation(releases, ticket.kot_items, original_tickets(ticket.get("original_kot")),
                               full=ticket.get("type") == "Cancelled")
        else:
            releases.extend([ticket.name, row, Decimal(str(row.get("quantity") or 0))] for row in ticket.kot_items)
    return releases


def split_ticket_families(frappe, invoice, bindings):
    """Yield indivisible release/cancellation histories and their outstanding lines."""
    headers = []
    for filters in owned_ticket_filters(invoice):
        headers.extend(frappe.get_all(
            "URY KOT", filters=dict(filters, docstatus=1),
            fields=["name", "creation", "type", "original_kot"], order_by="creation asc, name asc",
        ))
    if ticket_ownership(invoice):
        headers.sort(key=lambda row: (row.creation, row.name))
    tickets = [getattr(header, "name", header) for header in headers]
    cancellations = {row.name: row for row in headers if getattr(row, "type", None) in CANCELLATIONS}
    rows_by_ticket = {}
    links = {name: set() for name in tickets}
    releases = []
    uncertain = set()
    for name in tickets:
        rows = frappe.get_all("URY KOT Items", filters={"parent": name, "parenttype": "URY KOT"},
                              fields=["name", "reservation_line_key", "item", "quantity", "cancelled_qty",
                                      "comments", "course", "seat_no"], order_by="idx")
        rows_by_ticket[name] = rows
        if name not in cancellations:
            releases.extend([name, row, Decimal(str(row.get("quantity") or 0))] for row in rows)
            continue
        originals = original_tickets(cancellations[name].original_kot)
        if not originals or not originals.issubset(links):
            uncertain.add(name)
        for original in originals.intersection(links):
            links[name].add(original)
            links[original].add(name)
        if not apply_cancellation(releases, rows, originals, full=cancellations[name].type == "Cancelled"):
            uncertain.add(name)

    remaining = {id(row): qty for _, row, qty in releases}
    seen = set()
    for name in tickets:
        if name in seen:
            continue
        family, pending = [], [name]
        while pending:
            ticket = pending.pop()
            if ticket not in seen:
                seen.add(ticket)
                family.append(ticket)
                pending.extend(sorted(links[ticket] - seen))
        rows = []
        ambiguous = not uncertain.isdisjoint(family)
        for ticket in family:
            for row in rows_by_ticket[ticket]:
                if ambiguous:
                    rows.append((row, Decimal(str(row.get("quantity") or 0))))
                elif id(row) in remaining:
                    qty = remaining[id(row)]
                    if qty or not row.get("quantity"):
                        rows.append((bound_item(row, bindings.get(ticket, {})), qty))
        yield tuple(family), rows, ambiguous
