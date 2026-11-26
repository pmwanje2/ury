import { assertNetworkAcknowledgement } from '@ury/core/receipt';
import {
  call,
  db,
  isLinkDown,
  ownedSessionKey,
} from '@ury/core';
import {
  MERGED_POS_INVOICE_PRINT_FORMAT as CORE_MERGED_FORMAT,
  UNSAFE_POS_INVOICE_PRINT_FORMATS as CORE_UNSAFE_FORMATS,
  buildLetterheadParams,
  resolveSafePosPrintFormat,
} from '@ury/core/print-format';
import { assertOfflineActionAllowed } from '../offline/cashier';
import { DOCTYPES } from '../data/doctypes';
import { OrderStatusType, OrderType } from '../data/order-types';
import type { Filter } from 'frappe-js-sdk/lib/db/types';

export interface POSInvoice {
  branch?: string;
  ury_client_order_id?: string | null;
  name: string;
  pos_profile?: string | null;
  currency?: string;
  conversion_rate?: number | null;
  efris_irn?: string | null;
  efris_status?: string | null;
  invoice_printed: number;
  custom_printing_time?: string | null;
  grand_total: number;
  restaurant_table: string | null;
  cashier: string;
  /** the waiter's User id, usually an email address */
  waiter: string;
  /** the waiter's full name, when the server resolves it; not read from POS Invoice */
  waiter_name?: string | null;
  net_total: number;
  total?: number;
  posting_time: string;
  total_taxes_and_charges: number;
  custom_service_charge_waived?: number;
  custom_tax_exempt?: number;
  customer: string;
  customer_name?: string;
  /** a short name for this check (Susan, Seat 2); not a Customer */
  custom_guest_label?: string | null;
  /** covers (guest count) on the check */
  no_of_pax?: number | null;
  /** when the check was opened (site time); open checks are aged from it */
  creation?: string;
  status: 'Draft' | 'Unbilled' | 'Recently Paid' | 'Paid' | 'Consolidated' | 'Return';
  mobile_number: string;
  posting_date: string;
  rounded_total: number;
  order_type: OrderType;
  custom_merged_tables?: string | null;
  custom_split_group?: string | null;
  custom_split_from?: string | null;
  split_index?: number;
  split_total?: number;
  split_siblings?: string[];
  custom_merged_pos_invoice?: string | null;
  custom_charged_tip?: number | null;
  custom_tip_server?: string | null;
  custom_merged_total?: number | null;
  discount_amount?: number;
  additional_discount_percentage?: number;
  coupon_code?: string | null;
  payments?: POSInvoicePayment[];
  room_number?: string | null;
  custom_offline_conflict?: number;
  age_minutes?: number;
  items_preview?: Array<{ item_name: string; qty: number }>;
}

export interface POSInvoicePayment {
  mode_of_payment: string;
  amount: number;
  reference_no?: string | null;
  reference_date?: string | null;
  clearance_date?: string | null;
  room_number?: string | null;
  custom_tender_received?: number | boolean;
  custom_tender_currency?: string | null;
  custom_tender_amount?: number | null;
  custom_exchange_rate?: number | null;
  custom_exchange_rate_date?: string | null;
}

export interface SplitGroupInvoice extends POSInvoice {
  split_index: number;
  split_total: number;
  is_original: boolean;
  docstatus: number;
  split_siblings?: string[];
}

export interface SplitGroupResponse {
  invoices: SplitGroupInvoice[];
  current: string;
  group: string | null;
}

export interface POSInvoiceItem {
  name: string;
  item_code?: string;
  item_name: string;
  custom_course?: string;
  custom_seat_no?: number;
  custom_is_shareable?: number;
  qty: number;
  rate: number;
  price_list_rate: number;
  amount: number;
  discount_percentage?: number;
  discount_amount?: number;
  max_discount?: number;
  is_disposable?: number;
  comment?: string;
  reservation_line_key?: string | null;
  custom_parent_line_key?: string | null;
  custom_line_role?: string;
  custom_is_modifier?: number;
  custom_modifier_prefix?: string | null;
  custom_modifiers_json?: string | null;
  custom_bundle_overrides_json?: string | null;
}

