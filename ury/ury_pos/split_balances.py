"""Conserve the already-priced check through split, payment and consolidation."""

import json
from decimal import Decimal, ROUND_HALF_UP

import frappe
from frappe import _
from frappe.utils import cint, flt

from ury.brand import coded_message


LINE_AMOUNTS = (
    "amount", "net_amount", "base_amount", "base_net_amount",
    "distributed_discount_amount", "stock_qty",
)
TAX_AMOUNTS = (
    "tax_amount", "base_tax_amount", "tax_amount_after_discount_amount",
    "base_tax_amount_after_discount_amount", "net_amount", "base_net_amount",
)
_SERVER_BALANCE = object()
HEADER_AMOUNTS = (
    "total", "base_total", "net_total", "base_net_total", "grand_total",
    "base_grand_total", "rounded_total", "base_rounded_total",
    "rounding_adjustment", "base_rounding_adjustment", "total_taxes_and_charges",
    "base_total_taxes_and_charges", "taxes_and_charges_added",
    "base_taxes_and_charges_added", "taxes_and_charges_deducted",
    "base_taxes_and_charges_deducted", "discount_amount", "base_discount_amount",
    "custom_charged_tip",
)


def _decimal(value):
    return Decimal(str(value or 0))


def _units(value, places):
    return int((_decimal(value) * 10**places).to_integral_value(rounding=ROUND_HALF_UP))


def _amount(minor, places):
    return float(Decimal(minor).scaleb(-places))


def _partition(fields, row, ratios, currency_places, base_places):
    parent, child = {}, {}
    for field in fields:
        value = row.get(field)
        if value is None:
            continue
        places = 4 if field == "stock_qty" else base_places if field.startswith("base_") else currency_places
        minor = _units(value, places)
        ratio = ratios.get(field, ratios.get("*", Decimal(0)))
        moved = int((minor * ratio).to_integral_value(rounding=ROUND_HALF_UP))
        parent[field], child[field] = _amount(minor - moved, places), _amount(moved, places)
    return parent, child


def _net(row, base=False):
    field = "base_net_amount" if base else "net_amount"
    return _decimal(row.get(field) if row.get(field) is not None else _decimal(row.get("qty")) * _decimal(row.get("rate")))


def _ratio(part, whole):
    return min(Decimal(1), max(Decimal(0), part / whole)) if whole else Decimal(0)


