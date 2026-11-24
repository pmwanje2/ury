export interface TaxRowLike {
  description: string;
  rate: number;
  header_rate?: number;
  effective_rate?: number | null;
  item_rate_override?: boolean;
  tax_amount: number;
  account_head?: string;
}

export function formatPreviewTaxLabel(
  taxRow: TaxRowLike,
  netTotal: number | undefined,
  fallbackLabel: string,
): string {
  const raw = (taxRow.description || '').trim();
  const baseFromAccount = (taxRow.account_head || '')
    .replace(/\s+-\s+[A-Za-z0-9]+$/, '')
    .trim();
  const stripTrailingPct = (s: string) => s.replace(/\s*\(\s*\d+(?:\.\d+)?%\s*\)\s*$/, '').trim();
  const fmtPct = (r: number) => {
    const rounded = Math.round(r * 100) / 100;
    return `${rounded}%`;
  };

  if (taxRow.item_rate_override) {
    const labelBase = baseFromAccount || stripTrailingPct(raw) || fallbackLabel;
    if (taxRow.effective_rate != null && Number.isFinite(taxRow.effective_rate)) {
      return `${labelBase} (${fmtPct(taxRow.effective_rate)})`;
    }
    return labelBase;
  }

  if (raw && netTotal && netTotal > 0 && Number.isFinite(taxRow.tax_amount)) {
    const match = raw.match(/\(\s*(\d+(?:\.\d+)?)%\s*\)/);
    if (match) {
      const stated = parseFloat(match[1]);
      const actual = (taxRow.tax_amount / netTotal) * 100;
      if (Number.isFinite(stated) && Math.abs(actual - stated) > 0.5) {
        return baseFromAccount || stripTrailingPct(raw) || fallbackLabel;
      }
    }
  }

  return raw || baseFromAccount || fallbackLabel;
}
