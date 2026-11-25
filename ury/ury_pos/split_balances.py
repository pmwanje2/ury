"""Conserve the already-priced check through split, payment and consolidation."""

import json
from collections import Counter
from contextlib import contextmanager
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
_DISCOUNT_REPRICE = object()
_PREVIOUS_TAX = {"On Previous Row Amount": "amount", "On Previous Row Total": "total"}
_ITEM_RATE_CHARGES = {"On Net Total", "On Item Quantity", *_PREVIOUS_TAX}
_TAX_PRICING_FIELDS = (
    "charge_type", "rate", "row_id", "included_in_print_rate", "included_in_paid_amount", "dont_recompute_tax",
)
DETAIL_AMOUNTS = ("amount", "taxable_amount", "base_amount", "base_taxable_amount")
TAX_DETAIL_FIELDS = {
    "tax_amount": "amount", "tax_amount_after_discount_amount": "amount", "net_amount": "taxable_amount",
}
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


def _partition(fields, row, ratios, currency_places, base_places, *, stock_places=None):
    parent, child = {}, {}
    for field in fields:
        value = row.get(field)
        if value is None:
            continue
        if field == "stock_qty":
            places = 4 if stock_places is None else stock_places
            if stock_places is not None:
                value = flt(value, places)
        else:
            places = base_places if field.startswith("base_") else currency_places
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


def _fixed_tax_weights(item_names, source_rows, amount):
    """Use ERPNext's Actual charge basis for the applicable items."""
    nets = [_net(source_rows[name]) for name in item_names]
    net_total = sum(nets)
    if net_total:
        return [_decimal(amount) * net / net_total for net in nets]
    return [Decimal(0)] * (len(nets) - 1) + [_decimal(amount)] if nets else []


def _tax_detail_allocations(
    tax, details, source_rows, exchange_rate, currency_places, base_places, previous, fixed_fees=None,
):
    """Recover transaction detail from native company-currency detail and the tax row."""
    sign = -1 if tax.get("add_deduct_tax") == "Deduct" else 1
    txn_tax = tax.get("tax_amount_after_discount_amount")
    if txn_tax is None:
        txn_tax = tax.get("tax_amount")
    weights = [_decimal(row.get("amount")) for row in details]
    charge_type = tax.get("charge_type")
    item_names = [detail.get("item_row") for detail in details]
    bases = None
    txn_weights = []
    if charge_type in _PREVIOUS_TAX:
        bases = [
            previous.get(name, Decimal(0)) if previous is not None else _net(source_rows[name])
            for name in item_names
        ]
        txn_weights = [base * _decimal(detail.get("rate")) / 100
                       for base, detail in zip(bases, details, strict=True)]
        if not any(txn_weights) or (not sum(txn_weights) and _decimal(txn_tax)):
            txn_weights = _fixed_tax_weights(item_names, source_rows, txn_tax)
    elif charge_type == "Actual":
        if fixed_fees is None:
            txn_weights = _fixed_tax_weights(item_names, source_rows, txn_tax)
        else:
            txn_weights = [_decimal(fixed_fees.get(name))
                           for name in item_names]
    elif charge_type == "On Item Quantity":
        txn_weights = [_decimal(source_rows[name].get("qty")) * _decimal(detail.get("rate"))
                       for name, detail in zip(item_names, details, strict=True)]
    elif charge_type == "On Net Total" or not any(weights):
        txn_weights = [_net(source_rows[name]) * _decimal(detail.get("rate")) / 100
                       for name, detail in zip(item_names, details, strict=True)]
    has_transaction_weights = bool(sum(txn_weights)) or (not _decimal(txn_tax) and any(txn_weights))
    if has_transaction_weights:
        weights = txn_weights

    total_weight = sum(weights)
    total_units = _units(txn_tax, currency_places) * sign
    running_weight = Decimal(0)
    previous_units = 0
    allocations = []
    for index, (name, detail, weight) in enumerate(zip(item_names, details, weights, strict=True)):
        running_weight += weight
        if total_weight:
            units = int((total_units * running_weight / total_weight).to_integral_value(rounding=ROUND_HALF_UP))
        elif has_transaction_weights and not total_units:
            # Opposite signed components can be individually payable even
            # when their sum is zero. Round cumulative transaction amounts.
            units = sign * _units(running_weight, currency_places)
        else:
            units = 0
        taxable = bases[index] if bases is not None else _decimal(detail.get("taxable_amount")) / exchange_rate
        allocations.append({
            "item": name,
            "rate": flt(detail.get("rate")),
            "amount": _amount(units - previous_units, currency_places),
            "taxable_amount": _amount(_units(taxable, currency_places), currency_places),
            "base_amount": _amount(_units(detail.get("amount"), base_places), base_places),
            "base_taxable_amount": _amount(_units(detail.get("taxable_amount"), base_places), base_places),
        })
        previous_units = units
    # Subsequent native tax rows must never inherit company-currency weights.
    unrounded = txn_weights if has_transaction_weights else _fixed_tax_weights(item_names, source_rows, txn_tax)
    return allocations, dict(zip(item_names, unrounded, strict=True))