export interface POSInvoiceTax {
  description: string;
  rate: number;
  tax_amount?: number;
  tax_rate?: number;
  charge_type?: string;
  custom_pos_charge_kind?: string;
  account_head?: string;
}

interface GetPOSInvoicesResponse {
  message: {
    data: POSInvoice[];
    next: boolean;
  };
}

interface GetPOSInvoicesParams {
  status: OrderStatusType;
  limit?: number;
  limit_start?: number;
  paid_limit?: number;
  branch?: string;
}

interface GetPOSInvoiceItemsResponse {
  message: [POSInvoiceItem[], POSInvoiceTax[], POSInvoicePayment[]?];
}

export const outstandingFields = [
  'docstatus', 'name', 'pos_profile', 'currency', 'creation', 'invoice_printed', 'custom_printing_time', 'grand_total', 'restaurant_table',
  'custom_merged_tables', 'cashier', 'waiter', 'net_total', 'posting_time', 'total_taxes_and_charges',
  'customer', 'customer_name', 'status', 'mobile_number', 'posting_date', 'rounded_total', 'order_type',
  'custom_split_group', 'custom_split_from', 'custom_guest_label', 'custom_merged_pos_invoice',
  'custom_merged_total', 'additional_discount_percentage', 'discount_amount', 'ury_client_order_id', 'no_of_pax',
];

export async function getTillBranch() {
  const { message: branch } = await call.get<{ message: string }>('ury.ury_pos.api.getBranch');
  if (!branch) throw new Error('Till branch unavailable');
  return branch;
}

/**
 * Adds what an open-check card shows beyond the list row: how long the check
 * has been open (age_minutes, from its creation and the site clock) and its
 * first items. Either part is left out when it cannot be read.
 */
async function enrichOpenChecks<T extends POSInvoice>(invoices: T[]): Promise<T[]> {
  if (!invoices.length) return invoices;
  const [items, clock] = await Promise.allSettled([
    call.get<{ message: { parent: string; item_name: string; qty: number }[] }>('frappe.client.get_list', {
      doctype: 'POS Invoice Item', parent: 'POS Invoice', fields: ['parent', 'item_name', 'qty'],
      filters: { parenttype: 'POS Invoice', parent: ['in', invoices.map(row => row.name)] },
      order_by: 'parent asc, idx asc', limit_page_length: 0,
    }),
    call.get<{ message: string }>('ury.ury.api.ury_server_time.get_server_time'),
  ]);
  // Both values are site-local wall times. Parse in the same fixed offset so
  // the till's timezone and daylight saving transitions do not change the age.
  const siteMillis = (value: string) => Date.parse(value.replace(' ', 'T') + 'Z');
  for (const inv of invoices) {
    const age = clock.status === 'fulfilled' && typeof clock.value.message === 'string' && inv.creation
      ? siteMillis(clock.value.message) - siteMillis(inv.creation) : NaN;
    if (Number.isFinite(age)) inv.age_minutes = Math.max(0, Math.floor(age / 60000));
    if (items.status === 'fulfilled' && Array.isArray(items.value.message)) inv.items_preview = items.value.message.filter(row => row.parent === inv.name).map(({ item_name, qty }) => ({ item_name, qty }));
  }
  return invoices;
}

/** The queues that list open (unsettled) checks, which carry an age. */
const isOpenCheckQueue = (status: string) => status === 'Draft' || status === 'Unbilled';