def allocate_split_balances(check, move_map, currency_places, base_places):
    """Allocate in integer minor units and leave the exact residual on Check 1."""
    source = {"items": {}, "taxes": [], "details": [], "header": {}}
    child = {"items": {}, "taxes": [], "details": [], "header": {}}
    ratios_by_name = {}
    local_total = base_total = local_move = base_move = Decimal(0)
    total_qty = moved_qty = Decimal(0)
    for row in check.get("items") or ():
        qty = _decimal(row.get("qty"))
        move = _decimal(move_map.get(row.name))
        ratio = ratios_by_name[row.name] = _ratio(move, qty)
        parent_values, child_values = _partition(LINE_AMOUNTS, row, {"*": ratio}, currency_places, base_places)
        if qty > move:
            source["items"][row.name] = parent_values
        if move:
            child["items"][row.name] = child_values
        total_qty += qty
        moved_qty += move
        local_total += _net(row)
        local_move += _net(row) * ratio
        base_total += _net(row, base=True)
        base_move += _net(row, base=True) * ratio

    local_ratio = _ratio(local_move, local_total) if local_total else _ratio(moved_qty, total_qty)
    base_ratio = _ratio(base_move, base_total) if base_total else local_ratio
    field_ratios = {
        field: base_ratio if field.startswith("base_") else local_ratio
        for field in (*HEADER_AMOUNTS, *TAX_AMOUNTS)
    }
    details_by_tax = {}
    for detail in check.get("item_wise_tax_details") or ():
        if detail.get("item") in ratios_by_name:
            details_by_tax.setdefault(detail.get("tax"), []).append(detail)
    for index, tax in enumerate(check.get("taxes") or ()):
        details = details_by_tax.get(tax.get("name"), ())
        total = sum((_decimal(detail.get("amount")) for detail in details), Decimal(0))
        moved = sum((_decimal(detail.get("amount")) * ratios_by_name[detail.get("item")] for detail in details), Decimal(0))
        row_ratios = {field: _ratio(moved, total) for field in TAX_AMOUNTS} if total else field_ratios
        left, right = _partition(TAX_AMOUNTS, tax, row_ratios, currency_places, base_places)
        labels = {
            field: tax.get(field)
            for field in ("account_head", "description", "cost_center", "add_deduct_tax", "custom_pos_charge_kind")
            if tax.get(field) is not None
        }
        source["taxes"].append({**labels, **left})
        child["taxes"].append({**labels, **right})
        start = len(child["details"])
        for detail in details:
            key = detail.get("item")
            left, right = _partition(("amount", "taxable_amount"), detail, {"*": ratios_by_name[key]},
                                     currency_places, base_places)
            for snapshot, amounts in ((source, left), (child, right)):
                if key in snapshot["items"]:
                    snapshot["details"].append(dict(amounts, item=key, tax=index, rate=flt(detail.get("rate"))))
        # Allocate the tax row from the individually rounded item detail when
        # those details represent the row total. Every item's remaining detail
        # and the accounting row then carry the same residual.
        detail_total = _units(total, currency_places)
        moved_detail = sum(_units(detail.get("amount"), currency_places) for detail in child["details"][start:])
        if details:
            for field in ("tax_amount", "tax_amount_after_discount_amount"):
                if field in child["taxes"][-1] and _units(tax.get(field), currency_places) == detail_total:
                    source["taxes"][-1][field] = _amount(detail_total - moved_detail, currency_places)
                    child["taxes"][-1][field] = _amount(moved_detail, currency_places)

    source["header"], child["header"] = _partition(HEADER_AMOUNTS, check, field_ratios, currency_places, base_places)
    for prefix, places in (("", currency_places), ("base_", base_places)):
        net = prefix + "net_total"
        total = prefix + "total"
        taxes = prefix + "total_taxes_and_charges"
        tax_amount = prefix + "tax_amount_after_discount_amount"
        grand = prefix + "grand_total"
        rounded = prefix + "rounded_total"
        adjustment = prefix + "rounding_adjustment"
        for snapshot in (source, child):
            rows, tax_rows, header = snapshot["items"].values(), snapshot["taxes"], snapshot["header"]
            for field, line_field in ((net, prefix + "net_amount"), (total, prefix + "amount")):
                if field in header and all(line_field in row for row in rows):
                    header[field] = _amount(sum(_units(row[line_field], places) for row in rows), places)
            if taxes in header:
                header[taxes] = _amount(sum(
                    (-1 if row.get("add_deduct_tax") == "Deduct" else 1) * _units(row.get(tax_amount), places)
                    for row in tax_rows
                ), places)
        # Preserve the exact original grand total and any difference between
        # its total and the sum of net lines plus taxes (for example a tip).
        child_header = child["header"]
        if grand in child_header and net in child_header:
            original_tax = _units(check.get(taxes), places) if check.get(taxes) is not None else sum(
                (-1 if row.get("add_deduct_tax") == "Deduct" else 1) * _units(row.get(tax_amount), places)
                for row in check.get("taxes") or ()
            )
            child_tax = _units(child_header.get(taxes), places) if taxes in child_header else sum(
                (-1 if row.get("add_deduct_tax") == "Deduct" else 1) * _units(row.get(tax_amount), places)
                for row in child["taxes"]
            )
            other = _units(check.get(grand), places) - _units(check.get(net), places) - original_tax
            child_grand = _units(child_header[net], places) + child_tax + int(
                (other * (base_ratio if prefix else local_ratio)).to_integral_value(rounding=ROUND_HALF_UP)
            )
            child_header[grand] = _amount(child_grand, places)
            source["header"][grand] = _amount(_units(check.get(grand), places) - child_grand, places)
        for snapshot in (source, child):
            header = snapshot["header"]
            if adjustment in header and rounded in header and header.get(grand) is not None and check.get(rounded):
                header[rounded] = _amount(_units(header[grand], places) + _units(header[adjustment], places), places)
    return source, child


