import React, { useState, useEffect, useRef } from 'react';
import { X, Coins, Printer, AlertTriangle, ShieldCheck } from 'lucide-react';
import { usePOSStore } from '../store/pos-store';
import { formatCurrency, parseFrappeError } from '@ury/core';
import { Button, Input, Dialog, DialogContent, showToast } from '@ury/ui';
import { call } from '@ury/core';
import { t } from '../i18n';
import { assertOfflineActionAllowed, submitSettlementOfflineAware } from '../offline/cashier';
import { canSettleOffline } from '../offline/outbox';
import { useRootStore } from '../store/root-store';
import { printOrder } from '../lib/print';
import { queuePendingFiscalReprint, resolveEfrisPrintWaitSeconds, waitForEfrisFdn } from '../lib/efris-print';
import { resolvePrintFormat, type POSInvoice, type POSInvoicePayment } from '../lib/invoice-api';
import { formatPreviewTaxLabel } from '../lib/tax-preview-label';
import { useOrderPreview } from '../lib/use-order-preview';
import { getPosFxRates, type PaymentModeConfig, type PosFxRateInfo } from '../lib/payment-api';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';
import { isConfiguredDefaultCustomer, profileLacksDefaultCustomerConfig } from '@ury/core/default-customer';
import { isAuthRequiredError } from '../lib/override-api';

const MAX_REF_PATTERN_LENGTH = 128;

function stepForCurrency(code?: string | null): string {
  const digits = currencyDecimalPlaces(code);
  if (digits <= 0) return '1';
  if (digits === 3) return '0.001';
  return '0.01';
}

interface PatternGroupFrame {
  hasQuantifier: boolean;
  hasAlternation: boolean;
  isLookaround: boolean;
  prevUnbounded: boolean;
  lastWasQuantifier: boolean;
}

/**
 * Linear-time client ReDoS guard mirroring the server AST validator in `ury/ury_pos/fx.py`.
 * Rejects backreferences, nested quantifiers, alternation inside repeated groups (including
 * arbitrarily nested groups such as `^(a|(b|c))+$`), lookarounds inside repeats, and
 * adjacent unbounded repeats while respecting escapes and `[...]` character classes.
 */
function isSafeReferencePattern(raw: string): boolean {
  if (raw.length > MAX_REF_PATTERN_LENGTH) return false;

  const stack: PatternGroupFrame[] = [
    {
      hasQuantifier: false,
      hasAlternation: false,
      isLookaround: false,
      prevUnbounded: false,
      lastWasQuantifier: false,
    },
  ];
  let inCharClass = false;

  const readQuantifierAt = (
    idx: number,
  ): { nextIdx: number; isMulti: boolean; isUnbounded: boolean } | null => {
    const ch = raw[idx];
    if (ch === '+' || ch === '*') {
      const next = raw[idx + 1] === '?' ? idx + 2 : idx + 1;
      return { nextIdx: next, isMulti: true, isUnbounded: true };
    }
    if (ch === '?') {
      const next = raw[idx + 1] === '?' ? idx + 2 : idx + 1;
      return { nextIdx: next, isMulti: false, isUnbounded: false };
    }
    if (ch === '{') {
      const m = /^\{(\d+)(?:,(\d*))?\}(\?)?/.exec(raw.slice(idx));
      if (m) {
        const min = Number(m[1]);
        const hasComma = m[0].includes(',');
        const maxStr = m[2];
        const isUnbounded = hasComma && (!maxStr || maxStr.length === 0);
        const max = isUnbounded ? Infinity : hasComma ? Number(maxStr) : min;
        return {
          nextIdx: idx + m[0].length,
          isMulti: isUnbounded || max > 1,
          isUnbounded,
        };
      }
    }
    return null;
  };

  let i = 0;
  while (i < raw.length) {
    const ch = raw[i];
    const top = stack[stack.length - 1];

    if (ch === '\\') {
      if (i + 1 >= raw.length) return false;
      const escaped = raw[i + 1];
      if (!inCharClass && escaped >= '1' && escaped <= '9') {
        return false; // Backreference
      }
      if (!inCharClass) {
        top.prevUnbounded = false;
        top.lastWasQuantifier = false;
      }
      i += 2;
      continue;
    }

    if (inCharClass) {
      if (ch === ']') {
        inCharClass = false;
        top.prevUnbounded = false;
        top.lastWasQuantifier = false;
      }
      i += 1;
      continue;
    }

    if (ch === '[') {
      inCharClass = true;
      i += 1;
      continue;
    }

    if (ch === '(') {
      let isLookaround = false;
      let advance = 1;
      if (raw[i + 1] === '?') {
        if (raw[i + 2] === '=' || raw[i + 2] === '!') {
          isLookaround = true;
          advance = 3;
        } else if (raw[i + 2] === '<' && (raw[i + 3] === '=' || raw[i + 3] === '!')) {
          isLookaround = true;
          advance = 4;
        } else if (raw[i + 2] === ':') {
          advance = 3;
        }
      }
      stack.push({
        hasQuantifier: false,
        hasAlternation: false,
        isLookaround,
        prevUnbounded: false,
        lastWasQuantifier: false,
      });
      i += advance;
      continue;
    }

    if (ch === '|') {
      top.hasAlternation = true;
      top.prevUnbounded = false;
      top.lastWasQuantifier = false;
      i += 1;
      continue;
    }

    if (ch === ')') {
      if (stack.length <= 1) return false;
      const popped = stack.pop()!;
      const parent = stack[stack.length - 1];
      const q = readQuantifierAt(i + 1);
      if (q) {
        if (q.isMulti && (popped.hasQuantifier || popped.hasAlternation || popped.isLookaround)) {
          return false;
        }
        if (parent.prevUnbounded && q.isUnbounded) {
          return false;
        }
        // Reject stacked quantifiers directly after a quantified group (e.g. (a)+*)
        if (readQuantifierAt(q.nextIdx)) {
          return false;
        }
        parent.hasQuantifier = true;
        if (popped.hasAlternation) parent.hasAlternation = true;
        if (popped.isLookaround) parent.isLookaround = true;
        parent.prevUnbounded = q.isUnbounded;
        parent.lastWasQuantifier = true;
        i = q.nextIdx;
        continue;
      }
      if (popped.hasQuantifier) parent.hasQuantifier = true;
      if (popped.hasAlternation) parent.hasAlternation = true;
      if (popped.isLookaround) parent.isLookaround = true;
      parent.prevUnbounded = popped.prevUnbounded;
      parent.lastWasQuantifier = false;
      i += 1;
      continue;
    }

    const q = readQuantifierAt(i);
    if (q) {
      if (top.lastWasQuantifier) return false;
      if (top.prevUnbounded && q.isUnbounded) return false;
      top.hasQuantifier = true;
      top.prevUnbounded = q.isUnbounded;
      top.lastWasQuantifier = true;
      i = q.nextIdx;
      continue;
    }

    top.prevUnbounded = false;
    top.lastWasQuantifier = false;
    i += 1;
  }

  return !inCharClass && stack.length === 1;
}

function matchesReferencePattern(value: string, pattern?: string | null): boolean {
  const raw = (pattern || '').trim();
  if (!raw) return true;
  if (!isSafeReferencePattern(raw)) return false;
  try {
    const anchored = raw.startsWith('^') && raw.endsWith('$') ? raw : `^(?:${raw})$`;
    return new RegExp(anchored).test(value);
  } catch {
    return false;
  }
}


const currencyDigitsCache = new Map<string, number>();

function currencyDecimalPlaces(code?: string | null): number {
  if (!code) return 2;
  const cached = currencyDigitsCache.get(code);
  if (cached !== undefined) return cached;
  let digits = 2;
  try {
    digits = new Intl.NumberFormat('en', { style: 'currency', currency: code }).resolvedOptions()
      .maximumFractionDigits ?? 2;
  } catch {
    digits = 2;
  }
  currencyDigitsCache.set(code, digits);
  return digits;
}

function roundToCurrency(amount: number, code?: string | null): number {
  const digits = currencyDecimalPlaces(code);
  const factor = 10 ** digits;
  const rounded = Math.round((amount + Math.sign(amount) * 1e-12) * factor) / factor;
  return Object.is(rounded, -0) ? 0 : rounded;
}

type CreditAccount = {
  name: string;
  customer_name: string;
  balance: number;
  limit: number;
  available: number;
  currency: string;
  conversion_rate?: number | null;
};

interface RoomMatchCandidate {
  ok?: boolean;
  reservation?: string;
  room_number?: string;
  guest_name?: string;
  departure?: string;
  posting_allowed?: boolean;
  no_post?: unknown;
  limit?: number;
  balance?: number;
  available?: number | null;
  currency?: string;
  charge_amount?: number | null;
  override_required?: boolean;
  reason_code?: string | null;
  reason?: string | null;
}

interface RoomCheckState {
  room: string;
  roomNumber?: string;
  reservation?: string;
  guestName?: string;
  departure?: string;
  label?: string;
  ok: boolean;
  error?: string;
  overrideRequired?: boolean;
  noPost?: boolean;
  limit?: number;
  balance?: number;
  available?: number | null;
  currency?: string;
  chargeAmount?: number | null;
  reason?: string | null;
}