async function outstandingChecks(limit: number, offset: number, query?: string, resolvedBranch?: string) {
  const branch = resolvedBranch || await getTillBranch();
  const { message: rows } = await call.get<{ message: (POSInvoice & { creation: string })[] }>('frappe.client.get_list', {
    doctype: 'POS Invoice', filters: { branch, docstatus: 0, status: 'Draft' }, fields: outstandingFields,
    ...(query ? { or_filters: ['name', 'customer', 'customer_name', 'mobile_number'].map(field => [field, 'like', `%${query}%`]) } : {}),
    order_by: 'creation asc, name asc', limit_start: offset, limit_page_length: limit + 1,
  });
  const invoices = await enrichOpenChecks(rows.slice(0, limit));
  return { invoices, hasMore: rows.length > limit };
}

/**
 * Open checks per open queue for the till's branch, as the order status rail
 * shows them. Outstanding is every open check; Unbilled is the unprinted ones
 * on a table; Draft is the rest, as getPosInvoice splits them.
 */
export async function getOpenCheckCounts(resolvedBranch?: string | null): Promise<Partial<Record<OrderStatusType, number>>> {
  const branch = resolvedBranch || await getTillBranch();
  const open = { branch, docstatus: 0, status: 'Draft' };
  const count = async (filters: Record<string, unknown>) => {
    const { message } = await call.get<{ message: number }>('frappe.client.get_count', { doctype: 'POS Invoice', filters });
    if (typeof message !== 'number' || !Number.isFinite(message)) throw new Error('Count unavailable');
    return message;
  };
  const [outstanding, unbilled] = await Promise.all([
    count(open),
    count({ ...open, invoice_printed: 0, restaurant_table: ['is', 'set'] }),
  ]);
  return { Outstanding: outstanding, Draft: Math.max(0, outstanding - unbilled), Unbilled: unbilled };
}

export async function getPOSInvoices({
  status,
  limit,
  limit_start,
  paid_limit, branch
}: GetPOSInvoicesParams) {
  try {
    if (status === 'Outstanding') return await outstandingChecks(limit ?? 20, limit_start ?? 0, undefined, branch);
    // Use paid_limit as the limit for Recently Paid status
    const actualLimit = status === 'Recently Paid' && paid_limit ? paid_limit : limit;

    const response = await call.get<GetPOSInvoicesResponse>(
      'ury.ury_pos.api.getPosInvoice',
      {
        status,
        limit: actualLimit,
        limit_start
      }
    );

    const invoices = isOpenCheckQueue(status)
      ? await enrichOpenChecks(response.message.data)
      : response.message.data;
    return {
      invoices,
      hasMore: response.message.next
    };
  } catch (error) {
    console.error('Error fetching POS invoices:', error);
    if (isLinkDown(error)) throw error;
    throw new Error('Failed to fetch POS invoices');
  }
}

export async function getPOSInvoiceItems(invoiceId: string) {
  try {
    const response = await call.get<GetPOSInvoiceItemsResponse>(
      'ury.ury_pos.api.getPosInvoiceItems',
      {
        invoice: invoiceId
      }
    );

    return {
      items: response.message[0],
      taxes: response.message[1],
      payments: Array.isArray(response.message[2]) ? response.message[2] : undefined,
    };
  } catch (error) {
    console.error('Error fetching POS invoice items:', error);
    throw new Error('Failed to fetch POS invoice items');
  }
}

export async function updateInvoiceStatus(
  invoice: string,
  status: POSInvoice['status']
) {
  try {
    await call.post('ury.ury_pos.api.updatePosInvoiceStatus', {
      invoice,
      status,
    });
  } catch (error) {
    console.error('Error updating invoice status:', error);
    throw new Error('Failed to update invoice status');
  }
}

export async function searchPosInvoice(query: string, status: string, branch?: string) {
  try {
    if (status === 'Outstanding') {
      const { invoices, hasMore } = await outstandingChecks(10, 0, query, branch);
      return { data: invoices, next: hasMore };
    }
    const response = await call.get<{ message: { data: POSInvoice[]; next: boolean } }>('ury.ury_pos.api.searchPosInvoice', {
      query,
      status,
    });
    if (isOpenCheckQueue(status) && Array.isArray(response.message?.data)) await enrichOpenChecks(response.message.data);
    return response.message;
  } catch (error) {
    console.error('Error searching POS invoices:', error);
    throw error;
  }
}

