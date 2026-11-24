import { DOCTYPES } from '../data/doctypes';
import { call, db } from '@ury/core';
import type { DefaultCustomerEntry } from '@ury/core/default-customer';

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
  /** Seconds to wait for EFRIS FDN before printing pending receipt + queuing auto-reprint. */
  efris_print_wait_seconds?: number;
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
export interface PosProfileFull {
  ury_enable_hold_fire?: number;
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
  custom_void_reasons?: string | null;
  /** Roles exempt from floor-tablet sign-out (POS Profile.custom_floor_stay_signed_in_roles). */
  custom_floor_stay_signed_in_roles?: Array<{ role: string }>;
  efris_print_wait_seconds?: number;
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
  efris_print_wait_seconds?: number;
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

export async function getCombinedPosProfile(): Promise<PosProfileCombined> {
  // Get limited fields first
  const limitedProfile = await getPosProfileLimitedFields();
  console.log('limitedProfile', limitedProfile);
  
  // Get full profile using the pos_profile name from limited profile
  const fullProfile = await getPosProfileFull(limitedProfile.pos_profile);

  const enablePaymentWithoutPrinting = Number(
    limitedProfile.enable_payment_without_printing ??
    (limitedProfile as any).custom_enable_payment_without_printing ??
    fullProfile.enable_payment_without_printing ??
    fullProfile.custom_enable_payment_without_printing ??
    0
  );
  
  // Merge both profiles
  const combinedProfile: PosProfileCombined = {
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
    allow_discount_change: Number(fullProfile.allow_discount_change ?? limitedProfile.allow_discount_change ?? 0),
    multiple_cashier: limitedProfile.multiple_cashier,
    edit_order_type: limitedProfile.edit_order_type,
    custom_invoice_warning_time: limitedProfile.custom_invoice_warning_time ?? 0,
    enable_payment_without_printing: enablePaymentWithoutPrinting,
    custom_enable_payment_without_printing: enablePaymentWithoutPrinting,
    custom_disable_print_status_tracking: limitedProfile.custom_disable_print_status_tracking ?? 0,
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
    efris_print_wait_seconds: limitedProfile.efris_print_wait_seconds ?? fullProfile.efris_print_wait_seconds ?? 5,
    custom_drawer_pin: limitedProfile.custom_drawer_pin ?? fullProfile.custom_drawer_pin ?? 2,
    custom_drawer_kick_hex: limitedProfile.custom_drawer_kick_hex ?? fullProfile.custom_drawer_kick_hex ?? '',
    custom_auto_kick_on_cash_tender: Number(
      limitedProfile.custom_auto_kick_on_cash_tender ??
      fullProfile.custom_auto_kick_on_cash_tender ??
      1
    ),
    drawer_opening_modes: limitedProfile.drawer_opening_modes ?? fullProfile.drawer_opening_modes ?? [],
  };

  return combinedProfile;
}

export async function getCurrencyInfo(currencyCode: string): Promise<Currency> {
  const doc = await db.getDoc(DOCTYPES.CURRENCY, currencyCode);
  return doc;
}