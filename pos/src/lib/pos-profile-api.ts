import type { CoversProfile } from '@ury/core/covers';
import { DOCTYPES } from '../data/doctypes';
import { call, db } from '@ury/core';
import type { DefaultCustomerEntry } from '@ury/core/default-customer';
import type { AvailableOutlet } from './outlet-api';
import type { ServiceChargeRule } from './service-charge';

export interface OfflinePaymentMode {
  mode_of_payment: string;
  type?: string;
  allow_offline: boolean;
  requires_reference: boolean;
}

export interface PaymentModeMeta {
  type: string;
  overtender_rule: string;
  halo_max_amount?: number;
}

// Limited fields response
export interface PosProfileLimited {
  pos_profile: string;
  branch: string;
  company: string;
  currency?: string | null;
  country?: string;
  waiter: string;
  warehouse: string;
  cashier: string;
  print_format: string | null;
  letter_head?: string | null;
  qz_print: number;
  qz_host: string | null;
  printer: string | null;
  print_type: string;
  tableAttention: number;
  paid_limit: number;
  disable_rounded_total: number;
  enable_discount: number;
  allow_discount_change?: number;
  multiple_cashier: number;
  owner: string;
  edit_order_type?: number;
  custom_invoice_warning_time?: number;
  enable_payment_without_printing?: number;
  custom_enable_payment_without_printing?: number;
  custom_disable_print_status_tracking?: number | boolean;
  custom_blind_cash_count?: number;
  /** Mode of Payment -> default account for the profile's company. */
  payment_mode_accounts?: Record<string, string | null>;
  /** Mode of Payment -> type and non-cash over-tender rule. */
  payment_mode_meta?: Record<string, PaymentModeMeta>;
  /** POS Profile toggle for cashier touchscreen Pickup / Loan / Paid Out. */
  enable_cash_movements?: number;
  require_over_short_reason?: number;
  over_short_reason_tolerance?: number;
  over_short_tolerance?: number;
  cash_pull_threshold?: number;
  paid_out_approval_threshold?: number | null;
  paid_in_approval_threshold?: number;
  paid_in_enabled?: boolean;
  cash_drawer_id?: string;
  floor_idle_signout_seconds?: number;
  floor_signout_after_send?: number;
  /** Whitelisted method the till calls before a Room Charge; "" keeps the free-text room box. */
  room_lookup_method?: string;
  /** The POS Profile payment row marked Default; the till prefills it with the total. */
  default_payment_mode?: string | null;
  /** Payment methods posting to the Company's Guest Ledger Clearing account (room charge). */
  room_charge_modes?: string[];
  /** True when the site/profile expects room charge but Company.custom_guest_ledger_clearing is unmapped. */
  room_charge_unconfigured?: boolean;
  /** Per-till maximum offline tender amount without online manager PIN approval (0 = no cap). */
  offline_tender_limit?: number;
  approval_currency?: string;
  approval_conversion_rate?: number | null;
  /** Mode of Payment offline eligibility flags from server configuration (never tender-name regex). */
  offline_payment_modes?: OfflinePaymentMode[];
  /** Seconds to wait for EFRIS FDN before printing pending receipt + queuing auto-reprint. */
  efris_print_wait_seconds?: number;
  tax_category?: string | null;
  order_type_tax_categories?: Record<string, string>;
  /** G36: optional clock-in enforcement flags. */
  require_clock_in_to_order?: number;
  block_clock_out_with_open_work?: number;
  /** G38: optional shift-close tip declaration enforcement flag. */
  require_tip_declaration?: number;
  /** G43: multi-outlet bootstrap selection state. */
  requires_outlet_selection?: boolean;
  available_outlets?: AvailableOutlet[];
  available_outlets_count?: number;
  /** G44: active shift business date & Shift X/Z report availability. */
  active_opening_entry?: string | null;
  active_business_date?: string | null;
  shift_report_available?: boolean;
  shift_z_print_format_available?: boolean;
  custom_drawer_pin?: number | string | null;
  custom_drawer_kick_hex?: string | null;
  custom_auto_kick_on_cash_tender?: number | boolean | null;
  drawer_opening_modes?: string[];
}

export interface PosProfileLimitedResponse {
  message: PosProfileLimited;
}

interface RolePermission {
  name: string;
  owner: string;
  creation: string;
  modified: string;
  modified_by: string;
  docstatus: number;
  idx: number;
  role: string;
  parent: string;
  parentfield: string;
  parenttype: string;
  doctype: string;
}

