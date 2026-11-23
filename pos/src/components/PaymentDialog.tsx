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
import { resolvePrintFormat, type POSInvoice } from '../lib/invoice-api';
import { isConfiguredDefaultCustomer, profileLacksDefaultCustomerConfig } from '@ury/core/default-customer';


type CreditAccount = {name: string; customer_name: string; balance: number; limit: number; available: number; currency: string};

interface PaymentDialogProps {
  order?: (Pick<POSInvoice, 'custom_merged_pos_invoice' | 'ury_client_order_id'> & { invoice_printed?: string | number }) | null;
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
  const { paymentModes, fetchPaymentModes, posProfile: storePosProfile } = usePOSStore();
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
  const [roomNumber, setRoomNumber] = useState('');
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

  // baseTotal represents the amount before any invoice-level discount (like pricing rule or manual discount)
  const baseTotal = grandTotal + (discountAmount || 0);

  const handleApplyDiscount = async () => {
    try {
      await assertOfflineActionAllowed('discount');
    } catch (error) {
      setError(error instanceof Error ? error.message : t('errors.invalid_discount'));
      return;
    }
    const value = parseFloat(discountValue);
    if (isNaN(value) || value <= 0) {
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
      setAppliedDiscountPct(value);
      setAppliedDiscount(Math.max(0, Math.round((baseTotal - message.grand_total) * 100) / 100));
    } catch (err) {
      setError(parseFrappeError(err, t('errors.invalid_discount')));
      return;
    }
    setError(null);
  };

  // Order summary logic
  const subtotal = baseTotal;
  const adjustment = roundedTotal - grandTotal;
  const roundedAdjustment = Math.round(adjustment * 100) / 100;
  const showAdjustment = Math.abs(roundedAdjustment) > 0.001;
  const totalDiscount = appliedDiscount;
  const discountedTotal = Math.max(0, subtotal - totalDiscount);
  // The server's total: its rounded total, or its grand total where the profile does not round.
  const finalTotal = serverTotals
    ? (serverTotals.rounded_total || serverTotals.grand_total)
    : (roundedTotal || grandTotal);

  // Calculate split payment total
  const payments = paymentModes
    .map((mode: any) => {
      const id = typeof mode === 'string' ? mode : mode.id;
      const amount = parseFloat(paymentInputs[id] || '');
      return amount > 0 ? { mode_of_payment: id, amount } : null;
    })
    .filter(Boolean);
  const paymentsTotal = payments.reduce((sum, p: any) => sum + p.amount, 0);
  // Room-charge tenders are those the POS Profile reports as posting to Guest Ledger Clearing.
  const roomChargeModes = storePosProfile?.room_charge_modes || [];
  const roomChargeUnconfigured = Boolean(storePosProfile?.room_charge_unconfigured) && roomChargeModes.length === 0;
  const typedDiscount = discountValue.trim() === '' ? null : parseFloat(discountValue);
  const hasUnappliedDiscount = discountValue.trim() !== '' && typedDiscount !== appliedDiscountPct;
  const roomChargeAmount = roomChargeModes
    .reduce((sum: number, mode: string) => sum + (parseFloat(paymentInputs[mode] || '') || 0), 0);
  const roomVerified = !roomLookupMethod || !(roomChargeAmount > 0)
    || (!!roomCheck?.ok && roomCheck.room === roomNumber.trim());
  const roomFields = roomChargeAmount > 0 ? { room_number: roomNumber } : {};
  const shortfall = finalTotal - paymentsTotal;
  const isShort = shortfall > 0.005;
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
  const paymentDisabled = isProcessing || !roomVerified
    || ((payments.length === 0 || isShort) && !canPayOnAccount);
  const settlementPayments = payments.length === 0 && canPayOnAccount
    ? [{ mode_of_payment: accountPaymentMode, amount: 0 }]
    : payments;

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
  const roundedFinalAdjustment = Math.round(finalAdjustment * 100) / 100;
  const showFinalAdjustment = Math.abs(roundedFinalAdjustment) > 0.001;

