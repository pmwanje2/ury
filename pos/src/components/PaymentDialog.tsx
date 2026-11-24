import React, { useState, useEffect } from 'react';
import { X, Percent, Coins, Printer } from 'lucide-react';
import { usePOSStore } from '../store/pos-store';
import { formatCurrency, parseFrappeError } from '@ury/core';
import { Button, Input, Dialog, DialogContent, showToast } from '@ury/ui';
import { call } from '@ury/core';
import { t } from '../i18n';
import { assertOfflineActionAllowed, submitSettlementOfflineAware } from '../offline/cashier';
import { useRootStore } from '../store/root-store';
import { printOrder } from '../lib/print';
import { resolvePrintFormat, type POSInvoice, type POSInvoicePayment } from '../lib/invoice-api';
import { isConfiguredDefaultCustomer, profileLacksDefaultCustomerConfig } from '@ury/core/default-customer';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';
import { isAuthRequiredError } from '../lib/override-api';


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
  reason?: string | null;
}

interface ApprovalGrant {
  token: string;
  approverName: string;
  reason: string;
}

interface PaymentDialogProps {
  order?: (Pick<POSInvoice, 'custom_merged_pos_invoice' | 'ury_client_order_id' | 'payments' | 'room_number'> & { invoice_printed?: string | number }) | null;
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
  const currencyCode = storePosProfile?.currency || storeCurrency || '';
  const formatMoney = (amount: number) =>
    currencyCode ? formatCurrency(amount, currencyCode) : formatCurrency(amount);
  const enablePaymentWithoutPrinting = Boolean(
    Number(storePosProfile?.enable_payment_without_printing) === 1 ||
    Number((storePosProfile as any)?.custom_enable_payment_without_printing) === 1
  );
  const [isProcessing, setIsProcessing] = useState(false);
  const [printInvoice, setPrintInvoice] = useState(enablePaymentWithoutPrinting);
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
  const [roomCheck, setRoomCheck] = useState<RoomCheckState | null>(null);
  const [roomMatches, setRoomMatches] = useState<RoomMatchCandidate[]>([]);
  const [checkingRoom, setCheckingRoom] = useState(false);
  const [roomApproval, setRoomApproval] = useState<ApprovalGrant | null>(null);
  const [onAccountApproval, setOnAccountApproval] = useState<ApprovalGrant | null>(null);
  const [activeApproval, setActiveApproval] = useState<'room_charge_over_limit' | 'on_account_over_limit' | null>(null);

  useEffect(() => {
    fetchPaymentModes();
  }, [fetchPaymentModes]);

  // baseTotal represents the amount before any invoice-level discount (like pricing rule or manual discount)
  const baseTotal = grandTotal + (discountAmount || 0);

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
  const adjustment = roundedTotal - grandTotal;
  const roundedAdjustment = roundToCurrency(adjustment, currencyCode);
  const showAdjustment = Math.abs(roundedAdjustment) > 0.001;
  const totalDiscount = appliedDiscount;
  const discountedTotal = Math.max(0, subtotal - totalDiscount);
  // The server's total: its rounded total, or its grand total where the profile does not round.
  const finalTotal = serverTotals
    ? (serverTotals.rounded_total || serverTotals.grand_total)
    : (roundedTotal || grandTotal);
  const balanceDue = Math.max(0, roundToCurrency(finalTotal - receivedTotal, currencyCode));

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
  const rawShortfall = finalTotal - rawTotalTendered;
  const shortfall = Math.max(0, roundToCurrency(rawShortfall, currencyCode));
  const isShort = rawShortfall > 0.005;
  const changeDue = Math.max(0, roundToCurrency(rawTotalTendered - finalTotal, currencyCode));
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
  const onAccountOverLimit = canPayOnAccount && !!account && shortfall > Number(account.available) + 0.005;

  useEffect(() => {
    setRoomApproval(null);
  }, [roomNumber, roomChargeAmount]);

  useEffect(() => {
    setOnAccountApproval(null);
  }, [customer, shortfall]);

  // Both rules hold: a Room Charge waits for a verified room, and only a named
  // account may settle short.
  const paymentDisabled = isProcessing || !roomVerified
    || ((combinedPayments.length === 0 || isShort) && !canPayOnAccount);
  const settlementPayments = combinedPayments.length === 0 && canPayOnAccount
    ? [{ mode_of_payment: accountPaymentMode, amount: 0 }]
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

