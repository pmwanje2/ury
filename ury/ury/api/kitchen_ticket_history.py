"""Replay submitted kitchen quantities in issue order, preserving ticket ownership."""

import json
from decimal import Decimal


CANCELLATIONS = ("Cancelled", "Partially cancelled")
TICKET_TYPES = ("New Order", "Order Modified", "Duplicate", *CANCELLATIONS)


def round_ledger(invoice):
    """Read legacy round digests or the extended ledger with immutable ticket bindings."""
    raw = getattr(invoice, "ury_applied_rounds", None)
    if not isinstance(raw, str) or not raw:
        return {}, {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}, {}
    if not isinstance(value, dict):
        return {}, {}
    if isinstance(value.get("applied_rounds"), dict):
        return value["applied_rounds"], value.get("issued_items", {})
    return value, {}


def write_round_ledger(invoice, rounds, bindings):
    data = {"applied_rounds": rounds, "issued_items": bindings} if bindings else rounds
    invoice.ury_applied_rounds = json.dumps(data, sort_keys=True, separators=(",", ":"))


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
    if requested != saved:
        write_round_ledger(invoice, rounds, saved)


def bound_item(row, ticket_bindings):
    changes = ticket_bindings.get(row.get("name"))
    return dict(row, **changes) if changes else row


def original_tickets(value):
    return {name.strip() for name in (value or "").split(",") if name.strip()}


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
    tickets = frappe.db.get_values(
        "URY KOT", {"invoice": invoice, "docstatus": 1, "type": ("in", TICKET_TYPES)},
        ["name", "type", "original_kot", "production", "pos_profile", "release_group"],
        order_by="creation asc, name asc", as_dict=True, for_update=True,
    )
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
    headers = frappe.get_all(
        "URY KOT", filters={"invoice": invoice, "docstatus": 1},
        fields=["name", "type", "original_kot"], order_by="creation asc, name asc",
    )
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
