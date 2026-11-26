"""Walk invoice modifier links in parent-before-child order, independent of display order."""
from collections import defaultdict, deque

import frappe
from frappe import _


def modifier_edges(rows, roots):
    children = defaultdict(list)
    for row in rows:
        if parent_key := row.get("custom_parent_line_key"):
            children[parent_key].append(row)
    pending = deque(roots)
    visited = {row.name for row in pending}
    while pending:
        parent = pending.popleft()
        for child in children.get(parent.get("reservation_line_key") or parent.name, ()):
            if child.name in visited:
                frappe.throw(_("A linked item has no parent on this check."))
            visited.add(child.name)
            pending.append(child)
            yield parent, child