  // The POS Profile payment row marked Default is prefilled with the total.
  const defaultPaymentMode = storePosProfile?.default_payment_mode || '';

  useEffect(()=>{
    const defaultPaymentModePresent=paymentModes.find((mode)=>mode===defaultPaymentMode)
    //only one payment mode should be present, then autofill the final amount, if not do not fill
    const otherPaymentModesNotEntered=Object.keys(paymentInputs).length<=1;
    if(finalTotal && paymentModes && defaultPaymentMode && defaultPaymentModePresent && otherPaymentModesNotEntered){
      //check if default payment mode is present in paymentModes
      setPaymentInputs((prev)=>({ 
        ...prev,
        [defaultPaymentMode]:String(finalTotal) 
      }))
    }
  },[finalTotal,paymentModes,defaultPaymentMode])

  // Helper to calculate remaining balance
  const getRemainingBalance = (currentId: string) => {
    const totalEntered = Object.entries(paymentInputs)
      .filter(([id]) => id !== currentId)
      .reduce((sum, [_, val]) => sum + (parseFloat(val) || 0), 0);
    return Math.max(0, finalTotal - totalEntered);
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
    setIsProcessing(true);
    setError(null);
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
        });
      } else {
        const settlement = await submitSettlementOfflineAware({
          profile: storePosProfile ?? { name: posProfile }, user: user ?? { name: 'Guest', roles: [] },
          invoice, customer, table, cashier, owner, clientOrderId: order?.ury_client_order_id,
          amount: paymentsTotal, due: finalTotal, discount: appliedDiscount,
          modeOfPayment: payments.length === 1 ? payments[0]?.mode_of_payment ?? '' : '',
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
      showToast.success(t('payment.success', { amount: formatCurrency(paymentsTotal) }));
      onClose();
      clearSelectedOrder();
      await fetchOrders();
    } catch (err) {
      console.error('Payment failed:', err);
      setError(parseFrappeError(err, t('errors.payment_failed')));
    } finally {
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
                  if (paymentsTotal === finalTotal) setPaymentInputs({});
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
                {formatCurrency(paymentsTotal)} / {formatCurrency(finalTotal)}
                {paymentsTotal > finalTotal && (
                  <span className="text-yellow-700 font-semibold">
                    <Coins className="inline w-4 h-4 ms-1 text-yellow-500" />
                    <span className="text-yellow-500 font-bold ms-1">{formatCurrency(paymentsTotal - finalTotal)}</span>
                  </span>
                )}
              </span>
            </div>
            {isShort && (
              <p role="status" className="text-amber-700 text-sm font-medium">
                {t(canPayOnAccount ? 'payment.account_amount' : 'payment.short_by', { amount: formatCurrency(shortfall) })}
              </p>
            )}
            {paymentsTotal > finalTotal && (
              <p className="text-yellow-700 text-sm font-medium">
                {t('payment.change_due', { amount: formatCurrency(paymentsTotal - finalTotal) })}
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
                <span>{formatCurrency(subtotal)}</span>
              </div>
              {/* Discount */}
              {appliedDiscount > 0 && (
                <div className="flex justify-between text-green-600">
                  <span>{t('payment.discount')}</span>
                  <span>-{formatCurrency(appliedDiscount)}</span>
                </div>
              )}
              {/* Adjustment (if any) */}
              {showFinalAdjustment && (
                <div className="flex justify-between text-blue-600">
                  <span>{t('payment.adjustment')}</span>
                  <span>{roundedFinalAdjustment > 0 ? '+' : ''}{formatCurrency(roundedFinalAdjustment)}</span>
                </div>
              )}
              {/* Final Total (Rounded) */}
              <div className="border-t pt-2">
                <div className="flex justify-between font-semibold text-lg">
                  <span>{t('payment.total')}</span>
                  <span>{formatCurrency(finalTotal)}</span>
                </div>
              </div>
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
            {isProcessing ? t('payment.processing') : t('payment.pay_button', { amount: formatCurrency(paymentsTotal > 0 ? paymentsTotal : finalTotal) })}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
};

export default PaymentDialog; 