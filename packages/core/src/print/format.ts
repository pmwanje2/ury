export const MERGED_POS_INVOICE_PRINT_FORMAT = 'Merged POS Invoice Format';

export const UNSAFE_POS_INVOICE_PRINT_FORMATS: ReadonlySet<string> = new Set([
  'POS Invoice Classic',
  'POS Invoice Bordered',
  'POS Invoice Modern',
  'POS Invoice Modern with Images',
  'POS Invoice',
  'Return POS Invoice',
  'EFRIS POS Receipt 80mm',
  'POS EFRIS Invoice',
]);

export function resolveSafePosPrintFormat(
  defaultFormat: string | null | undefined,
  isMerged = false
): string {
  if (isMerged) {
    return MERGED_POS_INVOICE_PRINT_FORMAT;
  }
  const candidate = (defaultFormat || '').trim();
  if (!candidate || UNSAFE_POS_INVOICE_PRINT_FORMATS.has(candidate)) {
    return MERGED_POS_INVOICE_PRINT_FORMAT;
  }
  return candidate;
}

export function buildLetterheadParams(letterHead?: string | null): {
  no_letterhead: 0 | 1;
  letterhead: string;
  queryString: string;
} {
  const configured = (letterHead || '').trim();
  if (configured) {
    return {
      no_letterhead: 0,
      letterhead: configured,
      queryString: `&no_letterhead=0&letterhead=${encodeURIComponent(configured)}`,
    };
  }
  return {
    no_letterhead: 1,
    letterhead: 'No Letterhead',
    queryString: '&no_letterhead=1&letterhead=No%20Letterhead',
  };
}