// Full POS Profile response
export interface PosProfileFull extends CoversProfile {
  ury_enable_hold_fire?: number;
  default_fire_delay_minutes?: number;
  fire_time_zone?: string;
  name: string;
  owner: string;
  creation: string;
  modified: string;
  modified_by: string;
  docstatus: number;
  idx: number;
  company: string;
  customer: string | null;
  allow_partial_payment?: number;
  allow_discount_change?: number;
  country: string;
  disabled: number;
  warehouse: string;
  campaign: string | null;
  company_address: string | null;
  letter_head?: string | null;
  restaurant: string;
  branch: string;
  currency: string;
  role_allowed_for_billing: RolePermission[];
  assigned_billing_user?: string | null;
  role_restricted_for_table_order?: RolePermission[];
  /** Roles that must not see grand/net total and expected amounts (POS Profile config). */
  role_masked_for_amounts?: RolePermission[];
  transfer_role_permissions?: RolePermission[];
  paid_limit?: number;
  /** Per-order-type default customer (POS Profile "Order Type Default Customers"), keyed by order type. */
  order_type_customer_map?: Record<string, DefaultCustomerEntry>;
  /** Profile-level default customer, or null. */
  default_customer?: DefaultCustomerEntry | null;
  enable_payment_without_printing?: number;
  custom_enable_payment_without_printing?: number;
  custom_blind_cash_count?: number;
  /** One configured void reason per line. Absent on sites without the field. */
  custom_workstation_layout?: string | null;
  custom_void_reasons?: string | null;
  /** Roles exempt from floor-tablet sign-out (POS Profile.custom_floor_stay_signed_in_roles). */
  custom_floor_stay_signed_in_roles?: Array<{ role: string }>;
  offline_tender_limit?: number;
  approval_currency?: string;
  approval_conversion_rate?: number | null;
  offline_payment_modes?: OfflinePaymentMode[];
  allow_rate_change?: number;
  custom_max_free_bill_prints?: number;
  custom_max_free_closed_copies?: number;
  custom_service_charge_rate?: number;
  custom_service_charge_account?: string | null;
  custom_service_charge_min_pax?: number;
  custom_service_charge_order_types?: string | null;
  custom_service_charge_taxable?: number;
  custom_service_charge_description?: string | null;
  custom_service_charge_rules?: ServiceChargeRule[];
  custom_tax_exempt_category?: string | null;
  custom_tips_payable_account?: string | null;
  efris_print_wait_seconds?: number;
  tax_category?: string | null;
  order_type_tax_categories?: Record<string, string>;
  require_clock_in_to_order?: number;
  block_clock_out_with_open_work?: number;
  require_tip_declaration?: number;
  active_opening_entry?: string | null;
  active_business_date?: string | null;
  shift_report_available?: boolean;
  shift_z_print_format_available?: boolean;
  custom_allow_fx_rate_override?: number;
  custom_max_fx_variance_pct?: number;
  custom_offline_peer_relay_url?: string;
  custom_offline_collision_policy?: string;
  custom_drawer_pin?: number | string | null;
  custom_drawer_kick_hex?: string | null;
  custom_auto_kick_on_cash_tender?: number | boolean | null;
  drawer_opening_modes?: string[];
}

// Combined POS Profile with both limited and full fields
export interface PosProfileCombined extends PosProfileFull {
  pos_profile?: string;
  ury_offline_capture?: number | string | boolean;
  // Add limited fields that don't exist in full profile
  waiter: string;
  cashier: string;
  print_format: string | null;
  letter_head?: string | null;
  qz_print: number;
  qz_host: string | null;
  printer: string | null;
  print_type: string;
  tableAttention: number;
  paid_limit: number;
  disable_rounded_total: number;
  enable_discount: number;
  allow_discount_change?: number;
  multiple_cashier: number;
  edit_order_type?: number;
  view_all_status?: number;
  custom_daily_pos_close?: number;
  custom_invoice_warning_time?: number;
  enable_payment_without_printing?: number;
  custom_enable_payment_without_printing?: number;
  custom_disable_print_status_tracking?: number | boolean;
  payment_mode_accounts?: Record<string, string | null>;
  payment_mode_meta?: Record<string, PaymentModeMeta>;
  enable_cash_movements?: number;
  require_over_short_reason?: number;
  over_short_reason_tolerance?: number;
  over_short_tolerance?: number;
  cash_pull_threshold?: number;
  paid_out_approval_threshold?: number | null;
  paid_in_approval_threshold?: number;
  paid_in_enabled?: boolean;
  cash_drawer_id?: string;
  floor_idle_signout_seconds?: number;
  floor_signout_after_send?: number;
  room_lookup_method?: string;
  default_payment_mode?: string;
  room_charge_modes?: string[];
  room_charge_unconfigured?: boolean;
  offline_tender_limit?: number;
  approval_currency?: string;
  approval_conversion_rate?: number | null;
  offline_payment_modes?: OfflinePaymentMode[];
  efris_print_wait_seconds?: number;
  require_clock_in_to_order?: number;
  block_clock_out_with_open_work?: number;
  require_tip_declaration?: number;
  available_outlets_count?: number;
  active_opening_entry?: string | null;
  active_business_date?: string | null;
  shift_report_available?: boolean;
  shift_z_print_format_available?: boolean;
}

