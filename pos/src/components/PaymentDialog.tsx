import React, { useState, useEffect, useRef } from 'react';
import { X, Percent, Coins, Printer } from 'lucide-react';
import { usePOSStore } from '../store/pos-store';
import { formatCurrency, parseFrappeError } from '@ury/core';
import { Button, Input, Dialog, DialogContent, showToast } from '@ury/ui';
import { call } from '@ury/core';
import { t } from '../i18n';
import { assertOfflineActionAllowed, submitSettlementOfflineAware } from '../offline/cashier';
import { canCaptureOffline } from '../offline/outbox';
import { useRootStore } from '../store/root-store';
import { printOrder } from '../lib/print';
import { queuePendingFiscalReprint, resolveEfrisPrintWaitSeconds, waitForEfrisFdn } from '../lib/efris-print';
import { resolvePrintFormat, type POSInvoice, type POSInvoicePayment } from '../lib/invoice-api';
import { isConfiguredDefaultCustomer, profileLacksDefaultCustomerConfig } from '@ury/core/default-customer';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';


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
  order?: (Pick<POSInvoice, 'currency' | 'custom_merged_pos_invoice' | 'ury_client_order_id' | 'payments' | 'room_number' | 'custom_charged_tip'> & { invoice_printed?: string | number }) | null;
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
  discountAmount
}) => {
  const user = useRootStore((state) => state.user);
  const { paymentModes, fetchPaymentModes, posProfile: storePosProfile, currency: storeCurrency } = usePOSStore();
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
  const [discountType] = useState<'percentage'>('percentage'); // Only percentage now
  
  // Only send a percentage discount the server already carries or previewed via Apply;
  // never derive a synthetic percentage from a fixed discount_amount or send unapplied typing.
  const initialAppliedPct = discountPercentage && discountPercentage > 0 ? discountPercentage : null;
  const [discountValue, setDiscountValue] = useState<string>(initialAppliedPct !== null ? String(initialAppliedPct) : '');
  const [appliedDiscountPct, setAppliedDiscountPct] = useState<number | null>(initialAppliedPct);
  const [appliedDiscount, setAppliedDiscount] = useState<number>(discountAmount || 0); // Only tracking transaction discount!
  const [paymentInputs, setPaymentInputs] = useState<{ [mode: string]: string }>({});
  // Totals the server priced for a discount applied in this dialog; null keeps the order's own.
  const [serverTotals, setServerTotals] = useState<{ grand_total: number; rounded_total: number } | null>(null);
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

  // baseTotal represents the pre-tip amount before any invoice-level discount
  const baseTotal = preTipGrandTotal + (discountAmount || 0);

  const handleApplyDiscount = async () => {
    try {
      await assertOfflineActionAllowed('discount');
    } catch (error) {
      setError(error instanceof Error ? error.message : t('errors.invalid_discount'));
      return;
    }
    const trimmed = discountValue.trim();
    const isClearToZero = appliedDiscountPct !== null && (trimmed === '' || parseFloat(trimmed) === 0);
    const value = isClearToZero ? 0 : parseFloat(trimmed);
    if (!isClearToZero && (isNaN(value) || value <= 0)) {
      setError(t('errors.invalid_discount'));
      return;
    }
    if (value > 100) {
      setError(t('errors.discount_exceeds_max'));
      return;
    }
    try {
      const { message } = await call.get<{ message: { grand_total: number; rounded_total: number } }>(
        'ury.ury.doctype.ury_order.ury_order.preview_settlement_totals',
        { invoice, pos_profile: posProfile, additionalDiscount: value },
      );
      setServerTotals(message);
      setAppliedDiscountPct(isClearToZero ? null : value);
      if (isClearToZero) {
        setDiscountValue('');
      }
      setAppliedDiscount(Math.max(0, roundToCurrency(baseTotal - message.grand_total, currencyCode)));
    } catch (err) {
      setError(parseFrappeError(err, t('errors.invalid_discount')));
      return;
    }
    setError(null);
  };

  // Order summary logic
  const subtotal = baseTotal;
  const adjustment = preTipRoundedTotal - preTipGrandTotal;
  const roundedAdjustment = roundToCurrency(adjustment, currencyCode);
  const showAdjustment = Math.abs(roundedAdjustment) > 0.001;
  const totalDiscount = appliedDiscount;
  const discountedTotal = Math.max(0, subtotal - totalDiscount);
  // The server's total: its rounded total, or its grand total where the profile does not round.
  const finalTotal = serverTotals
    ? (serverTotals.rounded_total || serverTotals.grand_total)
    : (preTipRoundedTotal || preTipGrandTotal);
  // A charged or cash tip is owed to the server: the tenders cover it, it is
  // never change, and the server books it to the till's Tips Payable account.
  const tipAmount = parseFloat(tipInput) || 0;
  const tipInvalid = tipAmount < 0;
  const amountDue = roundToCurrency(finalTotal + Math.max(0, tipAmount), currencyCode);
  const balanceDue = Math.max(0, roundToCurrency(amountDue - receivedTotal, currencyCode));
  // Sent only when there is a tip, so an untipped settlement is unchanged.
  const tipFields = tipAmount > 0 ? { tip: tipAmount } : {};

  // Calculate split payment total for new tenders taken now
  const newPayments = paymentModes
    .map((mode: any) => {
      const id = typeof mode === 'string' ? mode : mode.id;
      const amount = parseFloat(paymentInputs[id] || '');
      return amount > 0 ? { mode_of_payment: id, amount } : null;
    })
    .filter(Boolean) as Array<{ mode_of_payment: string; amount: number }>;
  const rawNewPaymentsTotal = newPayments.reduce((sum, p) => sum + p.amount, 0);
  const newPaymentsTotal = roundToCurrency(rawNewPaymentsTotal, currencyCode);
  const rawTotalTendered = receivedTotal + rawNewPaymentsTotal;
  const totalTendered = roundToCurrency(rawTotalTendered, currencyCode);
  const receivedPaymentPayloads = receivedPayments.map((row) => ({
    mode_of_payment: row.mode_of_payment,
    amount: Number(row.amount),
    ...(row.reference_no ? { reference_no: row.reference_no } : {}),
    ...(row.reference_date ? { reference_date: row.reference_date } : {}),
    ...(row.clearance_date ? { clearance_date: row.clearance_date } : {}),
  }));
  const combinedPayments = [...receivedPaymentPayloads, ...newPayments];
  const modeMeta = storePosProfile?.payment_mode_meta ?? {};
  const isCashMode = (mode: string) => {
    const metaType = modeMeta[mode]?.type;
    return metaType ? metaType === 'Cash' : /^cash$/i.test(mode);
  };
  const excess = rawTotalTendered - amountDue;
  const hasExcess = excess > 0.005;
  const cashTendered = combinedPayments.reduce(
    (sum, p) => (isCashMode(p.mode_of_payment) ? sum + p.amount : sum),
    0,
  );
  const nonCashModes = combinedPayments
    .filter((p) => !isCashMode(p.mode_of_payment))
    .map((p) => p.mode_of_payment);
  const nonCashExcess = hasExcess ? Math.max(0, roundToCurrency(excess - cashTendered, currencyCode)) : 0;
  const cashChangeDue =
    hasExcess && nonCashExcess <= 0.005
      ? Math.max(0, roundToCurrency(Math.min(excess, cashTendered), currencyCode))
      : 0;
  const nonCashAllowsTip =
    nonCashExcess > 0.005 &&
    nonCashModes.length > 0 &&
    nonCashModes.every((mode) => modeMeta[mode]?.overtender_rule === 'Post to Charged Tip');

  // High Amount Lockout (HALO): sum per mode_of_payment to prevent split-row evasion.
  const overHaloTenders = (() => {
    const totalsByMode = new Map<string, number>();
    for (const row of combinedPayments) {
      if (row.amount <= 0) continue;
      totalsByMode.set(
        row.mode_of_payment,
        roundToCurrency((totalsByMode.get(row.mode_of_payment) || 0) + row.amount, currencyCode),
      );
    }
    const exceeded: Array<{ mode_of_payment: string; amount: number; halo: number }> = [];
    for (const mode of Array.from(totalsByMode.keys()).sort()) {
      const amount = totalsByMode.get(mode) || 0;
      const halo = roundToCurrency(Number(modeMeta[mode]?.halo_max_amount) || 0, currencyCode);
      if (halo > 0 && amount > halo + 1e-6) {
        exceeded.push({ mode_of_payment: mode, amount, halo });
      }
    }
    return exceeded;
  })();

  // Room-charge tenders are those the POS Profile reports as posting to Guest Ledger Clearing.
  const roomChargeModes = storePosProfile?.room_charge_modes || [];
  const roomChargeUnconfigured = Boolean(storePosProfile?.room_charge_unconfigured) && roomChargeModes.length === 0;
  const trimmedDiscount = discountValue.trim();
  const typedDiscount = trimmedDiscount === '' ? null : parseFloat(trimmedDiscount);
  const hasUnappliedDiscount =
    appliedDiscountPct !== null
      ? typedDiscount === null || typedDiscount !== appliedDiscountPct
      : trimmedDiscount !== '';
  const roomChargeAmount = roomChargeModes
    .reduce((sum: number, mode: string) => sum + (parseFloat(paymentInputs[mode] || '') || 0), 0);
  const roomVerified = !roomLookupMethod || !(roomChargeAmount > 0)
    || (!!roomCheck?.ok && roomCheck.room === roomNumber.trim());
  const roomFields = roomChargeAmount > 0 ? { room_number: roomNumber } : {};
  const rawShortfall = amountDue - rawTotalTendered;
  const shortfall = Math.max(0, roundToCurrency(rawShortfall, currencyCode));
  const isShort = rawShortfall > 0.005;
  const changeDue = Math.max(0, roundToCurrency(rawTotalTendered - amountDue, currencyCode));
  // A named (non-default) customer on a profile with allow_partial_payment
  // may settle short; the balance stays owed on the invoice. Profile, order-type
  // and profile-level default customers are not named accounts; a profile that
  // predates the default-customer keys fails closed. The submitted payment list
  // keeps one native mode row, at zero when nothing was tendered.
  const accountPaymentMode = paymentModes
    .map((mode: string | { id: string }) => (typeof mode === 'string' ? mode : mode.id))
    .find(Boolean);
  const canPayOnAccount = canSelectCredit && isShort && !!accountPaymentMode
    && storePosProfile?.name === posProfile
    && storePosProfile.allow_partial_payment === 1 && !!storePosProfile.customer
    && !!customer.trim() && customer !== storePosProfile.customer
    && !profileLacksDefaultCustomerConfig(storePosProfile)
    && !isConfiguredDefaultCustomer(customer, storePosProfile);
  // Both rules hold: a Room Charge waits for a verified room, and only a named
  // account may settle short.
  const paymentDisabled = isProcessing || !roomVerified || tipInvalid
    || ((combinedPayments.length === 0 || isShort) && !canPayOnAccount);
  const settlementPayments = combinedPayments.length === 0 && canPayOnAccount
    ? [{ mode_of_payment: accountPaymentMode, amount: 0 }]
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

  // The POS Profile payment row marked Default is prefilled with the remaining balance due.
  const defaultPaymentMode = storePosProfile?.default_payment_mode || '';

  useEffect(()=>{
    const defaultPaymentModePresent=paymentModes.find((mode: string | { id: string })=>(typeof mode === 'string' ? mode : mode.id)===defaultPaymentMode)
    //only one payment mode should be present, then autofill the remaining balance due, if not do not fill
    const otherPaymentModesNotEntered=Object.keys(paymentInputs).length<=1;
    if(paymentModes && defaultPaymentMode && defaultPaymentModePresent && otherPaymentModesNotEntered){
      if(balanceDue > 0){
        setPaymentInputs((prev)=>({
          ...prev,
          [defaultPaymentMode]:String(balanceDue)
        }))
      } else if (receivedTotal > 0) {
        setPaymentInputs((prev)=>({
          ...prev,
          [defaultPaymentMode]:''
        }))
      }
    }
  },[balanceDue,receivedTotal,paymentModes,defaultPaymentMode])

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

  const executePayment = async (haloApprovalToken?: string, haloApprovalReason?: string) => {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setIsProcessing(true);
    setError(null);
    const haloFields = {
      ...(haloApprovalToken ? { halo_approval_token: haloApprovalToken } : {}),
      ...(haloApprovalReason ? { halo_approval_reason: haloApprovalReason } : {}),
    };
    try {
      await assertOfflineActionAllowed('online invoice settlement');
      const wasAlreadyPrinted = order ? String(order.invoice_printed) !== '0' : false;
      if (!enablePaymentWithoutPrinting && !wasAlreadyPrinted) {
        setError(t('errors.please_print_first'));
        showToast.error(t('errors.please_print_first'));
        return;
      }

      const offlineCapable = canCaptureOffline(
        storePosProfile ?? { name: posProfile },
        user ?? { name: 'Guest', roles: [] }
      );
      const trimmedRoomNumber = roomChargeAmount > 0 ? roomNumber.trim() : undefined;

      if (effectivePrintInvoice && !offlineCapable) {
        // Step 1: Save the draft invoice with discount and payment modes
        await call.post('ury.ury.doctype.ury_order.ury_order.make_invoice', {
          additionalDiscount: appliedDiscountPct,
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
          ...haloFields,
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
          amount: totalTendered, due: amountDue, discount: appliedDiscount, tip: tipAmount,
          modeOfPayment: receivedPayments.length === 0 && newPayments.length === 1 ? newPayments[0]?.mode_of_payment ?? '' : '',
          roomNumber: trimmedRoomNumber,
          onlineSettle: (ids) => call.post('ury.ury.doctype.ury_order.ury_order.make_invoice', {
            additionalDiscount: appliedDiscountPct,
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
            ...haloFields,
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

  const handlePayment = () => {
    if (overHaloTenders.length > 0) {
      setError(null);
      setShowHaloApproval(true);
      return;
    }
    void executePayment();
  };

  const handleHaloApproved = (result: ManagerApprovalResult) => {
    setShowHaloApproval(false);
    void executePayment(result.token, result.reason);
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

          {/* Discount Section (conditional) */}
          {storePosProfile?.enable_discount === 1 && (
            <div className="space-y-4 mb-6">
              <h3 className="text-lg font-semibold flex items-center gap-2">
                <Percent className="w-5 h-5" />
                {t('payment.apply_discount')}
              </h3>
              <div className="flex gap-2">
                <Input
                  type="number"
                  value={discountValue}
                  onChange={(e) => setDiscountValue(e.target.value)}
                  placeholder={t('payment.discount_placeholder')}
                  size="sm"
                  className="flex-1"
                />
                <Button
                  onClick={handleApplyDiscount}
                  variant="default"
                  size="sm"
                >
                  {t('common.apply')}
                </Button>
              </div>
              {hasUnappliedDiscount && (
                <p role="status" className="text-xs text-amber-700">
                  {t('payment.discount_not_applied')}
                </p>
              )}
            </div>
          )}

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
                return (
                  <div key={id} className="flex items-center gap-3">
                    <span className="w-24 font-medium">{typeof mode === 'string' ? mode : mode.name}</span>
                    <Input
                      type="number"
                      min="0"
                      step="0.01"
                      value={paymentInputs[id] || ''}
                      onChange={e => setPaymentInputs(inputs => ({ ...inputs, [id]: e.target.value }))}
                      onFocus={() => handlePaymentInputFocus(id)}
                      placeholder={t('payment.amount_placeholder')}
                      className="flex-1"
                      size="sm"
                      disabled={isProcessing}
                    />
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
                {cashChangeDue > 0.005 && (
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
            {cashChangeDue > 0.005 && (
              <p className="text-yellow-700 text-sm font-medium">
                {t('payment.change_due', { amount: formatMoney(cashChangeDue) })}
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
                      limit: formatMoney(row.halo),
                    })}
                  </p>
                ))}
              </div>
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
              {/* Discount */}
              {appliedDiscount > 0 && (
                <div className="flex justify-between text-green-600">
                  <span>{t('payment.discount')}</span>
                  <span>-{formatMoney(appliedDiscount)}</span>
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
            onClick={handlePayment}
            disabled={paymentDisabled}
            variant={paymentDisabled ? "secondary" : "default"}
            className="w-full"
          >
            {isProcessing ? t('payment.processing') : t('payment.pay_button', { amount: formatMoney(newPaymentsTotal > 0 ? newPaymentsTotal : balanceDue) })}
          </Button>
        </div>
      </DialogContent>
      {showHaloApproval && overHaloTenders.length > 0 && (
        <ManagerApproval
          open={showHaloApproval}
          posProfile={posProfile}
          privilege="over_halo_tender"
          summaryLines={overHaloTenders.map((row) =>
            t('payment.halo_warning', {
              mode: row.mode_of_payment,
              amount: formatMoney(row.amount),
              limit: formatMoney(row.halo),
            }),
          )}
          reference={{ doctype: 'POS Invoice', name: invoice }}
          amount={Math.max(...overHaloTenders.map((row) => row.amount))}
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