export async function getSplitGroup(invoiceId: string): Promise<SplitGroupResponse> {
  const response = await call.get<{ message: SplitGroupResponse }>(
    'ury.ury_pos.api.get_split_group',
    { invoice: invoiceId }
  );
  return response.message;
}

export function getOrdersTabForInvoice(
  inv: {
    invoice_printed: number;
    restaurant_table: string | null;
    status: string;
    docstatus?: number;
  },
  options?: { paidLimit?: number; viewAllStatus?: number }
): OrderStatusType {
  const isPaid = inv.docstatus === 1 || inv.status === 'Paid';
  if (isPaid) {
    if (options?.paidLimit && options.paidLimit > 0) return 'Recently Paid';
    if (options?.viewAllStatus === 1) return 'Paid';
    return 'Recently Paid';
  }
  if (inv.invoice_printed === 0 && inv.restaurant_table) return 'Unbilled';
  return 'Draft';
}

export function mapSplitGroupInvoiceToPOSInvoice(inv: SplitGroupInvoice): POSInvoice {
  return {
    name: inv.name,
    pos_profile: inv.pos_profile,
    currency: inv.currency,
    efris_irn: inv.efris_irn,
    efris_status: inv.efris_status,
    ury_client_order_id: inv.ury_client_order_id,
    invoice_printed: inv.invoice_printed,
    custom_printing_time: inv.custom_printing_time ?? null,
    grand_total: inv.grand_total,
    restaurant_table: inv.restaurant_table,
    custom_merged_tables: inv.custom_merged_tables,
    cashier: inv.cashier ?? '',
    waiter: inv.waiter ?? '',
    net_total: inv.net_total ?? inv.grand_total,
    posting_time: inv.posting_time,
    total_taxes_and_charges: inv.total_taxes_and_charges ?? 0,
    customer: inv.customer,
    customer_name: inv.customer_name,
    custom_guest_label: inv.custom_guest_label ?? null,
    no_of_pax: inv.no_of_pax ?? null,
    status: inv.status,
    mobile_number: inv.mobile_number ?? '',
    posting_date: inv.posting_date,
    rounded_total: inv.rounded_total,
    order_type: inv.order_type,
    custom_split_group: inv.custom_split_group,
    custom_split_from: inv.custom_split_from,
    split_index: inv.split_index,
    split_total: inv.split_total,
    split_siblings: inv.split_siblings,
    discount_amount: inv.discount_amount,
    additional_discount_percentage: inv.additional_discount_percentage,
  };
}

export async function getInvoicePrintHtml(
  invoiceId: string,
  printFormat: string,
  letterHead?: string | null
) {
  try {
    const { no_letterhead, letterhead } = buildLetterheadParams(letterHead);
    const response = await call.get<{ message: { html: string } }>(
      'frappe.www.printview.get_html_and_style',
      {
        doc: 'POS Invoice',
        name: invoiceId,
        print_format: printFormat,
        _lang: 'en',
        no_letterhead,
        letterhead,
        settings: {}
      }
    );
    return response.message.html;
  } catch (error) {
    console.error('Error fetching invoice print HTML:', error);
    throw new Error('Failed to fetch invoice print HTML');
  }
}

export interface InvoicePrintAuth {
  approvalToken?: string;
  reason?: string;
  reasonNote?: string;
}

export async function networkPrint(orderId: string, printer: string, printFormat: string, auth?: InvoicePrintAuth) {
  const res = await call.post('ury.ury.api.ury_print.network_printing', {
    doctype: 'POS Invoice',
    name: orderId,
    printer_setting: printer,
    print_format: printFormat,
    ...(auth?.approvalToken ? { approval_token: auth.approvalToken } : {}),
    ...(auth?.reason ? { reason: auth.reason } : {}),
    ...(auth?.reasonNote ? { reason_note: auth.reasonNote } : {}),
  });
  assertNetworkAcknowledgement(res?.message);
}