export interface Currency {
  name: string;
  symbol: string;
  fraction: string;
  fraction_units: number;
  smallest_currency_fraction_value: number;
  number_format: string;
}

export interface PosProfileFullResponse {
  message: PosProfileFull;
}

export async function getPosProfileLimitedFields(): Promise<PosProfileLimited> {
  const res = await call.get('ury.ury_pos.api.getPosProfile');
  return res.message;
}

export async function getPosProfileFull(posProfileName: string): Promise<PosProfileFull> {
  // Cashier/Captain roles lack doctype-level read permission on POS Profile,
  // so a raw `/api/resource/POS Profile/<name>` fetch (db.getDoc) 403s for
  // them. Use the whitelisted server method instead, which reads the doc
  // server-side and returns only the fields the frontend needs.
  const res = await call.get('ury.ury_pos.api.getPosProfileFull', { pos_profile: posProfileName });
  return res.message;
}

export class OutletSelectionRequiredError extends Error {
  readonly requiresOutletSelection = true as const;
  readonly availableOutlets: AvailableOutlet[];

  constructor(availableOutlets: AvailableOutlet[]) {
    super('Outlet selection required');
    this.name = 'OutletSelectionRequiredError';
    this.availableOutlets = availableOutlets;
  }
}

export function isOutletSelectionRequiredError(
  error: unknown,
): error is OutletSelectionRequiredError {
  return (
    error instanceof OutletSelectionRequiredError ||
    (Boolean(error) &&
      typeof error === 'object' &&
      (error as { requiresOutletSelection?: unknown }).requiresOutletSelection === true)
  );
}