  const executePayment = async (overrideAccountGrant?: ApprovalGrant | null) => {
    setIsProcessing(true);
    setError(null);
    const activeAccountApproval = overrideAccountGrant !== undefined ? overrideAccountGrant : onAccountApproval;
    const accountApprovalFields = activeAccountApproval
      ? {
          on_account_approval: activeAccountApproval.token,
          on_account_reason: activeAccountApproval.reason,
        }
      : {};
    try {
      await assertOfflineActionAllowed('online invoice settlement');
      const wasAlreadyPrinted = order ? String(order.invoice_printed) !== '0' : false;
      if (!enablePaymentWithoutPrinting && !wasAlreadyPrinted) {
        setError(t('errors.please_print_first'));
        showToast.error(t('errors.please_print_first'));
        setIsProcessing(false);
        return;
      }

      if (printInvoice) {
        // Step 1: Save draft invoice with discount and payment modes without submitting
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
          ...roomFields,
          ...accountApprovalFields,
        });

        // Step 2: Print invoice with discount and payment modes included
        if (storePosProfile) {
          try {
            await printOrder({
              orderId: invoice,
              posProfile: storePosProfile,
              printFormat: resolvePrintFormat(order || {}, storePosProfile.print_format),
            });
          } catch (printErr: any) {
            console.error('Print failed:', printErr);
            const reason = printErr?.message || parseFrappeError(printErr) || 'Unknown error';
            setError(`${t('errors.print_failed', { reason })} ${t('payment.payment_not_processed')}`);
            showToast.error(`${t('errors.print_failed', { reason })} ${t('payment.payment_not_processed')}`);
            setIsProcessing(false);
            return;
          }
        }

        // Step 3: Print succeeded! Submit the invoice to finalize payment
        await call.post('ury.ury.doctype.ury_order.ury_order.submit_order_invoice', {
          invoice,
          ...accountApprovalFields,
        });
      } else {
        const settlement = await submitSettlementOfflineAware({
          profile: storePosProfile ?? { name: posProfile }, user: user ?? { name: 'Guest', roles: [] },
          invoice, customer, table, cashier, owner, clientOrderId: order?.ury_client_order_id,
          amount: totalTendered, due: finalTotal, discount: appliedDiscount,
          modeOfPayment: receivedPayments.length === 0 && newPayments.length === 1 ? newPayments[0]?.mode_of_payment ?? '' : '',
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
            ...roomFields,
            ...accountApprovalFields,
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
      setIsProcessing(false);
    }
  };

  const handlePayment = async () => {
    if (onAccountOverLimit && !onAccountApproval) {
      setActiveApproval('on_account_over_limit');
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
      void executePayment(grant);
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
                      className="flex items-center justify-between text-sm text-emerald-900"
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
                {changeDue > 0 && (
                  <span className="text-yellow-700 font-semibold">
                    <Coins className="inline w-4 h-4 ms-1 text-yellow-500" />
                    <span className="text-yellow-500 font-bold ms-1">{formatMoney(changeDue)}</span>
                  </span>
                )}
              </span>
            </div>
            {isShort && (
              <p role="status" className="text-amber-700 text-sm font-medium">
                {t(canPayOnAccount ? 'payment.account_amount' : 'payment.short_by', { amount: formatMoney(shortfall) })}
              </p>
            )}
            {changeDue > 0 && (
              <p className="text-yellow-700 text-sm font-medium">
                {t('payment.change_due', { amount: formatMoney(changeDue) })}
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
              {/* Final Total (Rounded) */}
              <div className="border-t pt-2">
                <div className="flex justify-between font-semibold text-lg">
                  <span>{t('payment.total')}</span>
                  <span>{formatMoney(finalTotal)}</span>
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

          {/* Print Invoice Checkbox */}
          {enablePaymentWithoutPrinting && (
            <div className="mb-4 flex items-center gap-2">
              <input
                id="print-invoice-checkbox"
                type="checkbox"
                checked={printInvoice}
                onChange={(e) => setPrintInvoice(e.target.checked)}
                className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500 cursor-pointer"
              />
              <label
                htmlFor="print-invoice-checkbox"
                className="text-sm font-medium text-gray-700 cursor-pointer select-none flex items-center gap-1.5"
              >
                <Printer className="w-4 h-4 text-gray-500" />
                {t('payment.print_invoice')}
              </label>
            </div>
          )}

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
      {activeApproval === 'room_charge_over_limit' && roomCheck && (
        <ManagerApproval
          open={true}
          posProfile={posProfile}
          privilege="room_charge_over_limit"
          summaryLines={[
            roomCheck.label || `${roomCheck.roomNumber || roomNumber} · ${roomCheck.guestName || ''}`,
            roomCheck.reason || t('payment.room_over_limit'),
            t('manager_approval.amount_line', { amount: formatMoney(roomChargeAmount) }),
          ]}
          reference={{ doctype: 'POS Invoice', name: invoice }}
          claim={{
            reservation: roomCheck.reservation || '',
            room: roomCheck.roomNumber || roomNumber.trim(),
            amount: roundToCurrency(roomChargeAmount, roomCheck.currency || currencyCode),
            currency: roomCheck.currency || currencyCode,
          }}
          amount={roomChargeAmount}
          requireReason={true}
          onApproved={handleManagerApproved}
          onCancel={() => setActiveApproval(null)}
        />
      )}
      {activeApproval === 'on_account_over_limit' && (
        <ManagerApproval
          open={true}
          posProfile={posProfile}
          privilege="on_account_over_limit"
          summaryLines={[
            account ? `${account.customer_name} · ${account.name}` : customer,
            t('payment.account_over_limit_notice'),
            t('manager_approval.amount_line', { amount: formatMoney(shortfall) }),
          ]}
          reference={{ doctype: 'POS Invoice', name: invoice }}
          claim={{
            customer: account?.name || customer,
            company: storePosProfile?.company || '',
            proposed: roundToCurrency(shortfall, account?.currency || currencyCode),
            limit: roundToCurrency(Number(account?.limit || 0), account?.currency || currencyCode),
            currency: account?.currency || currencyCode,
          }}
          amount={shortfall}
          requireReason={true}
          onApproved={handleManagerApproved}
          onCancel={() => setActiveApproval(null)}
        />
      )}
    </Dialog>
  );
};

export default PaymentDialog; 