def set_split_balance(doc, snapshot, row_names):
    """Register allocations for a document that was produced by the split server."""
    snapshot["items"] = {
        row.get("custom_origin_invoice_item") or row.name: dict(snapshot["items"][key],
            qty=row.qty, item_code=row.get("item_code"), conversion_factor=row.get("conversion_factor"))
        for row, key in zip(doc.items, row_names, strict=True)
    }
    snapshot["details"] = [
        dict(detail, item=row.get("custom_origin_invoice_item") or row.name)
        for detail in snapshot["details"]
        for row, key in zip(doc.items, row_names, strict=True)
        if key == detail["item"]
    ]
    if "custom_charged_tip" in snapshot["header"]:
        doc.custom_charged_tip = snapshot["header"]["custom_charged_tip"]
    doc.flags.ury_split_balance_owner = _SERVER_BALANCE
    doc.flags.ury_split_balance = snapshot
    doc.custom_split_balance = json.dumps(snapshot, separators=(",", ":"))


def get_saved_split_balance(doc):
    flags = getattr(doc, "flags", None)
    working = getattr(flags, "ury_split_balance", None)
    if getattr(flags, "ury_split_balance_owner", None) is _SERVER_BALANCE:
        return working
    # Load the server copy even when a client omits or alters the hidden field.
    if doc.is_new():
        doc.custom_split_balance = None
        return None
    previous = doc.get_doc_before_save()
    if previous:
        raw = previous.get("custom_split_balance")
    elif doc.get("custom_split_group") or doc.get("custom_split_balance"):
        raw = frappe.db.get_value("POS Invoice", doc.name, "custom_split_balance")
    else:
        raw = None
    if not raw:
        doc.custom_split_balance = None
    return json.loads(raw) if raw else None


def _allocated_rows(doc, snapshot):
    expected = snapshot["items"]
    seen = set()
    for row in doc.get("items") or ():
        key = row.get("custom_origin_invoice_item") or row.name
        values = expected.get(key)
        if values is None and cint(row.get("is_disposable")) and not flt(row.get("amount")):
            continue
        if (key in seen or not values or row.get("item_code") != values["item_code"]
                or any(_decimal(row.get(field)) != _decimal(values[field])
                       for field in ("qty", "conversion_factor"))):
            frappe.throw(coded_message(
                "SPLIT-ALLOCATION-LOCKED", _("The quantities or items on this split check have changed."),
            ))
        seen.add(key)
        yield key, row, values
    if seen != set(expected):
        frappe.throw(coded_message("SPLIT-ALLOCATION-LOCKED", _("The split check is missing its allocated items.")))


def restore_split_stock(doc, snapshot):
    if snapshot:
        for _, row, values in _allocated_rows(doc, snapshot):
            if "stock_qty" in values:
                row.stock_qty = values["stock_qty"]