def _tax_pricing_rule(tax):
    charge_type = tax.get("charge_type")
    if not charge_type:
        return {}
    return {
        "charge_type": charge_type,
        "rate": flt(tax.get("rate")),
        "row_id": cint(tax.get("row_id")) or None,
        "included_in_print_rate": cint(tax.get("included_in_print_rate")),
        "included_in_paid_amount": cint(tax.get("included_in_paid_amount")),
        "dont_recompute_tax": cint(tax.get("dont_recompute_tax")),
    }


def _matching_tax_account(left, right):
    return all(left.get(field) == right.get(field) for field in ("account_head", "custom_pos_charge_kind"))


def _saved_tax_pricing_rules(check, snapshot):
    """Read captured tax rules, or recover matching template rows for old splits."""
    template_name = check.get("taxes_and_charges")
    template = ()
    if template_name and any(
        not row.get("charge_type") and not row.get("custom_pos_charge_kind") for row in snapshot["taxes"]
    ):
        template = frappe.get_all(
            "Sales Taxes and Charges",
            filters={"parent": template_name, "parenttype": "Sales Taxes and Charges Template"},
            fields=["account_head", *_TAX_PRICING_FIELDS],
            order_by="idx asc",
        )

    for index, tax in enumerate(snapshot["taxes"]):
        if tax.get("charge_type"):
            yield _tax_pricing_rule(tax)
        elif index < len(template) and _matching_tax_account(template[index], tax):
            yield _tax_pricing_rule(template[index])
        else:
            yield {}


def _partition_tax(tax, index, details, ratios, fallback, currency_places, base_places, source, child, rule):
    sign = -1 if tax.get("add_deduct_tax") == "Deduct" else 1
    row_ratios = dict(fallback)
    totals = {field: sum((_decimal(detail.get(field)) for detail in details), Decimal(0))
              for field in DETAIL_AMOUNTS}
    moved = {field: sum((_decimal(detail.get(field)) * ratios[detail["item"]] for detail in details), Decimal(0))
             for field in DETAIL_AMOUNTS}
    for prefix in ("", "base_"):
        for field, detail_field in TAX_DETAIL_FIELDS.items():
            key = prefix + detail_field
            if totals[key]:
                row_ratios[prefix + field] = _ratio(moved[key], totals[key])
    left, right = _partition(TAX_AMOUNTS, tax, row_ratios, currency_places, base_places)
    labels = {
        field: tax.get(field)
        for field in ("account_head", "description", "cost_center", "add_deduct_tax", "custom_pos_charge_kind")
        if tax.get(field) is not None
    }
    child_details = []
    for detail in details:
        key = detail["item"]
        parent_detail, child_detail = _partition(DETAIL_AMOUNTS, detail, {"*": ratios[key]},
                                                currency_places, base_places)
        if key in source["items"]:
            source["details"].append(dict(parent_detail, item=key, tax=index, rate=detail["rate"]))
        if key in child["items"]:
            child_details.append(dict(child_detail, item=key, tax=index, rate=detail["rate"]))
    child["details"].extend(child_details)

    # Where the native breakup accounts for the row total, use the sum of the
    # rounded shares even when some lines (or the entire row) are tax exempt.
    for prefix, places in (("", currency_places), ("base_", base_places)):
        for field, detail_field in TAX_DETAIL_FIELDS.items():
            key = prefix + field
            detail_key = prefix + detail_field
            total = sign * _units(totals[detail_key], places)
            if details and key in right and _units(tax.get(key), places) == total:
                moved_units = sign * sum(_units(detail.get(detail_key), places) for detail in child_details)
                left[key], right[key] = _amount(total - moved_units, places), _amount(moved_units, places)
    # Keep an explicit empty scope when the last taxable item moves away.
    if rule.get("charge_type") in _ITEM_RATE_CHARGES and details and "item_rates" not in rule:
        rule["item_rates"] = {detail["item"]: detail["rate"] for detail in details}
    source["taxes"].append({**labels, **rule, **left})
    child["taxes"].append({**labels, **rule, **right})