export async function selectNetworkPrinter(
  orderId: string,
  posProfile: string,
  printFormat?: string | null,
  auth?: InvoicePrintAuth,
) {
  const res = await call.post('ury.ury.api.ury_print.select_network_printer', {
    invoice_id: orderId,
    pos_profile: posProfile,
    print_format: printFormat,
    ...(auth?.approvalToken ? { approval_token: auth.approvalToken } : {}),
    ...(auth?.reason ? { reason: auth.reason } : {}),
    ...(auth?.reasonNote ? { reason_note: auth.reasonNote } : {}),
  });
  if (res?.message === null || res?.message === undefined) {
    throw new Error('No bill printer configured for this order. Please configure a bill printer in POS Profile or Room.');
  }
  assertNetworkAcknowledgement(res.message);
}


export async function updatePrintStatus(orderId: string, auth?: InvoicePrintAuth) {
  await call.post('ury.ury.api.ury_print.qz_print_update', {
    invoice: orderId,
    ...(auth?.approvalToken ? { approval_token: auth.approvalToken } : {}),
    ...(auth?.reason ? { reason: auth.reason } : {}),
    ...(auth?.reasonNote ? { reason_note: auth.reasonNote } : {}),
  });
}

export const MERGED_POS_INVOICE_PRINT_FORMAT = CORE_MERGED_FORMAT;

export interface MergeBillCandidate {
  name: string;
  currency?: string;
  customer: string;
  customer_name?: string;
  grand_total: number;
  rounded_total: number;
  posting_date: string;
  posting_time: string;
  order_type: OrderType;
  restaurant_table: string | null;
  custom_merged_tables?: string | null;
  status: string;
  invoice_printed: number;
  mobile_number?: string;
}

export interface MergeBillsResponse {
  status: 'success' | 'error';
  message: string;
  name?: string;
}

// export function getCombinedOrderTotals(order: Pick<POSInvoice, 'grand_total' | 'rounded_total' | 'custom_merged_total'>) {
//   const mergedTotal = order.custom_merged_total ?? 0;
//   return {
//     grandTotal: order.grand_total + mergedTotal,
//     roundedTotal: order.rounded_total + mergedTotal,
//   };
// }
export function getCombinedOrderTotals(
  order: Pick<POSInvoice, 'grand_total' | 'rounded_total' | 'custom_merged_total'>
) {
  // The server's totals: its rounded total, or its grand total where the profile does not
  // round. A legacy linked bill adds its partner's server total as it is.
  const merged = order.custom_merged_total ?? 0;

  return {
    grandTotal: (order.grand_total ?? 0) + merged,
    roundedTotal: (order.rounded_total || order.grand_total || 0) + merged,
  };
}

export function isMergedBill(order: Pick<POSInvoice, 'custom_merged_pos_invoice'>) {
  return !!order?.custom_merged_pos_invoice;
}

export const UNSAFE_POS_INVOICE_PRINT_FORMATS = CORE_UNSAFE_FORMATS;

export function resolvePrintFormat(
  order: Pick<POSInvoice, 'custom_merged_pos_invoice'>,
  defaultFormat: string | null | undefined
) {
  return resolveSafePosPrintFormat(defaultFormat, Boolean(order?.custom_merged_pos_invoice));
}

export const MERGE_CANDIDATE_PAGE_SIZE = 10;

const MERGE_CANDIDATE_FIELDS = [
  'name',
  'currency',
  'customer',
  'customer_name',
  'grand_total',
  'rounded_total',
  'posting_date',
  'posting_time',
  'order_type',
  'restaurant_table',
  'custom_merged_tables',
  'status',
  'invoice_printed',
  'custom_printing_time',
  'mobile_number',
] as const;