def restore_split_balance(doc, snapshot):
    if not snapshot:
        return
    allocated_rows = {}
    for key, row, values in _allocated_rows(doc, snapshot):
        allocated_rows[key] = row
        for field in LINE_AMOUNTS:
            if field in values:
                row.set(field, values[field])
        for amount, rate in (("net_amount", "net_rate"), ("base_net_amount", "base_net_rate")):
            if amount in values and row.qty:
                row.set(rate, values[amount] / row.qty)

    # Native recalculation can rebuild rows from the current tax and service
    # templates. Use the exact original posting account, row type and amounts.
    native_taxes = list(doc.get("taxes") or ())
    tax_rows = []
    tax_rows_by_index = {}
    existing_tip, existing_base_tip = Decimal(0), Decimal(0)
    live_tip = next((row for row in native_taxes if row.get("custom_pos_charge_kind") == "Charged Tip"), None)
    running = _decimal(snapshot["header"].get("net_total"))
    base_running = _decimal(snapshot["header"].get("base_net_total"))
    for index, data in enumerate(snapshot["taxes"]):
        if data.get("custom_pos_charge_kind") == "Charged Tip":
            existing_tip += _decimal(data.get("tax_amount_after_discount_amount"))
            existing_base_tip += _decimal(data.get("base_tax_amount_after_discount_amount"))
            continue
        native = native_taxes[index] if index < len(native_taxes) else None
        if not native or native.get("account_head") != data.get("account_head"):
            tax = doc.append("taxes", {})
        else:
            tax = native
        tax.update(data)
        tax.charge_type = "Actual"
        tax.included_in_print_rate = 0
        tax.row_id = None
        tax.dont_recompute_tax = 1
        sign = -1 if data.get("add_deduct_tax") == "Deduct" else 1
        running += sign * _decimal(data.get("tax_amount_after_discount_amount"))
        base_running += sign * _decimal(data.get("base_tax_amount_after_discount_amount"))
        tax.total, tax.base_total = float(running), float(base_running)
        tax_rows.append(tax)
        tax_rows_by_index[index] = tax
    if live_tip:
        live_tip.total = float(running + _decimal(live_tip.get("tax_amount_after_discount_amount")))
        live_tip.base_total = float(base_running + _decimal(live_tip.get("base_tax_amount_after_discount_amount")))
        tax_rows.append(live_tip)
    doc.set("taxes", tax_rows)
    if doc.meta.has_field("item_wise_tax_details"):
        doc.set("item_wise_tax_details", [])
        for detail in snapshot["details"]:
            item = allocated_rows[detail["item"]]
            tax = tax_rows_by_index.get(detail["tax"])
            if tax and item.name and tax.name:
                doc.append("item_wise_tax_details", dict(detail, item=item.name, tax=tax.name))
    # A new tender may add a tip after splitting. The saved bill amount
    # remains fixed while the separate payable row follows that tender.
    tip_delta = _decimal(live_tip.get("tax_amount_after_discount_amount") if live_tip else 0) - existing_tip
    base_tip_delta = _decimal(live_tip.get("base_tax_amount_after_discount_amount") if live_tip else 0) - existing_base_tip
    for field, value in snapshot["header"].items():
        if field == "custom_charged_tip":
            continue
        if field in ("grand_total", "total_taxes_and_charges", "taxes_and_charges_added") or (field == "rounded_total" and value):
            value = float(_decimal(value) + tip_delta)
        if field in ("base_grand_total", "base_total_taxes_and_charges", "base_taxes_and_charges_added") or (field == "base_rounded_total" and value):
            value = float(_decimal(value) + base_tip_delta)
        doc.set(field, value)
    doc.custom_split_balance = json.dumps(snapshot, separators=(",", ":"))
    doc.set_paid_amount()
    doc.set_outstanding_amount()


class ConsolidatedSplitStock:
    def set_qty_as_per_stock_uom(self):
        super().set_qty_as_per_stock_uom()
        if not self.get("is_consolidated"):
            return
        linked = {row.pos_invoice_item: row for row in self.get("items") if row.get("pos_invoice_item")}
        if not linked:
            return
        allocations = frappe.db.sql(
            """SELECT item.name, item.stock_qty
               FROM `tabPOS Invoice Item` item
               JOIN `tabPOS Invoice` invoice ON invoice.name = item.parent
               WHERE item.name IN %(rows)s AND COALESCE(invoice.custom_split_balance, '') != ''""",
            {"rows": tuple(linked)}, as_dict=True,
        )
        for allocation in allocations:
            linked[allocation.name].stock_qty = allocation.stock_qty
