"""Native ERPNext repricing with the original per-line VAT rates of a merged check."""

import json

from erpnext.controllers.taxes_and_totals import calculate_taxes_and_totals
from erpnext.stock.get_item_details import NOT_APPLICABLE_TAX

from ury.ury_pos.split_balances import fixed_fee_shares


class ScopedSplitTaxes(calculate_taxes_and_totals):
    def update_item_tax_map(self):
        super().update_item_tax_map()
        # Native maps are keyed by posting account. Distinct VAT rows can share
        # that account after Add Check. Give those rows a private key during
        # calculation, including native inclusive-tax rounding and discount passes.
        scopes = self.doc.flags.ury_split_tax_scopes
        self.native_item_maps = [(item, item.item_tax_rate) for item in self.doc.items]
        for item, native in self.native_item_maps:
            rates = json.loads(native)
            for tax in self.doc.taxes:
                if id(tax) in scopes:
                    rates[self.scope_key(tax)] = scopes[id(tax)].get(item.name, NOT_APPLICABLE_TAX)
            item.item_tax_rate = json.dumps(rates)

    @staticmethod
    def scope_key(tax):
        return f"\0split-tax-{id(tax)}"

    def _get_tax_rate(self, tax, item_tax_map):
        key = self.scope_key(tax)
        if key in item_tax_map:
            return super()._get_tax_rate(tax, {tax.account_head: item_tax_map[key]})
        return super()._get_tax_rate(tax, item_tax_map)

    def calculate_net_total(self):
        # Apply the original comp credit on every native pricing pass, before
        # dependent VAT and totals are calculated.
        for item in self._items:
            amounts = (self.doc.flags.ury_split_comp_nets or {}).get(item.name)
            if amounts:
                item.net_amount, item.base_net_amount = amounts
                item.net_rate = item.net_amount / item.qty
                item.base_net_rate = item.base_net_amount / item.qty
                item.distributed_discount_amount = item.amount - item.net_amount
        super().calculate_net_total()

    def set_discount_amount(self):
        net = self.doc.net_total
        if self.doc.apply_discount_on == "Net Total":
            self.doc.net_total -= self._comp_net_total()
        try:
            super().set_discount_amount()
        finally:
            self.doc.net_total = net

    def _comp_net_total(self):
        return sum(net for net, _ in (self.doc.flags.ury_split_comp_nets or {}).values())

    def get_total_for_discount_amount(self):
        return super().get_total_for_discount_amount() - self._comp_net_total()

    def apply_discount_amount(self):
        # Exclude already-comped credits from the per-item discount weights
        # and the native running rounding remainder. The next tax pass sees
        # the original credits again in calculate_net_total.
        credits = self.doc.flags.ury_split_comp_nets or {}
        if not credits or not self.doc.discount_amount:
            return super().apply_discount_amount()
        comped = [item for item in self._items if item.name in credits]
        for item in comped:
            item.net_amount = 0
            item._unrounded_net_amount = None
        try:
            super().apply_discount_amount()
        finally:
            for item in comped:
                item.net_amount = credits[item.name][0]

    def get_current_tax_and_net_amount(self, item, tax, item_tax_map):
        # Native ERPNext accumulates the item total in display order. At a
        # branch in the merged charge chain, finish the previous row against
        # its own predecessor before any dependent tax reads that total.
        if tax.idx > 1:
            previous = self.doc.taxes[tax.idx - 2]
            parent = previous.get("previous_charge")
            if parent is not None and parent != previous.idx - 1:
                base = self.doc.taxes[parent - 1].grand_total_for_current_item if parent else item.net_amount
                charge = self.get_tax_amount_if_for_valuation_or_deduction(previous.tax_amount_for_current_item, previous)
                previous.grand_total_for_current_item = base + charge
        shares = fixed_fee_shares(self.doc, tax)
        if shares is None:
            return super().get_current_tax_and_net_amount(item, tax, item_tax_map)
        if item.name not in shares:
            return 0, 0
        fee = shares[item.name]
        if not tax.get("dont_recompute_tax"):
            self.set_item_wise_tax(item, tax, self._get_tax_rate(tax, item_tax_map), fee, item.net_amount)
        return item.net_amount, fee

    def calculate(self, *args, **kwargs):
        try:
            return super().calculate(*args, **kwargs)
        finally:
            for item, native in getattr(self, "native_item_maps", ()):
                item.item_tax_rate = native


def calculate_scoped_split_taxes(doc):
    ScopedSplitTaxes(doc)
    # POS Invoice's native AccountsController runs these after tax calculation.
    doc.calculate_commission()
    doc.calculate_contribution()
