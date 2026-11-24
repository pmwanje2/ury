export interface ServiceChargeRule {
  order_type: string;
  enabled?: number;
  charge_mode: 'Percentage' | 'Fixed Amount' | string;
  rate?: number;
  fixed_amount?: number;
  min_amount_floor?: number;
  min_pax?: number;
  calculation_basis?: 'Post-Discount Subtotal' | 'Pre-Discount Subtotal' | 'Fixed Amount' | string;
  taxable?: number;
  account_head?: string;
  description?: string;
  efris_item_code?: string;
}

export interface ServiceChargeProfileLike {
  custom_service_charge_rules?: ServiceChargeRule[] | null;
  custom_service_charge_rate?: number | null;
  custom_service_charge_account?: string | null;
  custom_service_charge_min_pax?: number | null;
  custom_service_charge_order_types?: string | null;
  custom_service_charge_taxable?: number | null;
  custom_service_charge_description?: string | null;
}

export interface InvoiceTaxRowLike {
  description: string;
  rate: number;
  tax_amount?: number;
  tax_rate?: number;
  charge_type?: string;
  custom_pos_charge_kind?: string;
  account_head?: string;
}

const precisionCache = new Map<string, number>();

/** ISO 4217 minor-unit digits (UGX/JPY=0, USD/EUR=2, BHD/OMR/KWD=3). */
export function currencyPrecision(code?: string | null): number {
  const normalized = (code || '').trim().toUpperCase();
  if (!normalized) return 2;
  const cached = precisionCache.get(normalized);
  if (cached !== undefined) return cached;
  let digits = 2;
  try {
    digits =
      new Intl.NumberFormat('en', { style: 'currency', currency: normalized }).resolvedOptions()
        .maximumFractionDigits ?? 2;
  } catch {
    digits = 2;
  }
  precisionCache.set(normalized, digits);
  return digits;
}

export function currencyInputStep(code?: string | null): string {
  const digits = currencyPrecision(code);
  if (digits <= 0) return '1';
  return (1 / 10 ** digits).toFixed(digits);
}

export function roundCurrency(amount: number, code?: string | null): number {
  const digits = currencyPrecision(code);
  const factor = 10 ** digits;
  const rounded = Math.round((amount + Math.sign(amount) * 1e-12) * factor) / factor;
  return Object.is(rounded, -0) ? 0 : rounded;
}

/** Normalize enabled Service Charge rules from POS Profile with legacy scalar fallback. */
export function resolveServiceChargeRules(profile?: ServiceChargeProfileLike | null): ServiceChargeRule[] {
  if (!profile) return [];
  const raw = profile.custom_service_charge_rules;
  if (Array.isArray(raw) && raw.length > 0) {
    return raw
      .filter((r) => (r.enabled === undefined ? true : Number(r.enabled) === 1))
      .filter((r) => {
        const mode = (r.charge_mode || 'Percentage').trim();
        if (mode === 'Fixed Amount') return Number(r.fixed_amount || 0) > 0;
        return Number(r.rate || 0) > 0;
      })
      .map((r) => ({
        order_type: (r.order_type || '').trim(),
        enabled: 1,
        charge_mode: (r.charge_mode || 'Percentage').trim(),
        rate: Number(r.rate || 0),
        fixed_amount: Number(r.fixed_amount || 0),
        min_amount_floor: Math.max(0, Number(r.min_amount_floor || 0)),
        min_pax: Math.max(0, Math.trunc(Number(r.min_pax || 0))),
        calculation_basis:
          (r.charge_mode || 'Percentage').trim() === 'Fixed Amount'
            ? 'Fixed Amount'
            : (r.calculation_basis || 'Post-Discount Subtotal').trim(),
        taxable: Number(r.taxable || 0) === 1 ? 1 : 0,
        account_head: (r.account_head || '').trim(),
        description: (r.description || '').trim() || 'Service Charge',
        efris_item_code: (r.efris_item_code || '').trim(),
      }))
      .filter((r) => Boolean(r.order_type));
  }

  const legacyRate = Number(profile.custom_service_charge_rate || 0);
  const legacyAcct = (profile.custom_service_charge_account || '').trim();
  if (legacyRate <= 0 || !legacyAcct) return [];

  const rawTypes = (profile.custom_service_charge_order_types || 'Dine In').trim();
  const types = rawTypes
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
  const orderTypes = types.length > 0 ? types : ['Dine In'];
  const minPax = Math.max(0, Math.trunc(Number(profile.custom_service_charge_min_pax || 0)));
  const taxable = Number(profile.custom_service_charge_taxable || 0) === 1 ? 1 : 0;
  const desc = (profile.custom_service_charge_description || '').trim() || 'Service Charge';

  return orderTypes.map((ot) => ({
    order_type: ot,
    enabled: 1,
    charge_mode: 'Percentage',
    rate: legacyRate,
    fixed_amount: 0,
    min_amount_floor: 0,
    min_pax: minPax,
    calculation_basis: 'Post-Discount Subtotal',
    taxable,
    account_head: legacyAcct,
    description: desc,
    efris_item_code: '',
  }));
}

/** Mirror server precedence: exact order_type > '*' wildcard, highest qualifying min_pax wins. */
export function resolveActiveServiceChargeRule(
  profile: ServiceChargeProfileLike | null | undefined,
  orderType: string | null | undefined,
  pax: number | null | undefined,
): ServiceChargeRule | null {
  const rules = resolveServiceChargeRules(profile);
  if (!rules.length) return null;
  const ot = (orderType || '').trim().toLowerCase();
  const covers = Math.max(0, Math.trunc(Number(pax || 0)));

  const exact: ServiceChargeRule[] = [];
  const wildcard: ServiceChargeRule[] = [];
  for (const rule of rules) {
    if (covers < (rule.min_pax ?? 0)) continue;
    const ruleOt = rule.order_type.trim().toLowerCase();
    if (ot && ruleOt === ot) {
      exact.push(rule);
    } else if (ruleOt === '*') {
      wildcard.push(rule);
    }
  }
  const pool = exact.length > 0 ? exact : wildcard;
  if (!pool.length) return null;
  pool.sort((a, b) => (b.min_pax ?? 0) - (a.min_pax ?? 0));
  return pool[0];
}

export function estimateServiceCharge(
  subtotal: number,
  rule: ServiceChargeRule | null | undefined,
  currencyCode?: string | null,
): number {
  if (!rule || subtotal <= 0) return 0;
  if (rule.charge_mode === 'Fixed Amount') {
    return roundCurrency(Number(rule.fixed_amount || 0), currencyCode);
  }
  const computed = roundCurrency((subtotal * Number(rule.rate || 0)) / 100, currencyCode);
  const floor = roundCurrency(Number(rule.min_amount_floor || 0), currencyCode);
  if (floor > 0 && computed < floor) {
    return floor;
  }
  return computed;
}

export function classifyInvoiceTaxes<T extends InvoiceTaxRowLike>(taxes: T[]) {
  const serviceChargeRows: T[] = [];
  const taxRows: T[] = [];
  const tipRows: T[] = [];

  for (const row of taxes || []) {
    const kind = (row.custom_pos_charge_kind || '').trim();
    const text = `${row.description || ''} ${row.account_head || ''}`.toLowerCase();
    if (kind === 'Service Charge' || (!kind && text.includes('service charge'))) {
      serviceChargeRows.push(row);
    } else if (kind === 'Charged Tip') {
      tipRows.push(row);
    } else {
      // Includes both standard VAT/GST rows and 'Service Charge Tax' rows
      taxRows.push(row);
    }
  }
  return { serviceChargeRows, taxRows, tipRows };
}
