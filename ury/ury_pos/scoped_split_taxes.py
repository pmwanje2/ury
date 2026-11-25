"""Native ERPNext repricing with the original per-line VAT rates of a merged check."""

import json

from erpnext.controllers.taxes_and_totals import calculate_taxes_and_totals
from erpnext.stock.get_item_details import NOT_APPLICABLE_TAX


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
            origin = item.get("custom_origin_invoice_item") or item.name
            for tax in self.doc.taxes:
                if id(tax) in scopes:
                    rates[self.scope_key(tax)] = scopes[id(tax)].get(origin, NOT_APPLICABLE_TAX)
            item.item_tax_rate = json.dumps(rates)

    @staticmethod
    def scope_key(tax):
        return f"\0split-tax-{id(tax)}"

    def _get_tax_rate(self, tax, item_tax_map):
        key = self.scope_key(tax)
        if key in item_tax_map:
            return super()._get_tax_rate(tax, {tax.account_head: item_tax_map[key]})
        return super()._get_tax_rate(tax, item_tax_map)

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