interface ApprovalGrant {
  token: string;
  approverName: string;
  reason: string;
}

interface PaymentDialogProps {
  order?: (Pick<POSInvoice, 'currency' | 'conversion_rate' | 'custom_merged_pos_invoice' | 'ury_client_order_id' | 'custom_charged_tip' | 'payments' | 'room_number'> & {
    invoice_printed?: string | number;
    order_type?: string;
  }) | null;
  existingPayments?: POSInvoicePayment[];
  onClose: () => void;
  grandTotal: number;
  roundedTotal: number;
  invoice: string;
  customer: string;
  posProfile: string;
  table: string | null;
  tableLabel?: string | null;
  cashier: string;
  owner: string;
  fetchOrders: () => Promise<void>;
  clearSelectedOrder: () => void;
  discountPercentage?: number;
  discountAmount?: number;
  lineDiscountTotal?: number;
  discountReason?: string | null;
  discountNote?: string | null;
  couponCode?: string | null;
}

const PaymentDialog: React.FC<PaymentDialogProps> = ({
  order,
  existingPayments,
  onClose,
  grandTotal,
  roundedTotal,
  invoice,
  customer: originalCustomer,
  posProfile,
  table,
  tableLabel,
  cashier,
  owner,
  fetchOrders,
  clearSelectedOrder,
  discountPercentage,
  discountAmount,
  lineDiscountTotal,
  discountReason,
  discountNote,
  couponCode,
}) => {
  const user = useRootStore((state) => state.user);
  const {
    paymentModes,
    paymentModeDetails = {},
    fetchPaymentModes,
    posProfile: storePosProfile,
    currency: storeCurrency,
  } = usePOSStore();
  const currencyCode = order?.currency || storePosProfile?.currency || storeCurrency || '';
  const formatMoney = (amount: number) => formatCurrency(amount, currencyCode || undefined);
  const enablePaymentWithoutPrinting = Boolean(
    Number(storePosProfile?.enable_payment_without_printing) === 1 ||
    Number((storePosProfile as any)?.custom_enable_payment_without_printing) === 1
  );
  const existingTip = Math.max(0, Number(order?.custom_charged_tip) || 0);
  const preTipGrandTotal = Math.max(0, grandTotal - existingTip);
  const preTipRoundedTotal = roundedTotal ? Math.max(0, roundedTotal - existingTip) : roundedTotal;
  const submittingRef = useRef(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [showHaloApproval, setShowHaloApproval] = useState(false);
  const [tipInput, setTipInput] = useState(existingTip > 0 ? String(existingTip) : '');
  const printRequired = !enablePaymentWithoutPrinting;
  const [printInvoice, setPrintInvoice] = useState(true);
  const effectivePrintInvoice = printRequired ? true : printInvoice;
  const [error, setError] = useState<string | null>(null);
  const appliedDiscount = discountAmount || 0;
  const appliedLineDiscount = lineDiscountTotal || 0;
  const totalDiscount = appliedDiscount + appliedLineDiscount;
  const hasDiscount = totalDiscount > 0 || (discountPercentage ?? 0) > 0;
  const [paymentInputs, setPaymentInputs] = useState<{ [mode: string]: string }>({});
  const autoFilledDefaultRef = useRef<string | null>(null);
  const [fxInputs, setFxInputs] = useState<{ [mode: string]: string }>({});
  const [refInputs, setRefInputs] = useState<{ [mode: string]: string }>({});
  const [refDateInputs, setRefDateInputs] = useState<{ [mode: string]: string }>({});
  const [liveFxRates, setLiveFxRates] = useState<Record<string, PosFxRateInfo>>({});
  const [fxOverrides, setFxOverrides] = useState<Record<string, { rate: number; token: string; reason?: string }>>({});
  const [pendingFxOverride, setPendingFxOverride] = useState<{ mode: string; currency: string; rate: number } | null>(null);
  const [overrideRateDraft, setOverrideRateDraft] = useState<{ [mode: string]: string }>({});
  const receivedPayments = (existingPayments ?? order?.payments ?? []).filter(
    (row) => Number(row?.amount) > 0 && row?.custom_tender_received !== 0 && row?.custom_tender_received !== false,
  );
  const receivedTotal = roundToCurrency(
    receivedPayments.reduce((sum, row) => sum + Number(row.amount), 0),
    currencyCode,
  );
  const canSelectCredit = storePosProfile?.name === posProfile && storePosProfile.allow_partial_payment === 1
    && !!user?.roles?.some(role => role === 'URY Cashier' || role === 'URY Manager');
  const [query, setQuery] = useState('');
  const [account, setAccount] = useState<CreditAccount | null>(null);
  const [accounts, setAccounts] = useState<CreditAccount[]>([]);
  const [creditStatus, setCreditStatus] = useState('');
  const customer = canSelectCredit && account ? account.name : originalCustomer;
  useEffect(() => {
    let active = true;
    setAccounts([]);
    setCreditStatus('');
    if (!canSelectCredit || !query.trim()) return;
    const timer = window.setTimeout(async () => {
      try {
        const { message } = await call.get<{message: CreditAccount[]}>('ury.ury_pos.api.credit_accounts', {pos_profile: posProfile, query: query.trim()});
        if (active) { setAccounts(message); setCreditStatus(message.length ? '' : t('payment.no_accounts')); }
      } catch (err) { if (active) setCreditStatus(parseFrappeError(err, t('payment.account_search_failed'))); }
    }, 200);
    return () => { active = false; window.clearTimeout(timer); };
  }, [query, canSelectCredit, posProfile]);
  const initialRoom = order?.room_number || receivedPayments.find((row) => row.room_number)?.room_number || '';
  const [roomNumber, setRoomNumber] = useState(initialRoom);
  // Optional room lookup from this till's POS Profile. Blank: free-text room
  // box. Set: Pay waits for a verified match of the room as typed now.
  const roomLookupMethod = (storePosProfile?.name === posProfile && storePosProfile?.room_lookup_method) || '';
  const [roomCheck, setRoomCheck] = useState<RoomCheckState | null>(null);
  const [roomMatches, setRoomMatches] = useState<RoomMatchCandidate[]>([]);
  const [checkingRoom, setCheckingRoom] = useState(false);
  const [roomApproval, setRoomApproval] = useState<ApprovalGrant | null>(null);
  const [onAccountApproval, setOnAccountApproval] = useState<ApprovalGrant | null>(null);
  const [activeApproval, setActiveApproval] = useState<'room_charge_over_limit' | 'on_account_over_limit' | null>(null);

  useEffect(() => {
    fetchPaymentModes();
  }, [fetchPaymentModes]);

  // Stateless server tax breakdown & optional EFRIS readiness check for live orders
  const { preview: taxPreview } = useOrderPreview({
    enabled: Boolean(order?.order_type && posProfile && invoice),
    debounceMs: 150,
    posProfile,
    orderType: order?.order_type,
    customer,
    invoiceName: invoice,
    additionalDiscountPercentage: discountPercentage || 0,
    table,
  });

  useEffect(() => {
    let active = true;
    const hasFxNeedingRate = Object.values(paymentModeDetails || {}).some(
      (cfg) =>
        cfg?.custom_tender_currency &&
        cfg.custom_tender_currency !== currencyCode &&
        cfg.fx_rate_status === undefined &&
        cfg.exchange_rate === undefined,
    );
    if (!hasFxNeedingRate || !posProfile || typeof call?.get !== 'function') return;
    getPosFxRates(posProfile)
      .then((res) => {
        if (active && res?.rates) setLiveFxRates(res.rates);
      })
      .catch(() => {
        // Stale/unreachable FX rates fail closed when rate is absent.
      });
    return () => {
      active = false;
    };
  }, [posProfile, paymentModeDetails, currencyCode]);

  // baseTotal represents the pre-tip amount before any check or line discount
  const baseTotal = preTipGrandTotal + totalDiscount;

  // Order summary logic (discounts are applied on the open check, read-only here)
  const subtotal = baseTotal;
  const discountedTotal = Math.max(0, subtotal - totalDiscount);
  // The server's pre-tip total: its rounded total, or its grand total where the profile does not round.
  const finalTotal = preTipRoundedTotal || preTipGrandTotal;
  // A charged or cash tip is owed to the server: the tenders cover it, it is
  // never change, and the server books it to the till's Tips Payable account.
  const tipAmount = parseFloat(tipInput) || 0;
  const tipInvalid = tipAmount < 0;
  const amountDue = roundToCurrency(finalTotal + Math.max(0, tipAmount), currencyCode);
  const balanceDue = Math.max(0, roundToCurrency(amountDue - receivedTotal, currencyCode));
  // Sent only when there is a tip, so an untipped settlement is unchanged.
  const tipFields = tipAmount > 0 ? { tip: tipAmount } : {};

  const getModeDetail = (id: string): PaymentModeConfig | undefined => paymentModeDetails?.[id];

  const getFxCurrency = (id: string): string | null => {
    const curr = (getModeDetail(id)?.custom_tender_currency || '').trim().toUpperCase();
    return curr && currencyCode && curr !== currencyCode.toUpperCase() ? curr : null;
  };

  const getEffectiveFxRate = (id: string): number | null => {
    const overridden = fxOverrides[id]?.rate;
    if (overridden && overridden > 0) return overridden;
    const live = liveFxRates[id];
    if (live) {
      return live.status === 'fresh' && Number(live.rate) > 0 ? Number(live.rate) : null;
    }
    const detail = getModeDetail(id);
    if (detail?.fx_rate_status === 'stale') return null;
    if (Number(detail?.exchange_rate) > 0) return Number(detail?.exchange_rate);
    return null;
  };

  const getReferenceError = (id: string, active: boolean): string | null => {
    if (!active) return null;
    const detail = getModeDetail(id);
    const mode = detail?.custom_reference_mode || 'Hidden';
    if (mode === 'Hidden') return null;
    const refVal = (refInputs[id] || '').trim();
    const refDateVal = (refDateInputs[id] || '').trim();
    if (mode === 'Required' && !refVal) {
      return t('payment.reference_required', { mode: id });
    }
    if (refVal && detail?.custom_reference_pattern && !matchesReferencePattern(refVal, detail.custom_reference_pattern)) {
      return t('payment.reference_invalid', { mode: id });
    }
    if (Number(detail?.custom_reference_date_required) === 1 && (mode === 'Required' || Boolean(refVal)) && !refDateVal) {
      return t('payment.reference_date_required', { mode: id });
    }
    return null;
  };

  type TenderPayload = {
    mode_of_payment: string;
    amount: number;
    reference_no?: string;
    reference_date?: string;
    clearance_date?: string;
    custom_tender_currency?: string;
    custom_tender_amount?: number;
    override_exchange_rate?: number;
    fx_override_token?: string;
    fx_override_reason?: string;
  };

  // Calculate split payment total for new tenders taken now (home + foreign currency)
  const newPayments = paymentModes
    .map((mode: any): TenderPayload | null => {
      const id = typeof mode === 'string' ? mode : mode.id;
      const fxCurr = getFxCurrency(id);
      const refVal = (refInputs[id] || '').trim();
      const refDateVal = (refDateInputs[id] || '').trim();
      if (fxCurr) {
        const fxAmt = parseFloat(fxInputs[id] || '');
        const rate = getEffectiveFxRate(id);
        if (!(fxAmt > 0) || !rate) return null;
        const localAmt = roundToCurrency(fxAmt * rate, currencyCode);
        const ov = fxOverrides[id];
        return {
          mode_of_payment: id,
          amount: localAmt,
          custom_tender_currency: fxCurr,
          custom_tender_amount: roundToCurrency(fxAmt, fxCurr),
          ...(refVal ? { reference_no: refVal } : {}),
          ...(refDateVal ? { reference_date: refDateVal } : {}),
          ...(ov ? { override_exchange_rate: ov.rate, fx_override_token: ov.token, ...(ov.reason ? { fx_override_reason: ov.reason } : {}) } : {}),
        };
      }
      const amount = parseFloat(paymentInputs[id] || '');
      if (!(amount > 0)) return null;
      return {
        mode_of_payment: id,
        amount,
        ...(refVal ? { reference_no: refVal } : {}),
        ...(refDateVal ? { reference_date: refDateVal } : {}),
      };
    })
    .filter(Boolean) as TenderPayload[];

  const hasStaleFxInput = paymentModes.some((mode: any) => {
    const id = typeof mode === 'string' ? mode : mode.id;
    const fxCurr = getFxCurrency(id);
    if (!fxCurr) return false;
    const fxAmt = parseFloat(fxInputs[id] || '');
    return fxAmt > 0 && !getEffectiveFxRate(id);
  });

  const hasRefValidationError = paymentModes.some((mode: any) => {
    const id = typeof mode === 'string' ? mode : mode.id;
    const fxCurr = getFxCurrency(id);
    const active = fxCurr ? (parseFloat(fxInputs[id] || '') || 0) > 0 : (parseFloat(paymentInputs[id] || '') || 0) > 0;
    return getReferenceError(id, active) !== null;
  });

  const hasFxTender = newPayments.some((p) => Boolean(p.custom_tender_currency));
  const rawNewPaymentsTotal = newPayments.reduce((sum, p) => sum + p.amount, 0);
  const newPaymentsTotal = roundToCurrency(rawNewPaymentsTotal, currencyCode);
  const rawTotalTendered = receivedTotal + rawNewPaymentsTotal;
  const totalTendered = roundToCurrency(rawTotalTendered, currencyCode);
  const receivedPaymentPayloads: TenderPayload[] = receivedPayments.map((row) => ({
    mode_of_payment: row.mode_of_payment,
    amount: Number(row.amount),
    ...(row.reference_no ? { reference_no: row.reference_no } : {}),
    ...(row.reference_date ? { reference_date: row.reference_date } : {}),
    ...(row.clearance_date ? { clearance_date: row.clearance_date } : {}),
    ...(row.custom_tender_currency ? { custom_tender_currency: row.custom_tender_currency } : {}),
    ...(row.custom_tender_amount ? { custom_tender_amount: Number(row.custom_tender_amount) } : {}),
  }));
  const combinedPayments = [...receivedPaymentPayloads, ...newPayments];
  const modeMeta = storePosProfile?.payment_mode_meta ?? {};
  const isCashMode = (mode: string) => {
    const metaType = modeMeta[mode]?.type || paymentModeDetails?.[mode]?.type;
    if (metaType) return metaType === 'Cash';
    return /^cash$/i.test(mode) || (Boolean(getFxCurrency(mode)) && /\bcash\b/i.test(mode));
  };
  const rawExcess = rawTotalTendered - amountDue;
  const hasExcess = rawExcess > 0.005;
  const cashTendered = combinedPayments.reduce(
    (sum, p) => (isCashMode(p.mode_of_payment) ? sum + p.amount : sum),
    0,
  );
  const nonCashModes = combinedPayments
    .filter((p) => !isCashMode(p.mode_of_payment))
    .map((p) => p.mode_of_payment);
  const nonCashExcess = hasExcess ? roundToCurrency(Math.max(0, rawExcess - cashTendered), currencyCode) : 0;
  const cashChangeDue =
    hasExcess && nonCashExcess <= 0.005
      ? roundToCurrency(Math.max(0, Math.min(rawExcess, cashTendered)), currencyCode)
      : 0;
  const nonCashAllowsTip =
    nonCashExcess > 0.005 &&
    nonCashModes.length > 0 &&
    nonCashModes.every((mode) => modeMeta[mode]?.overtender_rule === 'Post to Charged Tip');

  // High Amount Lockout (HALO): sum per mode_of_payment to prevent split-row evasion.
  const approvalCurrency = storePosProfile?.approval_currency || currencyCode;
  const approvalRate = approvalCurrency.toUpperCase() === currencyCode.toUpperCase()
    ? 1
    : Number(order?.conversion_rate || storePosProfile?.approval_conversion_rate || 0);
  const overHaloTenders = (() => {
    const totalsByMode = new Map<string, number>();
    for (const row of combinedPayments) {
      if (row.amount <= 0) continue;
      totalsByMode.set(
        row.mode_of_payment,
        roundToCurrency((totalsByMode.get(row.mode_of_payment) || 0) + row.amount, currencyCode),
      );
    }
    const exceeded: Array<{ mode_of_payment: string; amount: number; halo: number; approvalAmount: number }> = [];
    for (const mode of Array.from(totalsByMode.keys()).sort()) {
      const amount = totalsByMode.get(mode) || 0;
      const halo = Number(modeMeta[mode]?.halo_max_amount) || 0;
      const approvalAmount = roundToCurrency(amount * approvalRate, approvalCurrency);
      if (halo > 0 && approvalAmount > halo + 1e-9) {
        exceeded.push({ mode_of_payment: mode, amount, halo, approvalAmount });
      }
    }
    return exceeded;
  })();

  // Room-charge tenders are those the POS Profile reports as posting to Guest Ledger Clearing.
  const roomChargeModes = storePosProfile?.room_charge_modes || [];
  const roomChargeUnconfigured = Boolean(storePosProfile?.room_charge_unconfigured) && roomChargeModes.length === 0;
  const roomChargeAmount = roomChargeModes
    .reduce((sum: number, mode: string) => sum + (parseFloat(paymentInputs[mode] || '') || 0), 0);
  const roomMatchActive = !!roomCheck && roomCheck.room === roomNumber.trim();
  const roomVerified = !roomLookupMethod || !(roomChargeAmount > 0)
    || (roomMatchActive && (roomCheck.ok || (Boolean(roomCheck.overrideRequired) && Boolean(roomApproval))));
  const effectiveRoomValue = (roomMatchActive && roomCheck?.roomNumber) ? roomCheck.roomNumber : roomNumber;
  const roomFields = roomChargeAmount > 0
    ? {
        room_number: effectiveRoomValue,
        ...(roomMatchActive && roomCheck?.reservation ? { room_charge_stay: roomCheck.reservation } : {}),
        ...(roomApproval ? { room_charge_approval: roomApproval.token, room_charge_reason: roomApproval.reason } : {}),
      }
    : {};
  const minorTolerance = 0.5 * Math.pow(10, -currencyDecimalPlaces(currencyCode));
  const rawShortfall = amountDue - rawTotalTendered;
  const shortfall = Math.max(0, roundToCurrency(rawShortfall, currencyCode));
  const isShort = rawShortfall > minorTolerance;
  // The POS Profile payment row marked Default is prefilled with the remaining balance due
  // and used as the zero-amount tender row when settling a 100% comped check (Simphony/SAP).
  const defaultPaymentMode = storePosProfile?.default_payment_mode || '';
  // A named (non-default) customer on a profile with allow_partial_payment
  // may settle short; the balance stays owed on the invoice. Profile, order-type
  // and profile-level default customers are not named accounts; a profile that
  // predates the default-customer keys fails closed. The submitted payment list
  // keeps one native mode row, at zero when nothing was tendered.
  const accountPaymentMode = paymentModes
    .map((mode: string | { id: string }) => (typeof mode === 'string' ? mode : mode.id))
    .find(Boolean);
  const zeroSettlementMode = defaultPaymentMode || accountPaymentMode;
  const canPayOnAccount = canSelectCredit && isShort && !!accountPaymentMode
    && storePosProfile?.name === posProfile
    && storePosProfile.allow_partial_payment === 1 && !!storePosProfile.customer
    && !!customer.trim() && customer !== storePosProfile.customer
    && !profileLacksDefaultCustomerConfig(storePosProfile)
    && !isConfiguredDefaultCustomer(customer, storePosProfile);
  const accountCurrency = account?.currency || currencyCode;
  const isFxAccount = Boolean(
    accountCurrency && currencyCode && accountCurrency.toUpperCase() !== currencyCode.toUpperCase(),
  );
  const accountConversionRate = isFxAccount
    ? Number(order?.conversion_rate || account?.conversion_rate || 0)
    : 1;
  const proposedInAccountCurrency = isFxAccount && accountConversionRate > 0
    ? roundToCurrency(shortfall * accountConversionRate, accountCurrency)
    : roundToCurrency(shortfall, accountCurrency);
  const accountTolerance = 0.5 * Math.pow(10, -currencyDecimalPlaces(accountCurrency));
  const onAccountOverLimit = canPayOnAccount && !!account && proposedInAccountCurrency > Number(account.available) + accountTolerance;

  useEffect(() => {
    setRoomApproval(null);
  }, [roomNumber, roomChargeAmount]);

  useEffect(() => {
    setOnAccountApproval(null);
  }, [customer, shortfall]);

  // A verified 0-balance check (100% comped or already fully covered by received tenders)
  // can settle cleanly without entering a positive payment amount (Simphony/SAP 100% comp close).
  const isZeroBalanceSettlement = balanceDue <= 0 && !isShort
    && (hasDiscount || receivedTotal > 0)
    && (combinedPayments.length > 0 || Boolean(zeroSettlementMode));
  // Both rules hold: a Room Charge waits for a verified room, required payment
  // references / valid FX rates / valid tips must be satisfied, and only a named account may settle short.
  const paymentDisabled = isProcessing || !roomVerified || tipInvalid || hasRefValidationError || hasStaleFxInput
    || ((combinedPayments.length === 0 || isShort) && !canPayOnAccount && !isZeroBalanceSettlement);
  const settlementPayments = combinedPayments.length === 0 && (canPayOnAccount || isZeroBalanceSettlement) && zeroSettlementMode
    ? [{ mode_of_payment: zeroSettlementMode, amount: 0 }]
    : combinedPayments;

  const applyRoomCandidate = (match: RoomMatchCandidate, queryKey: string) => {
    setRoomApproval(null);
    setRoomMatches([]);
    const matchCurrency = match.currency || currencyCode;
    const isNoPost = Boolean(match.no_post && Number(match.no_post) !== 0);
    if (match.ok === false || (!match.guest_name && !match.reservation)) {
      setRoomCheck({
        room: queryKey,
        ok: false,
        overrideRequired: false,
        limit: match.limit,
        balance: match.balance,
        available: match.available,
        currency: matchCurrency,
        error: match.reason || t('payment.room_check_failed'),
      });
      return;
    }
    if (match.override_required) {
      const reasonMsg = match.reason || (isNoPost ? t('payment.room_no_post') : t('payment.room_over_limit'));
      setRoomCheck({
        room: queryKey,
        roomNumber: match.room_number || queryKey,
        reservation: match.reservation,
        guestName: match.guest_name,
        departure: match.departure,
        ok: false,
        overrideRequired: true,
        noPost: isNoPost,
        limit: match.limit,
        balance: match.balance,
        available: match.available,
        currency: matchCurrency,
        chargeAmount: match.charge_amount ?? null,
        reason: reasonMsg,
        error: reasonMsg,
      });
      return;
    }
    if (isNoPost) {
      setRoomCheck({
        room: queryKey,
        ok: false,
        overrideRequired: false,
        noPost: true,
        limit: match.limit,
        balance: match.balance,
        available: match.available,
        currency: matchCurrency,
        chargeAmount: match.charge_amount ?? null,
        error: t('payment.room_no_post'),
      });
      return;
    }
    if (match.posting_allowed === false) {
      setRoomCheck({
        room: queryKey,
        roomNumber: match.room_number || queryKey,
        reservation: match.reservation,
        guestName: match.guest_name,
        departure: match.departure,
        ok: false,
        overrideRequired: false,
        limit: match.limit,
        balance: match.balance,
        available: match.available,
        currency: matchCurrency,
        chargeAmount: match.charge_amount ?? null,
        error: match.reason || t('payment.room_check_failed'),
      });
      return;
    }
    setRoomCheck({
      room: queryKey,
      roomNumber: match.room_number || queryKey,
      reservation: match.reservation,
      guestName: match.guest_name,
      departure: match.departure,
      ok: true,
      overrideRequired: false,
      noPost: false,
      limit: match.limit,
      balance: match.balance,
      available: match.available,
      currency: matchCurrency,
      chargeAmount: match.charge_amount ?? null,
      label: t('payment.room_match', {
        room: match.room_number || queryKey,
        guest: match.guest_name || '',
        departure: match.departure || '',
      }),
    });
  };

  const handleCheckRoom = async () => {
    const queryText = roomNumber.trim();
    if (!queryText || !roomLookupMethod) return;
    setCheckingRoom(true);
    setRoomCheck(null);
    setRoomMatches([]);
    setRoomApproval(null);
    try {
      const looksLikeSurname = /^[a-zA-Z\u00C0-\u024F\u0600-\u06FF\s'-]{2,}$/.test(queryText);
      const isRichVerifier = roomLookupMethod.includes('verify_room_charge');
      const params: Record<string, unknown> = looksLikeSurname
        ? {
            pos_profile: posProfile,
            surname: queryText,
            ...(roomChargeAmount > 0 ? { amount: roomChargeAmount, currency: currencyCode } : {}),
          }
        : isRichVerifier
          ? {
              pos_profile: posProfile,
              room: queryText,
              ...(roomChargeAmount > 0 ? { amount: roomChargeAmount, currency: currencyCode } : {}),
            }
          : { pos_profile: posProfile, room: queryText };
      const res = await call.get(roomLookupMethod, params);
      const msg = (res as { message?: RoomMatchCandidate & { matches?: RoomMatchCandidate[]; shared_room?: boolean } } | null)?.message;
      if (!msg) {
        setRoomCheck({ room: queryText, ok: false, error: t('payment.room_check_failed') });
        return;
      }
      if (Array.isArray(msg.matches) && msg.matches.length > 1) {
        setRoomMatches(msg.matches);
        setRoomCheck({
          room: queryText,
          ok: false,
          overrideRequired: false,
          error: msg.reason || t('payment.room_pick_guest'),
        });
        return;
      }
      if (Array.isArray(msg.matches) && msg.matches.length === 1) {
        applyRoomCandidate(msg.matches[0], queryText);
        return;
      }
      applyRoomCandidate(msg, queryText);
    } catch (err) {
      setRoomCheck({ room: queryText, ok: false, error: parseFrappeError(err, t('payment.room_check_failed')) });
    } finally {
      setCheckingRoom(false);
    }
  };

  const finalAdjustment = finalTotal - discountedTotal;
  const roundedFinalAdjustment = roundToCurrency(finalAdjustment, currencyCode);
  const showFinalAdjustment = Math.abs(roundedFinalAdjustment) > 0.001;

  useEffect(()=>{
    const defaultPaymentModePresent=paymentModes.find((mode: string | { id: string })=>(typeof mode === 'string' ? mode : mode.id)===defaultPaymentMode)
    const defaultIsFx = Boolean(defaultPaymentMode && getFxCurrency(defaultPaymentMode));
    //only one payment mode should be present, then autofill the remaining balance due, if not do not fill
    const otherPaymentModesNotEntered=Object.keys(paymentInputs).length<=1 && Object.keys(fxInputs).length===0;
    if(paymentModes && defaultPaymentMode && defaultPaymentModePresent && !defaultIsFx && otherPaymentModesNotEntered){
      if(balanceDue > 0){
        autoFilledDefaultRef.current = defaultPaymentMode;
        setPaymentInputs((prev)=>({
          ...prev,
          [defaultPaymentMode]:String(balanceDue)
        }))
      } else if (receivedTotal > 0) {
        autoFilledDefaultRef.current = null;
        setPaymentInputs((prev)=>({
          ...prev,
          [defaultPaymentMode]:''
        }))
      }
    }
  },[balanceDue,receivedTotal,paymentModes,defaultPaymentMode])

  const clearAutoFilledDefaultIfOther = (activeModeId: string) => {
    const autoMode = autoFilledDefaultRef.current;
    if (autoMode && autoMode !== activeModeId) {
      autoFilledDefaultRef.current = null;
      setPaymentInputs((prev) => {
        if (!(autoMode in prev)) return prev;
        const next = { ...prev };
        delete next[autoMode];
        return next;
      });
    } else if (autoMode === activeModeId) {
      autoFilledDefaultRef.current = null;
    }
  };

  // Helper to calculate remaining balance rounded to the active currency's precision
  const getRemainingBalance = (currentId: string) => {
    const totalEntered = Object.entries(paymentInputs)
      .filter(([id]) => id !== currentId)
      .reduce((sum, [_, val]) => sum + (parseFloat(val) || 0), 0);
    return Math.max(0, roundToCurrency(balanceDue - totalEntered, currencyCode));
  };

  // Handler for input focus to auto-fill remaining balance
  const handlePaymentInputFocus = (id: string) => {
    setPaymentInputs(inputs => {
      // Only auto-fill if the field is empty or zero
      if (!inputs[id] || parseFloat(inputs[id]) === 0) {
        const remaining = getRemainingBalance(id);
        return { ...inputs, [id]: remaining > 0 ? String(remaining) : '' };
      }
      return inputs;
    });
  };

  const executePayment = async (opts?: {
    accountGrant?: ApprovalGrant | null;
    haloApprovalToken?: string;
    haloApprovalReason?: string;
  }) => {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setIsProcessing(true);
    setError(null);
    const activeAccountApproval = opts?.accountGrant !== undefined ? opts.accountGrant : onAccountApproval;
    const accountApprovalFields = activeAccountApproval
      ? {
          on_account_approval: activeAccountApproval.token,
          on_account_reason: activeAccountApproval.reason,
        }
      : {};
    const haloFields = {
      ...(opts?.haloApprovalToken ? { halo_approval_token: opts.haloApprovalToken } : {}),
      ...(opts?.haloApprovalReason ? { halo_approval_reason: opts.haloApprovalReason } : {}),
    };
    try {
      await assertOfflineActionAllowed('online invoice settlement');
      const wasAlreadyPrinted = order ? String(order.invoice_printed) !== '0' : false;
      if (!enablePaymentWithoutPrinting && !wasAlreadyPrinted) {
        setError(t('errors.please_print_first'));
        showToast.error(t('errors.please_print_first'));
        return;
      }

      const offlineCapable = canSettleOffline(
        storePosProfile ?? { name: posProfile },
        user ?? { name: 'Guest', roles: [] }
      );
      const trimmedRoomNumber = roomChargeAmount > 0 ? roomNumber.trim() : undefined;
      const singleNewTender = receivedPayments.length === 0 && newPayments.length === 1 ? newPayments[0] : null;

      const dispatchPaymentSettled = () => {
        if (typeof window === 'undefined') return;
        window.dispatchEvent(
          new CustomEvent('pos:payment-settled', {
            detail: {
              invoice,
              posProfile,
              modes: newPayments.map((p) => p.mode_of_payment),
              payments: newPayments,
              changeAmount: cashChangeDue,
              reason: 'Payment Settlement',
              ...(storePosProfile?.drawer_opening_modes !== undefined
                ? { drawerModes: storePosProfile.drawer_opening_modes }
                : {}),
              ...(storePosProfile?.custom_auto_kick_on_cash_tender !== undefined
                ? { autoKickEnabled: storePosProfile.custom_auto_kick_on_cash_tender }
                : {}),
              ...(storePosProfile?.custom_drawer_pin !== undefined
                ? { pin: storePosProfile.custom_drawer_pin }
                : {}),
              ...(storePosProfile?.custom_drawer_kick_hex
                ? { hex: storePosProfile.custom_drawer_kick_hex }
                : {}),
            },
          }),
        );
      };

      if (effectivePrintInvoice && !offlineCapable) {
        // Step 1: Save draft invoice with payment modes without submitting (saved check discount is preserved server-side)
        await call.post('ury.ury.doctype.ury_order.ury_order.make_invoice', {
          cashier,
          customer,
          invoice,
          owner,
          payments: settlementPayments,
          pos_profile: posProfile,
          table,
          submit_invoice: 0,
          ...tipFields,
          ...roomFields,
          ...accountApprovalFields,
          ...haloFields,
        });

        // Step 2: Submit to settle. Submission fiscalises the invoice (EFRIS),
        // so the customer receipt prints only after it (Simphony: the guest
        // check is pro forma; the receipt prints at tender).
        await call.post('ury.ury.doctype.ury_order.ury_order.submit_order_invoice', {
          invoice,
          ...(roomChargeAmount > 0 && roomApproval
            ? { room_charge_approval: roomApproval.token, room_charge_reason: roomApproval.reason }
            : {}),
          ...accountApprovalFields,
        });
      } else {
        const settlement = await submitSettlementOfflineAware({
          profile: storePosProfile ?? { name: posProfile }, user: user ?? { name: 'Guest', roles: [] },
          invoice, customer, table, cashier, owner, clientOrderId: order?.ury_client_order_id,
          amount: totalTendered, due: amountDue, discount: totalDiscount, tip: tipAmount,
          modeOfPayment: singleNewTender?.mode_of_payment ?? (receivedPayments.length === 0 && settlementPayments.length === 1 ? settlementPayments[0]?.mode_of_payment ?? '' : ''),
          reference: singleNewTender?.reference_no,
          referenceDate: singleNewTender?.reference_date,
          roomNumber: trimmedRoomNumber,
          onlineSettle: (ids) => call.post('ury.ury.doctype.ury_order.ury_order.make_invoice', {
            cashier,
            customer,
            invoice,
            owner,
            payments: settlementPayments,
            pos_profile: posProfile,
            table,
            submit_invoice: 1,
            ...tipFields,
            ...roomFields,
            ...accountApprovalFields,
            ...haloFields,
            ...(ids && { client_order_id: ids.clientOrderId, round_id: ids.roundId }),
          }),
        });
        if (settlement.offline) {
          dispatchPaymentSettled();
          showToast.success(t('offline.saved'));
          onClose();
          clearSelectedOrder();
          return;
        }
      }
      dispatchPaymentSettled();
      // Step 3: Wait boundedly for the EFRIS FDN (default 5s, configurable 0-15s),
      // print the fiscal receipt, and on timeout queue an automatic COPY #n reprint.
      if (effectivePrintInvoice && storePosProfile) {
        const efrisOutcome = await waitForEfrisFdn({
          invoice,
          partnerInvoice: order?.custom_merged_pos_invoice ?? null,
          timeoutSeconds: resolveEfrisPrintWaitSeconds(storePosProfile.efris_print_wait_seconds),
        });
        try {
          await printOrder({
            orderId: invoice,
            posProfile: storePosProfile,
            printFormat: resolvePrintFormat(order || {}, storePosProfile.print_format),
            skipStatusUpdate: true,
          });
        } catch (printErr: unknown) {
          console.error('Print failed:', printErr);
          const reason = (printErr instanceof Error ? printErr.message : '') || parseFrappeError(printErr) || 'Unknown error';
          showToast.error(t('payment.receipt_print_failed', { reason }));
        }
        if (efrisOutcome.outcome === 'timed_out') {
          await queuePendingFiscalReprint({
            invoice,
            partnerInvoice: order?.custom_merged_pos_invoice ?? null,
            posProfile: storePosProfile,
          });
        }
      }
      showToast.success(t('payment.success', { amount: formatMoney(totalTendered) }));
      onClose();
      clearSelectedOrder();
      await fetchOrders();
    } catch (err) {
      console.error('Payment failed:', err);
      const parsed = parseFrappeError(err, t('errors.payment_failed'));
      if (canPayOnAccount && (isAuthRequiredError(err) || parsed.includes('POS-CREDIT-004'))) {
        setActiveApproval('on_account_over_limit');
      }
      setError(parsed);
    } finally {
      submittingRef.current = false;
      setIsProcessing(false);
    }
  };

  const handlePayment = async () => {
    if (onAccountOverLimit && !onAccountApproval) {
      setActiveApproval('on_account_over_limit');
      return;
    }
    if (overHaloTenders.length > 0) {
      setError(null);
      setShowHaloApproval(true);
      return;
    }
    await executePayment();
  };

  const handleManagerApproved = (result: ManagerApprovalResult) => {
    const grant: ApprovalGrant = {
      token: result.token,
      approverName: result.approverName,
      reason: result.reason || '',
    };
    const kind = activeApproval;
    setActiveApproval(null);
    if (kind === 'room_charge_over_limit') {
      setRoomApproval(grant);
      return;
    }
    if (kind === 'on_account_over_limit') {
      setOnAccountApproval(grant);
      if (overHaloTenders.length > 0) {
        setError(null);
        setShowHaloApproval(true);
        return;
      }
      void executePayment({ accountGrant: grant });
    }
  };

  const handleHaloApproved = (result: ManagerApprovalResult) => {
    setShowHaloApproval(false);
    void executePayment({
      haloApprovalToken: result.token,
      haloApprovalReason: result.reason,
    });
  };

  return (
    <Dialog open={true} onOpenChange={onClose}>
      <DialogContent variant="xlarge" className="bg-white w-full max-w-4xl max-h-dialog-max-h flex flex-col md:flex-row overflow-y-auto md:overflow-hidden p-0" showCloseButton={false}>
        {/* Left Column - Discount and Payment Mode */}
        <div className="md:w-1/2 shrink-0 md:shrink md:min-h-0 p-6 border-b md:border-b-0 md:border-e border-border md:overflow-y-auto">
          <div className="flex justify-between items-center mb-6">
            <h2 className="text-2xl font-bold text-gray-900">{t('payment.title')}</h2>
            <Button
              onClick={onClose}
              variant="ghost"
              size="icon"
              className="p-2"
            >
              <X className="w-5 h-5" />
            </Button>
          </div>

          {/* Payment Methods Section - Split Payment */}
          <div className="space-y-4 mb-6">
            <h3 className="text-lg font-semibold">{t('payment.payment_methods')}</h3>
            {receivedPayments.length > 0 && (
              <div
                data-testid="received-tenders"
                className="rounded-md border border-emerald-200 bg-emerald-50 p-3 space-y-1.5"
              >
                <p className="text-xs font-semibold uppercase tracking-wide text-emerald-800">
                  {t('payment.already_received')}
                </p>
                {receivedPayments.map((row, idx) => {
                  const ref =
                    row.reference_no ||
                    (roomChargeModes.includes(row.mode_of_payment)
                      ? row.room_number || order?.room_number || ''
                      : '');
                  return (
                    <div
                      key={`${row.mode_of_payment}-${idx}`}
                      data-testid="received-tender-row"
                      className="flex items-center justify-between gap-2 text-sm text-emerald-900"
                    >
                      <span className="font-medium">
                        {row.mode_of_payment}
                        {ref && (
                          <span className="ms-2 text-xs font-normal text-emerald-700">
                            {t('payment.reference_label', { ref })}
                          </span>
                        )}
                      </span>
                      <span className="font-semibold tabular-nums">{formatMoney(Number(row.amount))}</span>
                    </div>
                  );
                })}
              </div>
            )}
            <div className="grid grid-cols-1 gap-3">
              {paymentModes.map((mode: any) => {
                const id = typeof mode === 'string' ? mode : mode.id;
                const label = typeof mode === 'string' ? mode : mode.name;
                const detail = getModeDetail(id);
                const fxCurr = getFxCurrency(id);
                const fxRate = fxCurr ? getEffectiveFxRate(id) : null;
                const fxIsStale = Boolean(fxCurr && !fxRate);
                const fxRawVal = fxInputs[id] || '';
                const fxNum = parseFloat(fxRawVal) || 0;
                const fxLocalEquiv = fxCurr && fxRate && fxNum > 0 ? roundToCurrency(fxNum * fxRate, currencyCode) : 0;
                const isActiveTender = fxCurr ? fxNum > 0 : (parseFloat(paymentInputs[id] || '') || 0) > 0;
                const refMode = detail?.custom_reference_mode || 'Hidden';
                const showRefInput = refMode !== 'Hidden' && isActiveTender;
                const refRequired = refMode === 'Required';
                const refDateRequired = Number(detail?.custom_reference_date_required) === 1;
                const refError = getReferenceError(id, isActiveTender);
                const allowFxOverride = Number(storePosProfile?.custom_allow_fx_rate_override) === 1;

                return (
                  <div key={id} className="rounded-md border border-gray-200 p-2.5 space-y-2">
                    <div className="flex items-center gap-3">
                      <div className="w-28 shrink-0">
                        <span className="font-medium text-sm text-gray-900 break-words">{label}</span>
                        {fxCurr && (
                          <span
                            data-testid={`fx-badge-${id}`}
                            className="mt-0.5 inline-flex items-center rounded bg-blue-50 px-1.5 py-0.5 text-[11px] font-semibold text-blue-700 border border-blue-200"
                          >
                            {fxCurr}
                          </span>
                        )}
                      </div>
                      {fxCurr ? (
                        <Input
                          data-testid={`payment-fx-amount-${id}`}
                          type="number"
                          min="0"
                          step={stepForCurrency(fxCurr)}
                          value={fxRawVal}
                          onChange={(e) => {
                            if (e.target.value) clearAutoFilledDefaultIfOther(id);
                            setFxInputs((prev) => ({ ...prev, [id]: e.target.value }));
                          }}
                          placeholder={fxCurr}
                          className="flex-1"
                          size="sm"
                          disabled={isProcessing || fxIsStale}
                        />
                      ) : (
                        <Input
                          data-testid={`payment-amount-${id}`}
                          type="number"
                          min="0"
                          step="0.01"
                          value={paymentInputs[id] || ''}
                          onChange={(e) => {
                            if (e.target.value) clearAutoFilledDefaultIfOther(id);
                            setPaymentInputs((inputs) => ({ ...inputs, [id]: e.target.value }));
                          }}
                          onFocus={() => handlePaymentInputFocus(id)}
                          placeholder={t('payment.amount_placeholder')}
                          className="flex-1"
                          size="sm"
                          disabled={isProcessing}
                        />
                      )}
                    </div>

                    {fxCurr && fxRate && (
                      <div className="flex flex-wrap items-center justify-between gap-1 text-xs text-gray-600 ps-1">
                        <span className="tabular-nums">
                          {t('payment.fx_rate_badge', { currency: fxCurr, rate: fxRate, home: currencyCode })}
                        </span>
                        {fxLocalEquiv > 0 && (
                          <span data-testid={`fx-equiv-${id}`} className="font-semibold text-blue-700 tabular-nums">
                            {t('payment.fx_equivalent', { amount: formatMoney(fxLocalEquiv) })}
                          </span>
                        )}
                      </div>
                    )}

                    {fxCurr && fxIsStale && (
                      <div
                        role="alert"
                        data-testid={`fx-stale-alert-${id}`}
                        className="rounded border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-xs text-amber-900 space-y-1.5"
                      >
                        <div className="flex items-start gap-1.5">
                          <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0 text-amber-700" />
                          <span>{t('payment.fx_stale_rate', { currency: fxCurr })}</span>
                        </div>
                        {allowFxOverride && (
                          <div className="flex items-center gap-2 pt-1">
                            <Input
                              data-testid={`fx-override-rate-input-${id}`}
                              type="number"
                              min="0"
                              step="0.0001"
                              value={overrideRateDraft[id] || ''}
                              onChange={(e) => setOverrideRateDraft((prev) => ({ ...prev, [id]: e.target.value }))}
                              placeholder={t('payment.fx_override_rate')}
                              size="sm"
                              className="flex-1 bg-white"
                            />
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              disabled={isProcessing || !(parseFloat(overrideRateDraft[id] || '') > 0)}
                              onClick={() => {
                                const parsed = parseFloat(overrideRateDraft[id] || '');
                                if (parsed > 0) {
                                  setPendingFxOverride({ mode: id, currency: fxCurr, rate: parsed });
                                }
                              }}
                            >
                              <ShieldCheck className="w-3.5 h-3.5 me-1" />
                              {t('payment.fx_override_rate')}
                            </Button>
                          </div>
                        )}
                      </div>
                    )}

                    {showRefInput && (
                      <div className="space-y-1.5 pt-1 border-t border-gray-100">
                        <label
                          htmlFor={`payment-ref-${id}`}
                          className="block text-xs font-medium text-gray-700"
                        >
                          {detail?.custom_reference_label || t('payment.reference_label_default')}
                          {refRequired && <span className="text-red-600 ms-0.5">*</span>}
                        </label>
                        <Input
                          id={`payment-ref-${id}`}
                          data-testid={`payment-ref-${id}`}
                          type="text"
                          value={refInputs[id] || ''}
                          onChange={(e) => setRefInputs((prev) => ({ ...prev, [id]: e.target.value }))}
                          placeholder={detail?.custom_reference_placeholder || t('payment.reference_placeholder_default')}
                          size="sm"
                          disabled={isProcessing}
                        />
                        {refDateRequired && (
                          <div className="space-y-1">
                            <label
                              htmlFor={`payment-ref-date-${id}`}
                              className="block text-xs font-medium text-gray-700"
                            >
                              {t('payment.reference_date')}
                              {refRequired && <span className="text-red-600 ms-0.5">*</span>}
                            </label>
                            <Input
                              id={`payment-ref-date-${id}`}
                              data-testid={`payment-ref-date-${id}`}
                              type="date"
                              value={refDateInputs[id] || ''}
                              onChange={(e) => setRefDateInputs((prev) => ({ ...prev, [id]: e.target.value }))}
                              size="sm"
                              disabled={isProcessing}
                            />
                          </div>
                        )}
                        {refError && (
                          <p role="alert" className="text-xs text-red-600">
                            {refError}
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
            <div className="flex items-center gap-3">
              <label htmlFor="payment-tip" className="w-24 font-medium">{t('payment.tip')}</label>
              <Input
                id="payment-tip"
                type="text"
                inputMode="decimal"
                value={tipInput}
                onChange={e => setTipInput(e.target.value)}
                placeholder={t('payment.amount_placeholder')}
                className="flex-1"
                size="sm"
                disabled={isProcessing}
              />
            </div>
            {canSelectCredit && <section>
              <label htmlFor="credit-account-search">{t('payment.charge_account')}</label>
              <Input type="search" id="credit-account-search" value={query} disabled={isProcessing}
                onChange={e => { setAccount(null); setAccounts([]); setQuery(e.target.value); }} />
              {creditStatus && <p role="status">{creditStatus}</p>}
              {accounts.map(row => <Button type="button" variant="outline" key={row.name} disabled={isProcessing}
                className="w-full h-auto whitespace-normal justify-start text-start" onClick={() => {
                  setAccount(row); setQuery(''); setAccounts([]);
                  if (newPaymentsTotal === balanceDue) setPaymentInputs({});
                }}>
                {row.customer_name} · {row.name} · {t('payment.account_balance')}: {formatCurrency(row.balance,row.currency)} · {t('payment.account_limit')}: {formatCurrency(row.limit,row.currency)} · {t('payment.account_available')}: {formatCurrency(row.available,row.currency)}
              </Button>)}
              {account && <p>{account.customer_name} · {account.name} <Button type="button" variant="ghost" disabled={isProcessing} onClick={() => setAccount(null)}>{t('common.cancel')}</Button></p>}
            </section>}
            {roomChargeUnconfigured && (
              <p
                role="status"
                data-testid="room-charge-setup-notice"
                className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
              >
                {t('payment.room_charge_not_configured')}
              </p>
            )}
            {roomChargeAmount > 0 && (
              <div className="space-y-2">
                <label htmlFor="room-charge-reference" className="text-sm font-medium">
                  {roomLookupMethod ? t('payment.room_posting') : t('payment.room_folio')}
                </label>
                <div className="flex gap-2">
                  <Input
                    id="room-charge-reference"
                    type="text"
                    value={roomNumber}
                    onChange={e => setRoomNumber(e.target.value)}
                    className="w-full"
                    size="sm"
                    disabled={isProcessing}
                  />
                  {roomLookupMethod && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={handleCheckRoom}
                      disabled={isProcessing || checkingRoom || !roomNumber.trim()}
                    >
                      {checkingRoom ? t('payment.checking_room') : t('payment.check_room')}
                    </Button>
                  )}
                </div>
                {roomLookupMethod && roomMatches.length > 1 && (
                  <div className="space-y-1.5" data-testid="room-match-picker">
                    <p className="text-xs font-medium text-amber-800">{t('payment.room_pick_guest')}</p>
                    {roomMatches.map((candidate, idx) => (
                      <Button
                        key={candidate.reservation || `${candidate.room_number}-${idx}`}
                        type="button"
                        variant="outline"
                        size="sm"
                        className="w-full h-auto whitespace-normal justify-start text-start"
                        onClick={() => {
                          const chosenRoom = candidate.room_number || roomNumber.trim();
                          setRoomNumber(chosenRoom);
                          applyRoomCandidate(candidate, chosenRoom);
                        }}
                      >
                        <span>
                          {t('payment.room_match', {
                            room: candidate.room_number || '',
                            guest: candidate.guest_name || '',
                            departure: candidate.departure || '',
                          })}
                          {candidate.limit !== undefined && (
                            <> · {t('payment.room_limit')}: {formatCurrency(candidate.limit, candidate.currency || currencyCode)}</>
                          )}
                          {candidate.balance !== undefined && (
                            <> · {t('payment.room_balance')}: {formatCurrency(candidate.balance, candidate.currency || currencyCode)}</>
                          )}
                        </span>
                      </Button>
                    ))}
                  </div>
                )}
                {roomLookupMethod && roomMatchActive && roomCheck && (
                  <div className="space-y-1.5">
                    <p role="status" className={`text-sm font-medium ${roomCheck.ok || roomApproval ? 'text-green-700' : 'text-red-700'}`}>
                      {roomCheck.ok ? roomCheck.label : roomCheck.error}
                    </p>
                    {(roomCheck.limit !== undefined || roomCheck.balance !== undefined) && (
                      <p className="text-xs text-gray-700" data-testid="room-charge-limits">
                        {roomCheck.limit !== undefined && (
                          <span>
                            {t('payment.room_limit')}: {formatCurrency(roomCheck.limit, roomCheck.currency || currencyCode)}
                          </span>
                        )}
                        {roomCheck.limit !== undefined && roomCheck.balance !== undefined && ' · '}
                        {roomCheck.balance !== undefined && (
                          <span>
                            {t('payment.room_balance')}: {formatCurrency(roomCheck.balance, roomCheck.currency || currencyCode)}
                          </span>
                        )}
                      </p>
                    )}
                    {roomCheck.overrideRequired && !roomApproval && (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => setActiveApproval('room_charge_over_limit')}
                      >
                        {t('payment.request_room_override')}
                      </Button>
                    )}
                    {roomApproval && (
                      <p className="text-xs font-medium text-emerald-700">
                        {t('payment.override_approved_by', { approver: roomApproval.approverName })}
                      </p>
                    )}
                  </div>
                )}
                {roomLookupMethod && !roomVerified && !roomMatchActive && (
                  <p className="text-sm text-amber-700">{t('payment.room_check_required')}</p>
                )}
              </div>
            )}
            <div className="flex justify-between mt-2 text-sm">
              <span className="font-medium">{t('payment.total_entered')}</span>
              <span className={`${isShort ? 'text-amber-700' : 'text-green-600'} font-semibold flex items-center gap-1`}>
                {formatMoney(newPaymentsTotal)} / {formatMoney(balanceDue)}
                {cashChangeDue > 0 && (
                  <span className="text-yellow-700 font-semibold">
                    <Coins className="inline w-4 h-4 ms-1 text-yellow-500" />
                    <span className="text-yellow-500 font-bold ms-1">{formatMoney(cashChangeDue)}</span>
                  </span>
                )}
              </span>
            </div>
            {isShort && (
              <p role="status" className="text-amber-700 text-sm font-medium">
                {t(canPayOnAccount ? 'payment.account_amount' : 'payment.short_by', { amount: formatMoney(shortfall) })}
              </p>
            )}
            {cashChangeDue > 0 && (
              <p
                data-testid={hasFxTender ? 'fx-local-change' : undefined}
                className="text-yellow-700 text-sm font-medium"
              >
                {hasFxTender && currencyCode
                  ? t('payment.fx_change_local', { currency: currencyCode, amount: formatMoney(cashChangeDue) })
                  : t('payment.change_due', { amount: formatMoney(cashChangeDue) })}
              </p>
            )}
            {nonCashExcess > 0.005 && (
              <p className={`${nonCashAllowsTip ? 'text-blue-700' : 'text-red-700'} text-sm font-medium`}>
                {t(nonCashAllowsTip ? 'payment.overtender_to_tip' : 'payment.non_cash_overtender', {
                  amount: formatMoney(nonCashExcess),
                })}
              </p>
            )}
            {overHaloTenders.length > 0 && (
              <div
                role="alert"
                className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 space-y-1"
              >
                {overHaloTenders.map((row) => (
                  <p key={row.mode_of_payment} className="text-sm font-medium text-amber-900">
                    {t('payment.halo_warning', {
                      mode: row.mode_of_payment,
                      amount: formatMoney(row.amount),
                      limit: formatCurrency(row.halo, approvalCurrency || undefined),
                    })}
                  </p>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Right Column - Order Summary and Pay Button */}
        <div className="md:w-1/2 shrink-0 md:shrink md:min-h-0 p-6 md:overflow-y-auto">
          {/* Error Message */}
          {error && (
            <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg">
              <p className="text-red-700 text-sm">{error}</p>
            </div>
          )}

          {/* Order Summary */}
          <div className="space-y-3 mb-6">
            <h3 className="text-lg font-semibold">{t('payment.order_summary')}</h3>
            {tableLabel && (
              <div className="flex justify-between text-sm">
                <span className="text-gray-600">{t('tables.table_name')}</span>
                <span className="font-medium">{tableLabel}</span>
              </div>
            )}
            <div className="space-y-2 text-sm">
              {/* Subtotal (Grand Total) */}
              <div className="flex justify-between">
                <span className="text-gray-600">{t('payment.subtotal')}</span>
                <span>{formatMoney(subtotal)}</span>
              </div>
              {taxPreview && (
                <div
                  data-testid="payment-tax-breakdown"
                  className="space-y-1 rounded-md bg-gray-50 px-2.5 py-2 text-xs text-gray-700"
                >
                  <div className="flex justify-between tabular-nums">
                    <span>{t('payment.net_total')}</span>
                    <span>{formatMoney(taxPreview.net_total)}</span>
                  </div>
                  {taxPreview.tax_rows.map((taxRow, idx) => (
                    <div
                      key={`${taxRow.description}-${idx}`}
                      className="flex justify-between tabular-nums"
                    >
                      <span className="truncate pe-2">
                        {formatPreviewTaxLabel(taxRow, taxPreview.net_total, t('payment.tax'))}
                      </span>
                      <span className="shrink-0">{formatMoney(taxRow.tax_amount)}</span>
                    </div>
                  ))}
                </div>
              )}
              {taxPreview?.efris_warnings && taxPreview.efris_warnings.length > 0 && (
                <div
                  data-testid="payment-efris-alert"
                  role="alert"
                  className="rounded-md border border-amber-300 bg-amber-50 p-2.5 text-xs text-amber-900"
                >
                  <div className="flex items-start gap-2">
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-700" aria-hidden />
                    <div className="space-y-1">
                      <p className="font-semibold">{t('payment.efris_warning_title')}</p>
                      {taxPreview.efris_warnings.map((w, idx) => (
                        <p key={`${w.item_code}-${idx}`}>{w.message}</p>
                      ))}
                    </div>
                  </div>
                </div>
              )}
              {/* Discount (read-only display of check & line discounts applied on the open check) */}
              {hasDiscount && (
                <div
                  data-testid="payment-discount-summary"
                  className="flex justify-between items-start gap-2 text-emerald-700"
                >
                  <div className="min-w-0">
                    <span className="font-medium">
                      {t('payment.discount')}
                      {(discountPercentage ?? 0) > 0 ? ` (${discountPercentage}%)` : ''}
                      {couponCode ? ` · ${couponCode}` : ''}
                    </span>
                    {discountReason && (
                      <p className="text-xs text-emerald-600">
                        {discountReason}
                        {discountNote ? ` — ${discountNote}` : ''}
                      </p>
                    )}
                  </div>
                  <span className="shrink-0 font-semibold tabular-nums">-{formatMoney(totalDiscount)}</span>
                </div>
              )}
              {/* Adjustment (if any) */}
              {showFinalAdjustment && (
                <div className="flex justify-between text-blue-600">
                  <span>{t('payment.adjustment')}</span>
                  <span>{roundedFinalAdjustment > 0 ? '+' : ''}{formatMoney(roundedFinalAdjustment)}</span>
                </div>
              )}
              {/* Tip (if any) */}
              {tipAmount > 0 && (
                <div className="flex justify-between">
                  <span className="text-gray-600">{t('payment.tip')}</span>
                  <span>{formatMoney(tipAmount)}</span>
                </div>
              )}
              {/* Final Total (including tip) */}
              <div className="border-t pt-2">
                <div className="flex justify-between font-semibold text-lg">
                  <span>{t('payment.total')}</span>
                  <span>{formatMoney(amountDue)}</span>
                </div>
              </div>
              {receivedTotal > 0 && (
                <>
                  <div className="flex justify-between text-emerald-700">
                    <span>{t('payment.already_received')}</span>
                    <span>-{formatMoney(receivedTotal)}</span>
                  </div>
                  <div className="border-t pt-2">
                    <div className="flex justify-between font-semibold text-lg" data-testid="balance-due">
                      <span>{t('payment.balance_due')}</span>
                      <span>{formatMoney(balanceDue)}</span>
                    </div>
                  </div>
                </>
              )}
            </div>
          </div>

          {/* Print Receipt Checkbox (always shown; locked checked when printing is required) */}
          <div className="mb-4 flex items-center gap-2">
            <input
              id="print-invoice-checkbox"
              type="checkbox"
              checked={effectivePrintInvoice}
              disabled={printRequired || isProcessing}
              onChange={(e) => {
                if (!printRequired) setPrintInvoice(e.target.checked);
              }}
              className={`h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500 ${
                printRequired ? 'cursor-not-allowed opacity-75' : 'cursor-pointer'
              }`}
            />
            <label
              htmlFor="print-invoice-checkbox"
              className={`text-sm font-medium text-gray-700 select-none flex items-center gap-1.5 ${
                printRequired ? 'cursor-not-allowed opacity-75' : 'cursor-pointer'
              }`}
            >
              <Printer className="w-4 h-4 text-gray-500" />
              {t('payment.print_invoice')}
            </label>
          </div>

          {/* Payment Button */}
          <Button
            data-testid="payment-submit-btn"
            onClick={handlePayment}
            disabled={paymentDisabled}
            variant={paymentDisabled ? "secondary" : "default"}
            className="w-full"
          >
            {isProcessing ? t('payment.processing') : t('payment.pay_button', { amount: formatMoney(newPaymentsTotal > 0 ? newPaymentsTotal : balanceDue) })}
          </Button>
        </div>
        {pendingFxOverride && (
          <ManagerApproval
            open={true}
            posProfile={posProfile}
            privilege="fx_rate_override"
            title={t('payment.fx_override_rate')}
            summaryLines={[
              `${pendingFxOverride.mode}: 1 ${pendingFxOverride.currency} = ${pendingFxOverride.rate} ${currencyCode}`,
            ]}
            reference={{ doctype: 'POS Invoice', name: invoice }}
            claim={{
              action: 'pos_fx_rate_override',
              mode_of_payment: pendingFxOverride.mode,
              from_currency: pendingFxOverride.currency,
              to_currency: currencyCode,
              override_rate: pendingFxOverride.rate,
            }}
            onApproved={(result: ManagerApprovalResult) => {
              setFxOverrides((prev) => ({
                ...prev,
                [pendingFxOverride.mode]: {
                  rate: pendingFxOverride.rate,
                  token: result.token,
                  reason: result.reason,
                },
              }));
              setPendingFxOverride(null);
            }}
            onCancel={() => setPendingFxOverride(null)}
          />
        )}
      </DialogContent>
      {activeApproval === 'room_charge_over_limit' && roomCheck && (() => {
        const roomClaimCurrency = roomCheck.currency || currencyCode;
        const roomIsFx = Boolean(
          roomClaimCurrency && currencyCode && roomClaimCurrency.toUpperCase() !== currencyCode.toUpperCase(),
        );
        const roomClaimAmount =
          roomCheck.chargeAmount !== undefined && roomCheck.chargeAmount !== null
            ? roundToCurrency(Number(roomCheck.chargeAmount), roomClaimCurrency)
            : roomIsFx && Number(order?.conversion_rate) > 0
              ? roundToCurrency(roomChargeAmount * Number(order?.conversion_rate), roomClaimCurrency)
              : roundToCurrency(roomChargeAmount, roomClaimCurrency);
        const amountLabel = roomIsFx
          ? `${formatMoney(roomChargeAmount)} (${formatCurrency(roomClaimAmount, roomClaimCurrency)})`
          : formatMoney(roomChargeAmount);
        return (
          <ManagerApproval
            open={true}
            posProfile={posProfile}
            privilege="room_charge_over_limit"
            summaryLines={[
              roomCheck.label || `${roomCheck.roomNumber || roomNumber} · ${roomCheck.guestName || ''}`,
              roomCheck.reason || t('payment.room_over_limit'),
              t('manager_approval.amount_line', { amount: amountLabel }),
            ]}
            reference={{ doctype: 'POS Invoice', name: invoice }}
            claim={{
              reservation: roomCheck.reservation || '',
              room: roomCheck.roomNumber || roomNumber.trim(),
              amount: roomClaimAmount,
              currency: roomClaimCurrency,
            }}
            amount={roomClaimAmount}
            requireReason={true}
            onApproved={handleManagerApproved}
            onCancel={() => setActiveApproval(null)}
          />
        );
      })()}
      {activeApproval === 'on_account_over_limit' && (
        <ManagerApproval
          open={true}
          posProfile={posProfile}
          privilege="on_account_over_limit"
          summaryLines={[
            account ? `${account.customer_name} · ${account.name}` : customer,
            t('payment.account_over_limit_notice'),
            t('manager_approval.amount_line', {
              amount: isFxAccount
                ? `${formatMoney(shortfall)} (${formatCurrency(proposedInAccountCurrency, accountCurrency)})`
                : formatMoney(shortfall),
            }),
          ]}
          reference={{ doctype: 'POS Invoice', name: invoice }}
          claim={{
            customer: account?.name || customer,
            company: storePosProfile?.company || '',
            proposed: proposedInAccountCurrency,
            limit: roundToCurrency(Number(account?.limit || 0), accountCurrency),
            currency: accountCurrency,
          }}
          amount={proposedInAccountCurrency}
          requireReason={true}
          onApproved={handleManagerApproved}
          onCancel={() => setActiveApproval(null)}
        />
      )}
      {showHaloApproval && overHaloTenders.length > 0 && (
        <ManagerApproval
          open={showHaloApproval}
          posProfile={posProfile}
          privilege="over_halo_tender"
          summaryLines={overHaloTenders.map((row) =>
            t('payment.halo_warning', {
              mode: row.mode_of_payment,
              amount: formatMoney(row.amount),
              limit: formatCurrency(row.halo, approvalCurrency || undefined),
            }),
          )}
          reference={{ doctype: 'POS Invoice', name: invoice }}
          amount={Math.max(...overHaloTenders.map((row) => row.approvalAmount))}
          claim={{
            action: 'over_halo_tender',
            invoice,
            pos_profile: posProfile,
            tenders: overHaloTenders.map((row) => ({
              mode_of_payment: row.mode_of_payment,
              amount: row.amount,
            })),
          }}
          onApproved={handleHaloApproved}
          onCancel={() => setShowHaloApproval(false)}
        />
      )}
    </Dialog>
  );
};

export default PaymentDialog; 