function combinePosProfiles(
  limitedProfile: PosProfileLimited,
  fullProfile: PosProfileFull,
): PosProfileCombined {
  const enablePaymentWithoutPrinting = Number(
    limitedProfile.enable_payment_without_printing ??
      (limitedProfile as any).custom_enable_payment_without_printing ??
      fullProfile.enable_payment_without_printing ??
      fullProfile.custom_enable_payment_without_printing ??
      0,
  );

  return {
    ...fullProfile,
    pos_profile: limitedProfile.pos_profile || fullProfile.name,
    currency: fullProfile.currency || limitedProfile.currency || '',
    country: fullProfile.country || limitedProfile.country || '',
    owner: limitedProfile.owner,
    waiter: limitedProfile.waiter,
    cashier: limitedProfile.cashier,
    print_format: limitedProfile.print_format,
    letter_head: limitedProfile.letter_head ?? fullProfile.letter_head ?? null,
    qz_print: limitedProfile.qz_print,
    qz_host: limitedProfile.qz_host,
    printer: limitedProfile.printer,
    print_type: limitedProfile.print_type,
    tableAttention: limitedProfile.tableAttention,
    paid_limit: limitedProfile.paid_limit,
    disable_rounded_total: limitedProfile.disable_rounded_total,
    enable_discount: limitedProfile.enable_discount,
    allow_discount_change: Number(
      fullProfile.allow_discount_change ?? limitedProfile.allow_discount_change ?? 0,
    ),
    multiple_cashier: limitedProfile.multiple_cashier,
    edit_order_type: limitedProfile.edit_order_type,
    custom_invoice_warning_time: limitedProfile.custom_invoice_warning_time ?? 0,
    enable_payment_without_printing: enablePaymentWithoutPrinting,
    custom_enable_payment_without_printing: enablePaymentWithoutPrinting,
    custom_disable_print_status_tracking:
      limitedProfile.custom_disable_print_status_tracking ?? 0,
    custom_blind_cash_count: limitedProfile.custom_blind_cash_count ?? 0,
    payment_mode_accounts: limitedProfile.payment_mode_accounts,
    payment_mode_meta: limitedProfile.payment_mode_meta,
    enable_cash_movements: Number(limitedProfile.enable_cash_movements ?? 0),
    require_over_short_reason: Number(limitedProfile.require_over_short_reason ?? 0),
    over_short_reason_tolerance: Number(limitedProfile.over_short_reason_tolerance ?? 0),
    over_short_tolerance: limitedProfile.over_short_tolerance,
    cash_pull_threshold: limitedProfile.cash_pull_threshold,
    paid_out_approval_threshold: limitedProfile.paid_out_approval_threshold,
    paid_in_approval_threshold: limitedProfile.paid_in_approval_threshold,
    paid_in_enabled: limitedProfile.paid_in_enabled,
    cash_drawer_id: limitedProfile.cash_drawer_id,
    floor_idle_signout_seconds: limitedProfile.floor_idle_signout_seconds ?? 0,
    floor_signout_after_send: limitedProfile.floor_signout_after_send ?? 0,
    room_lookup_method: limitedProfile.room_lookup_method ?? '',
    default_payment_mode: limitedProfile.default_payment_mode ?? '',
    room_charge_modes: limitedProfile.room_charge_modes ?? [],
    room_charge_unconfigured: Boolean(limitedProfile.room_charge_unconfigured),
    approval_currency: limitedProfile.approval_currency,
    approval_conversion_rate: limitedProfile.approval_conversion_rate,
    offline_tender_limit: Number(
      limitedProfile.offline_tender_limit ?? fullProfile.offline_tender_limit ?? 0,
    ),
    offline_payment_modes:
      limitedProfile.offline_payment_modes ?? fullProfile.offline_payment_modes ?? [],
    efris_print_wait_seconds:
      limitedProfile.efris_print_wait_seconds ?? fullProfile.efris_print_wait_seconds ?? 5,
    tax_category: fullProfile.tax_category ?? limitedProfile.tax_category ?? null,
    order_type_tax_categories:
      fullProfile.order_type_tax_categories ?? limitedProfile.order_type_tax_categories ?? {},
    require_clock_in_to_order: Number(
      limitedProfile.require_clock_in_to_order ?? fullProfile.require_clock_in_to_order ?? 0,
    ),
    block_clock_out_with_open_work: Number(
      limitedProfile.block_clock_out_with_open_work ??
        fullProfile.block_clock_out_with_open_work ??
        1,
    ),
    require_tip_declaration: Number(
      limitedProfile.require_tip_declaration ?? fullProfile.require_tip_declaration ?? 0,
    ),
    available_outlets_count: limitedProfile.available_outlets_count,
    active_opening_entry:
      limitedProfile.active_opening_entry ?? fullProfile.active_opening_entry ?? null,
    active_business_date:
      limitedProfile.active_business_date ?? fullProfile.active_business_date ?? null,
    shift_report_available: Boolean(
      limitedProfile.shift_report_available ?? fullProfile.shift_report_available ?? false,
    ),
    shift_z_print_format_available: Boolean(
      limitedProfile.shift_z_print_format_available ??
        fullProfile.shift_z_print_format_available ??
        false,
    ),
    custom_drawer_pin: limitedProfile.custom_drawer_pin ?? fullProfile.custom_drawer_pin ?? 2,
    custom_drawer_kick_hex:
      limitedProfile.custom_drawer_kick_hex ?? fullProfile.custom_drawer_kick_hex ?? '',
    custom_auto_kick_on_cash_tender: Number(
      limitedProfile.custom_auto_kick_on_cash_tender ??
        fullProfile.custom_auto_kick_on_cash_tender ??
        1,
    ),
    drawer_opening_modes:
      limitedProfile.drawer_opening_modes ?? fullProfile.drawer_opening_modes ?? [],
  };
}

export async function getCombinedPosProfile(): Promise<PosProfileCombined> {
  // Get limited fields first
  const limitedProfile = await getPosProfileLimitedFields();
  if (limitedProfile?.requires_outlet_selection || !limitedProfile?.pos_profile) {
    throw new OutletSelectionRequiredError(limitedProfile?.available_outlets ?? []);
  }

  // Get full profile using the pos_profile name from limited profile
  const fullProfile = await getPosProfileFull(limitedProfile.pos_profile);
  return combinePosProfiles(limitedProfile, fullProfile);
}

export interface PosProfileBootstrapResult {
  requiresOutletSelection: boolean;
  availableOutlets: AvailableOutlet[];
  profile: PosProfileCombined | null;
}

export async function getPosProfileOrSelection(): Promise<PosProfileBootstrapResult> {
  const limitedProfile = await getPosProfileLimitedFields();
  if (limitedProfile?.requires_outlet_selection || !limitedProfile?.pos_profile) {
    return {
      requiresOutletSelection: true,
      availableOutlets: limitedProfile?.available_outlets ?? [],
      profile: null,
    };
  }
  const fullProfile = await getPosProfileFull(limitedProfile.pos_profile);
  return {
    requiresOutletSelection: false,
    availableOutlets: [],
    profile: combinePosProfiles(limitedProfile, fullProfile),
  };
}

export async function getCurrencyInfo(currencyCode: string): Promise<Currency> {
  const doc = await db.getDoc(DOCTYPES.CURRENCY, currencyCode);
  return doc;
}