def _billing_key(snapshot, row):
    return (snapshot.get("billing_rows", {}).get(row.name)
            or row.get("custom_origin_invoice_item") or row.name)


def _allocated_tax_details(saved, taxes, source_rows):
    """Use exact transaction shares from a prior split when splitting again."""
    if not saved:
        return {}

    prior_taxes = [
        index for index, tax in enumerate(saved["taxes"]) if tax.get("custom_pos_charge_kind") != "Charged Tip"
    ]
    current_taxes = [
        id(tax) for tax in taxes if tax.get("custom_pos_charge_kind") != "Charged Tip"
    ]
    if len(prior_taxes) != len(current_taxes):
        return {}
    tax_map = dict(zip(prior_taxes, current_taxes, strict=True))
    row_map = {_billing_key(saved, row): row.name for row in source_rows.values()}
    details = {}
    for detail in saved["details"]:
        key = row_map.get(detail["item"])
        if key and detail["tax"] in tax_map and "base_amount" in detail:
            details.setdefault(tax_map[detail["tax"]], []).append(dict(detail, item=key))
    return details


def allocate_split_balances(check, move_map, currency_places, base_places):
    """Allocate in integer minor units and leave the exact residual on Check 1."""
    source = {"items": {}, "taxes": [], "details": [], "header": {}}
    child = {"items": {}, "taxes": [], "details": [], "header": {}}
    ratios_by_name = {}
    # Match ERPNext's stock ledger rounding on actual POS documents.
    stock_places = (frappe.get_precision("Stock Ledger Entry", "actual_qty")
                    if getattr(check, "doctype", None) == "POS Invoice" else None)
    local_total = base_total = local_move = base_move = Decimal(0)
    total_qty = moved_qty = Decimal(0)
    for row in check.get("items") or ():
        qty = _decimal(row.get("qty"))
        move = _decimal(move_map.get(row.name))
        ratio = ratios_by_name[row.name] = _ratio(move, qty)
        parent_values, child_values = _partition(
            LINE_AMOUNTS, row, {"*": ratio}, currency_places, base_places, stock_places=stock_places,
        )
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
    repricing = _repricing_split_discount(check)
    pending_details = check.get("_item_wise_tax_details") if repricing else None
    details_by_tax = {}
    if pending_details:
        # Freshly calculated tax documents may not have database names yet.
        for detail in pending_details:
            if detail.item.name in ratios_by_name:
                details_by_tax.setdefault(id(detail.tax), []).append(dict(detail, item_row=detail.item.name))
    else:
        for detail in check.get("item_wise_tax_details") or ():
            if detail.get("item_row") in ratios_by_name:
                details_by_tax.setdefault(detail.get("tax_row"), []).append(detail)
    taxes = check.get("taxes") or ()
    rows_by_name = {row.name: row for row in check.get("items") or ()}
    saved = get_saved_split_balance(check) if check.get("custom_split_balance") and not repricing else None
    saved_details = _allocated_tax_details(saved, taxes, rows_by_name)
    saved_rules = iter(
        (tax, rule) for tax, rule in zip(saved["taxes"], _saved_tax_pricing_rules(check, saved), strict=True)
        if tax.get("custom_pos_charge_kind") != "Charged Tip"
    ) if saved else iter(())
    exchange_rate = _decimal(check.get("conversion_rate")) or Decimal(1)
    prior_rows = {}
    net_amounts = {name: _net(row) for name, row in rows_by_name.items()}
    for index, tax in enumerate(taxes):
        prior_tax, prior_rule = {}, {}
        if tax.get("custom_pos_charge_kind") != "Charged Tip":
            prior_tax, prior_rule = next(saved_rules, ({}, {}))
        predecessor = prior_tax.get("previous_charge")
        if repricing:
            predecessor = tax.get("previous_charge")
        parent = index if predecessor is None else predecessor
        running = prior_rows[parent]["total"] if parent else net_amounts
        details = saved_details.get(id(tax))
        if details is None:
            prior = prior_rows.get(cint(tax.get("row_id")), {})
            basis = prior.get(_PREVIOUS_TAX.get(tax.get("charge_type")))
            details, unrounded = _tax_detail_allocations(
                tax, details_by_tax.get(id(tax) if pending_details else tax.get("name"), ()),
                rows_by_name, exchange_rate, currency_places, base_places, basis,
                fixed_fee_shares(check, tax) if repricing else None,
            )
        else:
            sign = -1 if tax.get("add_deduct_tax") == "Deduct" else 1
            unrounded = {detail["item"]: sign * _decimal(detail["amount"]) for detail in details}
        # An old row with no item-wise breakdown cannot provide a chained base.
        if running is not None:
            row_amount = tax.get("tax_amount_after_discount_amount")
            if row_amount is None:
                row_amount = tax.get("tax_amount")
            running = (
                {name: amount + unrounded.get(name, 0) for name, amount in running.items()}
                if unrounded or not _decimal(row_amount)
                else None
            )
        prior_rows[index + 1] = {"amount": unrounded or None, "total": running}
        rule = _tax_pricing_rule(tax)
        rates = (check.flags.ury_split_tax_scopes or {}).get(id(tax)) if repricing else None
        if tax.get("custom_pos_charge_kind") != "Charged Tip":
            if _matching_tax_account(tax, prior_tax):
                rule = prior_rule or rule
                rates = prior_tax.get("item_rates", rates)
        if rule.get("charge_type") in _ITEM_RATE_CHARGES and check.get("name"):
            # A split inherits the rounding boundary of the original charge.
            # Independent repricing establishes a new boundary on this check.
            if repricing:
                rule["rounding_source"] = frappe.generate_hash(length=16)
            else:
                rule["rounding_source"] = prior_tax.get("rounding_source") or f"{check.name}/{index + 1}"
        if predecessor is not None:
            rule["previous_charge"] = predecessor
        if rates is not None:
            rule["item_rates"] = {
                name: rates[key] for name, row in rows_by_name.items()
                if (key := _billing_key(saved, row) if saved else name) in rates
            }
        _partition_tax(tax, index, details, ratios_by_name, field_ratios, currency_places, base_places,
                       source, child, rule)

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
            for field, deducted in (("taxes_and_charges_added", False), ("taxes_and_charges_deducted", True)):
                key = prefix + field
                original_units = sum(_units(row.get(tax_amount), places) for row in check.get("taxes") or ()
                                     if (row.get("add_deduct_tax") == "Deduct") == deducted)
                if key in header and _units(check.get(key), places) == original_units:
                    header[key] = _amount(sum(_units(row.get(tax_amount), places) for row in tax_rows
                                              if (row.get("add_deduct_tax") == "Deduct") == deducted), places)
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
    # A kitchen origin may now represent several separately priced lines.
    origin_counts = Counter(row.get("custom_origin_invoice_item") or row.name for row in doc.items)
    bindings = []
    origins = {}
    for row, key in zip(doc.items, row_names, strict=True):
        origin = row.get("custom_origin_invoice_item") or row.name
        if origin_counts[origin] > 1:
            if not row.name:
                row.set_new_name()
            origin = row.name
            bindings.append((row, origin))
        origins[key] = origin
    doc.flags.ury_split_billing_rows = bindings
    if bindings:
        snapshot["billing_rows"] = {row.name: key for row, key in bindings}
    snapshot["items"] = {
        origins[key]: dict(snapshot["items"][key],
            qty=row.qty, item_code=row.get("item_code"), conversion_factor=row.get("conversion_factor"))
        for row, key in zip(doc.items, row_names, strict=True)
        if key in snapshot["items"]
    }
    for tax in snapshot["taxes"]:
        if "item_rates" in tax:
            tax["item_rates"] = {origins[key]: rate for key, rate in tax["item_rates"].items() if key in origins}
    snapshot["details"] = [
        dict(detail, item=origins[detail["item"]]) for detail in snapshot["details"] if detail["item"] in origins
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
        # Insert can rename children of a newly split check. Record their
        # persisted names before the server serializes the allocation.
        if bindings := getattr(flags, "ury_split_billing_rows", None):
            working["billing_rows"] = {row.name: key for row, key in bindings}
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


def _repricing_split_discount(doc):
    return getattr(getattr(doc, "flags", None), "ury_split_discount_reprice", None) is _DISCOUNT_REPRICE


def fixed_fee_shares(check, tax):
    """Use saved shares while the native charge still has its allocated amount."""
    allocation = (check.flags.ury_split_fixed_fees or {}).get(id(tax))
    if allocation and allocation[0] == flt(tax.get("tax_amount")):
        return allocation[1]
    return None


def _restore_tax_pricing(check, snapshot):
    if not snapshot["taxes"]:
        return
    live = list(check.get("taxes") or ())
    tax_rows = []
    scopes = {}
    fixed_fees = {}
    fee_amounts = {}
    for detail in snapshot["details"]:
        amounts = fee_amounts.setdefault(detail["tax"], {})
        key = detail["item"]
        amounts[key] = amounts.get(key, Decimal(0)) + _decimal(detail["amount"])
    row_names = {_billing_key(snapshot, row): row.name for row in check.items}
    # Native Grand Total comps can leave a signed net line offset against a
    # fixed fee. Carry its original transaction and company amounts together.
    comp_nets = {}
    for key, row, values in _allocated_rows(check, snapshot):
        if (row.get("custom_is_complimentary") and not row.get("rate")
                and (check.flags.ury_converting_grand_total_comp or not values.get("amount"))
                and (values.get("net_amount") or values.get("base_net_amount"))):
            comp_nets[row.name] = (flt(values.get("net_amount")), flt(values.get("base_net_amount")))
    check.flags.ury_split_comp_nets = comp_nets
    detail_rates = _item_rates_by_tax(snapshot)
    rules = _saved_tax_pricing_rules(check, snapshot)
    for index, (saved, rule) in enumerate(zip(snapshot["taxes"], rules, strict=True)):
        if saved.get("custom_pos_charge_kind") == "Charged Tip":
            continue
        native = live[index] if index < len(live) else None
        values = native.as_dict() if native and _matching_tax_account(native, saved) else {}
        rates = saved.get("item_rates", detail_rates.get(index))
        if rule.get("charge_type") in _ITEM_RATE_CHARGES and rates is not None:
            scopes[len(tax_rows)] = {row_names[k]: v for k, v in rates.items() if k in row_names}
        elif rule.get("charge_type") == "Actual" and index in fee_amounts:
            sign = -1 if saved.get("add_deduct_tax") == "Deduct" else 1
            shares = fee_amounts[index]
            total = _decimal(saved.get("tax_amount"))
            if sum(shares.values()) == sign * total:
                position = len(tax_rows)
                fixed_fees[position] = (float(total), {row_names[k]: float(sign * v) for k, v in shares.items()})
                scopes[position] = {row_names[k]: v for k, v in detail_rates[index].items()}
        tax_rows.append({
            **values, **saved,
            "charge_type": "Actual", "row_id": None, "included_in_print_rate": 0, "dont_recompute_tax": 0,
            **rule,
        })
    check.set("taxes", tax_rows + [row for row in live if row.get("custom_pos_charge_kind") == "Charged Tip"])
    check.flags.ury_split_tax_scopes = {id(check.taxes[index]): rates for index, rates in scopes.items()}
    check.flags.ury_split_fixed_fees = {id(check.taxes[i]): allocation for i, allocation in fixed_fees.items()}
    # The restored rows have new object identities. Native pricing rebuilds the per-item details.
    check._item_wise_tax_details = []


@contextmanager
def reprice_split_discount(checks, *, precisions=None):
    """Refresh the monetary allocation after an explicit server-side discount edit."""
    active = [
        (doc, snapshot) for doc in checks
        if doc is not None and isinstance(doc.get("custom_split_balance"), str)
        and (snapshot := get_saved_split_balance(doc))
    ]
    try:
        for doc, snapshot in active:
            doc.flags.ury_split_discount_reprice = _DISCOUNT_REPRICE
            _restore_tax_pricing(doc, snapshot)
        yield [doc for doc, _ in active]
        if precisions is not None:
            for doc, _ in active:
                snapshot, _ = allocate_split_balances(doc, {}, *precisions(doc, doc.get("company")))
                set_split_balance(doc, snapshot, [row.name for row in doc.items])
    finally:
        for doc, _ in active:
            doc.flags.ury_split_discount_reprice = None
            doc.flags.ury_split_tax_scopes = None
            doc.flags.ury_split_fixed_fees = None
            doc.flags.ury_split_comp_nets = None


def _item_rates_by_tax(snapshot):
    rates = {}
    for detail in snapshot["details"]:
        rates.setdefault(detail["tax"], {})[detail["item"]] = detail["rate"]
    return rates


def _original_fee_details(index, tax, items, applicable, currency_places, base_places):
    """Recover missing Actual breakup on its original check, before other items join."""
    if not applicable:
        return []
    weights = _fixed_tax_weights(applicable, items, 1)
    sign = -1 if tax.get("add_deduct_tax") == "Deduct" else 1
    running = Decimal(0)
    previous = {"amount": 0, "base_amount": 0}
    details = []
    for key, weight in zip(applicable, weights, strict=True):
        running += weight
        detail = {"item": key, "tax": index, "rate": flt(tax.get("rate"))}
        for prefix, places in (("", currency_places), ("base_", base_places)):
            field = prefix + "amount"
            total = sign * _units(tax.get(prefix + "tax_amount"), places)
            cumulative = int((running * total).to_integral_value(rounding=ROUND_HALF_UP))
            detail[field] = _amount(cumulative - previous[field], places)
            previous[field] = cumulative
            detail[prefix + "taxable_amount"] = float(sign * _net(items[key], base=bool(prefix)))
        details.append(detail)
    return details


def _consolidation_keys(checks, snapshots):
    """Pick one representative child name per interchangeable billable line."""
    representatives, keys, units = {}, {}, {}
    fields = (
        "uom", "warehouse", "cost_center",
        "rate", "price_list_rate", "base_price_list_rate", "discount_percentage", "discount_amount",
        "margin_type", "margin_rate_or_amount", "ignore_pricing_rule", "pricing_rules", "is_free_item",
        "custom_is_comp", "custom_is_complimentary", "custom_comp_reason", "custom_comp_type",
        "custom_comp_approver", "custom_price_overridden", "custom_price_override_reason",
        "custom_price_override_approver", "custom_original_price_list_rate", "custom_is_open_item",
        "custom_open_item_reference", "item_tax_template", "item_tax_rate", "custom_efris_tax_category",
        "custom_tax_exempt", "custom_parent_line_key", "custom_line_role", "custom_is_modifier",
        "custom_modifier_prefix", "custom_modifier_group", "custom_modifier_option",
        "custom_modifiers_json", "custom_bundle_overrides_json", "custom_inherit_parent_routing",
        "custom_hide_unpriced_on_receipt", "custom_seat_no", "custom_course", "comment",
        "ury_held", "fire_at", "fire_confirmation_pending",
    )
    for doc, snapshot in zip(checks, snapshots, strict=True):
        taxes = snapshot["taxes"] if snapshot else doc.get("taxes") or ()
        rates = _item_rates_by_tax(snapshot) if snapshot else {}
        rules = list(_saved_tax_pricing_rules(doc, snapshot)) if snapshot else taxes
        fixed_rows = [i for i, rule in enumerate(rules) if rule.get("charge_type") == "Actual"]
        fee_amounts = {}
        for detail in snapshot["details"] if snapshot else ():
            if detail["tax"] in fixed_rows:
                amounts = fee_amounts.setdefault(detail["tax"], {})
                key = detail["item"]
                txn, base = amounts.get(key, (0, 0))
                amounts[key] = (txn + _decimal(detail["amount"]),
                                base + _decimal(detail.get("base_amount", detail["amount"])))
        pricing = {}
        for row in doc.items:
            quantity = _decimal(row.qty)
            key = _billing_key(snapshot, row) if snapshot else row.name
            # Where no breakup survives, retain a distinct source component.
            # Otherwise equal quantities are interchangeable only at the same
            # signed unit fee in both transaction and company currencies.
            fees = tuple(
                tuple(amount / quantity for amount in fee_amounts[i].get(key, (Decimal(0), Decimal(0))))
                if i in fee_amounts and quantity else (doc.name, i)
                for i in fixed_rows
            )
            pricing[row.name] = (tuple(row.get(field) for field in fields), fees)
        children = {}
        parent_pricing = {}
        for row in doc.items:
            if parent := row.get("custom_parent_line_key"):
                children.setdefault(parent, []).append(row)
            else:
                parent_pricing[row.get("reservation_line_key") or row.name] = pricing[row.name]
        chain = tuple(tuple(tax.get(field) for field in (
            "account_head", "description", "cost_center", "custom_pos_charge_kind", "add_deduct_tax",
            "previous_charge", *_TAX_PRICING_FIELDS,
        )) for tax in taxes)
        rounding_sources = tuple(
            tax.get("rounding_source") or f"{doc.name}/{index + 1}"
            for index, (tax, rule) in enumerate(zip(taxes, rules, strict=True))
            if rule.get("charge_type") in _ITEM_RATE_CHARGES
        )
        for row in doc.items:
            origin = row.get("custom_origin_invoice_item") or row.name
            unit = row.get("item_code"), _decimal(row.get("conversion_factor"))
            if units.setdefault(origin, unit) != unit:
                frappe.throw(coded_message(
                    "SPLIT-ALLOCATION-LOCKED", _("This split check contains conflicting item quantities."),
                ))
            key = _billing_key(snapshot, row) if snapshot else row.name
            scope = tuple((tax.get("item_rates") or rates.get(i, {})).get(key)
                          for i, tax in enumerate(taxes))
            quantity = _decimal(row.qty)
            modifiers = tuple(
                (child.item_code, pricing[child.name], _decimal(child.qty) / quantity if quantity else child.qty)
                for child in children.get(row.get("reservation_line_key") or row.name, ())
            )
            signature = (origin, pricing[row.name], parent_pricing.get(row.get("custom_parent_line_key")),
                         modifiers, chain, scope, rounding_sources)
            representative = representatives.setdefault(signature, row.name)
            # Existing destination rows already have independent allocations.
            keys[row.name] = row.name if doc is checks[0] else representative
    return keys


def combine_split_balances(checks, currency_places, base_places):
    """Sum the saved charges, including the item and tax identities on each check."""
    saved = [get_saved_split_balance(doc) if doc.get("custom_split_balance") else None for doc in checks]
    line_keys = _consolidation_keys(checks, saved)
    if not any(saved):
        return None, line_keys

    destination = checks[0]
    combined = {"items": {}, "taxes": [], "details": [], "header": {}}
    taxes_by_chain, details_by_row = {}, {}

    def add_amounts(merged, values, fields):
        for field in fields:
            if field in values:
                merged[field] = float(_decimal(merged.get(field)) + _decimal(values[field]))

    for doc, snapshot in zip(checks, saved, strict=True):
        if (doc.get("currency"), _decimal(doc.get("conversion_rate")) or Decimal(1)) != (
            destination.get("currency"), _decimal(destination.get("conversion_rate")) or Decimal(1),
        ):
            frappe.throw(coded_message(
                "SPLIT-ALLOCATION-CURRENCY", _("Split checks must have matching currencies and exchange rates."),
            ))

        has_saved = snapshot is not None
        if has_saved:
            rows = _allocated_rows(doc, snapshot)
        else:
            snapshot, _ = allocate_split_balances(doc, {}, currency_places, base_places)
            rows = ((row.name, row, snapshot["items"][row.name])
                    for row in doc.items if row.name in snapshot["items"])
        merged_keys = {}
        for key, row, values in rows:
            merged_keys[key] = line_keys[row.name]
            add_amounts(combined["items"].setdefault(line_keys[row.name], {}), values, LINE_AMOUNTS)
        add_amounts(combined["header"], snapshot["header"], HEADER_AMOUNTS)

        detail_rates = _item_rates_by_tax(snapshot)
        details = list(snapshot["details"])
        tax_indexes = {}
        rules = _saved_tax_pricing_rules(doc, snapshot)
        for index, (tax, rule) in enumerate(zip(snapshot["taxes"], rules, strict=True)):
            labels = {key: val for key, val in tax.items() if key not in TAX_AMOUNTS and key != "item_rates"}
            labels.update(rule)
            if rule.get("charge_type") in _ITEM_RATE_CHARGES:
                labels.setdefault("rounding_source", f"{doc.name}/{index + 1}")
            # The preceding row identifies the complete ordered prefix, even
            # for an Actual fee that a later cumulative charge references.
            predecessor = tax.get("previous_charge", index)
            labels["previous_charge"] = tax_indexes[predecessor - 1] + 1 if predecessor else 0
            previous_index = cint(labels.get("row_id")) - 1
            if labels.get("charge_type") in _PREVIOUS_TAX and previous_index in tax_indexes:
                labels["row_id"] = tax_indexes[previous_index] + 1
            identity = tuple(sorted(labels.items()))
            if identity not in taxes_by_chain:
                taxes_by_chain[identity] = len(combined["taxes"])
                combined["taxes"].append(labels)
            target_index = tax_indexes[index] = taxes_by_chain[identity]
            target = combined["taxes"][target_index]
            add_amounts(target, tax, TAX_AMOUNTS)
            if rule.get("charge_type") == "Actual" and index not in detail_rates:
                applicable = []
                for row in doc.items:
                    key = _billing_key(snapshot, row) if has_saved else row.name
                    rates = json.loads(row.get("item_tax_rate") or "{}")
                    if key in snapshot["items"] and rates.get(tax.get("account_head")) != "N/A":
                        applicable.append(key)
                details.extend(_original_fee_details(
                    index, tax, snapshot["items"], applicable, currency_places, base_places,
                ))
            if rule.get("charge_type") in _ITEM_RATE_CHARGES:
                rates = tax.get("item_rates", detail_rates.get(index))
                if rates is None:
                    # Older snapshots without a breakup still belong to their original check.
                    rates = dict.fromkeys(snapshot["items"], rule["rate"])
                target.setdefault("item_rates", {}).update({
                    merged_keys[key]: rate for key, rate in rates.items()
                    if key in snapshot["items"]
                })
        for detail in details:
            key = merged_keys[detail["item"]]
            index = tax_indexes[detail["tax"]]
            identity = (key, index, detail["rate"])
            if identity not in details_by_row:
                details_by_row[identity] = {"item": key, "tax": index, "rate": detail["rate"]}
                combined["details"].append(details_by_row[identity])
            add_amounts(details_by_row[identity], {
                **detail,
                "base_amount": detail.get("base_amount", detail.get("amount")),
                "base_taxable_amount": detail.get("base_taxable_amount", detail.get("taxable_amount")),
            }, DETAIL_AMOUNTS)
    return combined, line_keys


def _allocated_rows(doc, snapshot):
    expected = snapshot["items"]
    seen = set()
    for row in doc.get("items") or ():
        key = _billing_key(snapshot, row)
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
    if _repricing_split_discount(doc):
        restore_split_stock(doc, snapshot)
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
        doc._item_wise_tax_details = [
            frappe._dict(
                item=allocated_rows[detail["item"]],
                tax=tax_rows_by_index[detail["tax"]],
                rate=detail["rate"],
                amount=detail.get("base_amount", detail.get("amount")),
                taxable_amount=detail.get("base_taxable_amount", detail.get("taxable_amount")),
            )
            for detail in snapshot["details"]
            if detail["tax"] in tax_rows_by_index
        ]
        doc.update_item_wise_tax_details = True
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
