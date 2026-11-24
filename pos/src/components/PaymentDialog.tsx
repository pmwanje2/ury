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
import { getPosFxRates, type PaymentModeConfig, type PosFxRateInfo } from '../lib/payment-api';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';
import { isConfiguredDefaultCustomer, profileLacksDefaultCustomerConfig } from '@ury/core/default-customer';

const MAX_REF_PATTERN_LENGTH = 128;

function stepForCurrency(code?: string | null): string {
  const digits = currencyDecimalPlaces(code);
  if (digits <= 0) return '1';
  if (digits === 3) return '0.001';
  return '0.01';
}

function matchesReferencePattern(value: string, pattern?: string | null): boolean {
  const raw = (pattern || '').trim();
  if (!raw) return true;
  if (raw.length > MAX_REF_PATTERN_LENGTH) return false;
  if (/\([^)]*[+*?][^)]*\)[+*?{]|[+*?]\s*[+*?]|\\[1-9]/.test(raw)) return false;
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

type CreditAccount = {name: string; customer_name: string; balance: number; limit: number; available: number; currency: string};

interface PaymentDialogProps {
  order?: (Pick<POSInvoice, 'currency' | 'custom_merged_pos_invoice' | 'ury_client_order_id' | 'custom_charged_tip' | 'payments' | 'room_number'> & { invoice_printed?: string | number }) | null;
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
  const [roomCheck, setRoomCheck] = useState<{
    room: string; label?: string; ok: boolean; error?: string;
  } | null>(null);
  const [checkingRoom, setCheckingRoom] = useState(false);

  useEffect(() => {
    fetchPaymentModes();
  }, [fetchPaymentModes]);

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

  // Room-charge tenders are those the POS Profile reports as posting to Guest Ledger Clearing.
  const roomChargeModes = storePosProfile?.room_charge_modes || [];
  const roomChargeUnconfigured = Boolean(storePosProfile?.room_charge_unconfigured) && roomChargeModes.length === 0;
  const roomChargeAmount = roomChargeModes
    .reduce((sum: number, mode: string) => sum + (parseFloat(paymentInputs[mode] || '') || 0), 0);
  const roomVerified = !roomLookupMethod || !(roomChargeAmount > 0)
    || (!!roomCheck?.ok && roomCheck.room === roomNumber.trim());
  const roomFields = roomChargeAmount > 0 ? { room_number: roomNumber } : {};
  const rawShortfall = amountDue - rawTotalTendered;
  const shortfall = Math.max(0, roundToCurrency(rawShortfall, currencyCode));
  const isShort = rawShortfall > 0.005;
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

  const handleCheckRoom = async () => {
    const room = roomNumber.trim();
    if (!room || !roomLookupMethod) return;
    setCheckingRoom(true);
    setRoomCheck(null);
    try {
      const res = await call.get(roomLookupMethod, { pos_profile: posProfile, room });
      const match = (res as { message?: { room_number?: string; guest_name?: string; departure?: string; no_post?: unknown } } | null)?.message;
      if (!match || !match.guest_name) {
        setRoomCheck({ room, ok: false, error: t('payment.room_check_failed') });
      } else if (match.no_post && Number(match.no_post) !== 0) {
        setRoomCheck({ room, ok: false, error: t('payment.room_no_post') });
      } else {
        setRoomCheck({ room, ok: true, label: t('payment.room_match', {
          room: match.room_number || room, guest: match.guest_name, departure: match.departure || '',
        }) });
      }
    } catch (err) {
      setRoomCheck({ room, ok: false, error: parseFrappeError(err, t('payment.room_check_failed')) });
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

  const handlePayment = async () => {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setIsProcessing(true);
    setError(null);
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
        });

        // Step 2: Submit to settle. Submission fiscalises the invoice (EFRIS),
        // so the customer receipt prints only after it (Simphony: the guest
        // check is pro forma; the receipt prints at tender).
        await call.post('ury.ury.doctype.ury_order.ury_order.submit_order_invoice', {
          invoice,
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
            ...(ids && { client_order_id: ids.clientOrderId, round_id: ids.roundId }),
          }),
        });
        if (settlement.offline) {
          showToast.success(t('offline.saved'));
          onClose();
          clearSelectedOrder();
          return;
        }
      }
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
      setError(parseFrappeError(err, t('errors.payment_failed')));
    } finally {
      submittingRef.current = false;
      setIsProcessing(false);
    }
  };

  return (
    <Dialog open={true} onOpenChange={onClose}>
      <DialogContent variant="xlarge" className="bg-white w-full max-w-4xl max-h-dialog-max-h flex flex-col md:flex-row p-0" showCloseButton={false}>
        {/* Left Column - Discount and Payment Mode */}
        <div className="md:w-1/2 p-6 border-b md:border-b-0 md:border-e border-border overflow-y-auto">
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
                {roomLookupMethod && roomCheck && roomCheck.room === roomNumber.trim() && (
                  <p role="status" className={`text-sm font-medium ${roomCheck.ok ? 'text-green-700' : 'text-red-700'}`}>
                    {roomCheck.ok ? roomCheck.label : roomCheck.error}
                  </p>
                )}
                {roomLookupMethod && !roomVerified && !(roomCheck && roomCheck.room === roomNumber.trim()) && (
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
          </div>
        </div>

        {/* Right Column - Order Summary and Pay Button */}
        <div className="md:w-1/2 p-6 overflow-y-auto">
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
    </Dialog>
  );
};

export default PaymentDialog; 