function getBranchFromSession(): string {
  const key = ownedSessionKey('posProfile');
  const raw = key && sessionStorage.getItem(key);
  if (!raw) {
    throw new Error('POS profile not loaded');
  }
  const profile = JSON.parse(raw) as { branch?: string };
  if (!profile.branch) {
    throw new Error('Branch not found in POS profile');
  }
  return profile.branch;
}

export async function getLinkedMergeSecondaries(): Promise<string[]> {
  const rows = await db.getDocList(DOCTYPES.POS_INVOICE, {
    fields: ['custom_merged_pos_invoice'],
    filters: [
      ['docstatus', '=', 0],
      ['custom_merged_pos_invoice', 'is', 'set'],
    ],
    limit: 500,
  } as unknown as Parameters<typeof db.getDocList>[1]);

  return rows
    .map((row: any) => row.custom_merged_pos_invoice as string | undefined)
    .filter((name): name is string => Boolean(name));
}

export interface GetMergeBillCandidatesParams {
  primaryInvoice: string;
  query?: string;
  page?: number;
  pageSize?: number;
  linkedSecondaries?: string[];
}

export async function getMergeBillCandidates({
  primaryInvoice,
  query = '',
  page = 1,
  pageSize = MERGE_CANDIDATE_PAGE_SIZE,
  linkedSecondaries,
}: GetMergeBillCandidatesParams): Promise<{ data: MergeBillCandidate[]; hasMore: boolean }> {
  const branch = getBranchFromSession();
  const secondaries = linkedSecondaries ?? (await getLinkedMergeSecondaries());
  const exclude = Array.from(new Set([primaryInvoice, ...secondaries]));

  const filters: Filter[] = [
    ['branch', '=', branch],
    ['docstatus', '=', 0],
    ['name', 'not in', exclude],
  ];

  const q = query.trim();
  let orFilters: Filter[] | undefined;

  if (q) {
    const pattern = `%${q}%`;
    filters.push(['custom_merged_pos_invoice', 'is', 'not set'] as unknown as Filter);
    orFilters = [
      ['name', 'like', pattern],
      ['customer', 'like', pattern],
      ['customer_name', 'like', pattern],
      ['restaurant_table', 'like', pattern],
      ['mobile_number', 'like', pattern],
    ];
  } else {
    orFilters = [
      ['custom_merged_pos_invoice', 'is', 'not set'],
      ['custom_merged_pos_invoice', '=', ''],
    ] as unknown as Filter[];
  }

  const rows = await db.getDocList(DOCTYPES.POS_INVOICE, {
    fields: [...MERGE_CANDIDATE_FIELDS],
    filters,
    orFilters,
    orderBy: { field: 'modified', order: 'desc' },
    limit: pageSize + 1,
    limit_start: (page - 1) * pageSize,
  } as unknown as Parameters<typeof db.getDocList>[1]);

  const hasMore = rows.length > pageSize;
  const data = (hasMore ? rows.slice(0, pageSize) : rows) as MergeBillCandidate[];

  return { data, hasMore };
}

export async function mergeBills(
  primaryInvoice: string,
  secondaryInvoice: string,
  auth?: InvoicePrintAuth,
): Promise<MergeBillsResponse> {
  await assertOfflineActionAllowed('merge');
  const response = await call.post<{ message: MergeBillsResponse }>(
    'ury.ury_pos.api.merge_bills',
    {
      primary_invoice: primaryInvoice,
      secondary_invoice: secondaryInvoice,
      ...(auth?.approvalToken ? { approval_token: auth.approvalToken } : {}),
      ...(auth?.reason ? { reason: auth.reason } : {}),
      ...(auth?.reasonNote ? { reason_note: auth.reasonNote } : {}),
    }
  );
  const result = response.message;
  if (result.status === 'error') {
    throw new Error(result.message || 'Failed to merge bills');
  }
  return result;
} 