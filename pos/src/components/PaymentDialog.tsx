import React, { useState, useEffect, useRef } from 'react';
import { X, Percent, Coins } from 'lucide-react';
import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';
import { formatCurrency, call, parseFrappeError } from '@ury/core';
import { Button, Input, Dialog, DialogContent, showToast } from '@ury/ui';
import { DEFAULT_PAYMENT_MODE } from '../data/order-types';
import { t } from '../i18n';
import { assertOfflineActionAllowed, submitSettlementOfflineAware } from '../offline/cashier';


interface PaymentDialogProps {
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
  clientOrderId?: string | null;
}

interface LoyaltyQuote {
  enabled: boolean;
  balance: number;
  loyalty_points: number;
  loyalty_amount: number;
  amount_due: number;
  context: string;
}

const PaymentDialog: React.FC<PaymentDialogProps> = ({
  onClose,
  grandTotal,
  roundedTotal,
  invoice,
  customer,
  posProfile,
  table,
  tableLabel,
  cashier,
  owner,
  fetchOrders,
  clearSelectedOrder,
  discountPercentage,
  discountAmount,
  clientOrderId,
}) => {
  const { paymentModes, fetchPaymentModes, posProfile: storePosProfile } = usePOSStore();
  const user = useRootStore((state) => state.user);
  const [isProcessing, setIsProcessing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [discountType] = useState<'percentage'>('percentage'); // Only percentage now
  
  // Calculate effective percentage if only amount is provided (for invoice-level discounts)
  const effectivePercentage = discountPercentage 
    ? discountPercentage 
    : (discountAmount && grandTotal + discountAmount > 0 
        ? (discountAmount / (grandTotal + discountAmount)) * 100 
        : 0);
        
  const [discountValue, setDiscountValue] = useState<string>(effectivePercentage > 0 ? String(effectivePercentage) : '');
  const [appliedDiscount, setAppliedDiscount] = useState<number>(discountAmount || 0); // Only tracking transaction discount!
  const [paymentInputs, setPaymentInputs] = useState<{ [mode: string]: string }>({});
  const [pointsInput, setPointsInput] = useState('');
  const [loyaltyQuote, setLoyaltyQuote] = useState<LoyaltyQuote | null>(null);
  const [loyaltyBusy, setLoyaltyBusy] = useState(false);
  const loyaltyRequest = useRef(0);
  const loyaltyEnabled = storePosProfile?.name === posProfile && storePosProfile.ury_enable_loyalty === 1
    && !!storePosProfile.customer && !!customer.trim() && customer !== storePosProfile.customer
    && !['walkin', 'walkincustomer'].includes(customer.toLowerCase().replace(/[- ]/g, ''))
    && navigator.onLine !== false;
  const loyaltyContext = JSON.stringify([invoice, customer, posProfile, appliedDiscount]);
  const currentQuote = loyaltyEnabled && loyaltyQuote?.context === loyaltyContext ? loyaltyQuote : null;
  const requestedPoints = Number(pointsInput || 0);
  const unquotedPoints = !Number.isSafeInteger(requestedPoints) || requestedPoints < 0
    || requestedPoints !== (currentQuote?.loyalty_points ?? 0);
  const redeemedPoints = currentQuote?.enabled && !unquotedPoints ? currentQuote.loyalty_points : 0;

  useEffect(() => {
    const requests = loyaltyRequest;
    const request = ++requests.current;
    setLoyaltyQuote(null);
    setPointsInput('');
    setLoyaltyBusy(loyaltyEnabled);
    if (loyaltyEnabled) {
      call.get('ury.ury.doctype.ury_order.ury_order.get_loyalty_redemption', {
        invoice, customer, pos_profile: posProfile, loyalty_points: 0,
        additionalDiscount: appliedDiscount > 0 ? appliedDiscount : null,
      }).then(({ message }) => {
        if (request === loyaltyRequest.current) setLoyaltyQuote({ ...message, context: loyaltyContext });
      }).catch((err: unknown) => {
        if (request === loyaltyRequest.current) setError(parseFrappeError(err, t('errors.payment_failed')));
      }).finally(() => {
        if (request === loyaltyRequest.current) setLoyaltyBusy(false);
      });
    }
    return () => { ++requests.current; };
  }, [loyaltyEnabled, loyaltyContext, invoice, customer, posProfile, appliedDiscount]);

  const handleRedeem = async () => {
    if (unquotedPoints && (!Number.isSafeInteger(requestedPoints) || requestedPoints < 0)) return;
    const request = ++loyaltyRequest.current;
    setLoyaltyBusy(true);
    setError(null);
    try {
      await assertOfflineActionAllowed('loyalty redemption');
      const { message } = await call.get('ury.ury.doctype.ury_order.ury_order.get_loyalty_redemption', {
        invoice, customer, pos_profile: posProfile, loyalty_points: requestedPoints,
        additionalDiscount: appliedDiscount > 0 ? appliedDiscount : null,
      });
      if (request === loyaltyRequest.current) setLoyaltyQuote({ ...message, context: loyaltyContext });
    } catch (err) {
      if (request === loyaltyRequest.current) setError(parseFrappeError(err, t('errors.payment_failed')));
    } finally {
      if (request === loyaltyRequest.current) setLoyaltyBusy(false);
    }
  };

  useEffect(() => {
    fetchPaymentModes();
  }, [fetchPaymentModes]);

  // baseTotal represents the amount before any invoice-level discount (like pricing rule or manual discount)
  const baseTotal = grandTotal + (discountAmount || 0);

  const handleApplyDiscount = async () => {
    try { await assertOfflineActionAllowed('discount'); }
    catch (error) { setError(error instanceof Error ? error.message : t('errors.invalid_discount')); return; }
    const value = parseFloat(discountValue);
    if (isNaN(value) || value <= 0) {
      setError(t('errors.invalid_discount'));
      return;
    }
    if (value > 100) {
      setError(t('errors.discount_exceeds_max'));
      return;
    }
    const calculatedDiscount = (baseTotal * value) / 100;
    setAppliedDiscount(calculatedDiscount);
    setError(null);
  };

  // Order summary logic
  const subtotal = baseTotal;
  const adjustment = roundedTotal - grandTotal;
  const roundedAdjustment = Math.round(adjustment * 100) / 100;
  const showAdjustment = Math.abs(roundedAdjustment) > 0.001;
  const totalDiscount = appliedDiscount;
  const discountedTotal = Math.max(0, subtotal - totalDiscount);
  // If discount is applied, round up; else, round normally
  const finalTotal = currentQuote?.enabled && !unquotedPoints ? currentQuote.amount_due
    : appliedDiscount > 0 ? Math.ceil(discountedTotal) : Math.round(discountedTotal);

  // Calculate split payment total
  const payments = paymentModes
    .map((mode: any) => {
      const id = typeof mode === 'string' ? mode : mode.id;
      const amount = parseFloat(paymentInputs[id] || '');
      return amount > 0 ? { mode_of_payment: id, amount } : null;
    })
    .filter(Boolean);
  const paymentsTotal = payments.reduce((sum, p: any) => sum + p.amount, 0);
  const shortfall = finalTotal - paymentsTotal;
  const isShort = shortfall > 0.005;
  const accountPaymentMode = paymentModes
    .map((mode: string | { id: string }) => typeof mode === 'string' ? mode : mode.id)
    .find(Boolean);
  const canPayOnAccount = isShort && !!accountPaymentMode && storePosProfile?.name === posProfile
    && storePosProfile.allow_partial_payment === 1 && !!storePosProfile.customer
    && !!customer.trim() && customer !== storePosProfile.customer;
  const fullyRedeemed = redeemedPoints > 0 && finalTotal === 0 && !!accountPaymentMode;
  const paymentDisabled = isProcessing || loyaltyBusy || (loyaltyEnabled && unquotedPoints)
    || ((payments.length === 0 || isShort) && !canPayOnAccount && !fullyRedeemed);

  const finalAdjustment = finalTotal - discountedTotal;
  const roundedFinalAdjustment = Math.round(finalAdjustment * 100) / 100;
  const showFinalAdjustment = Math.abs(roundedFinalAdjustment) > 0.001;

  useEffect(()=>{
    const defaultPaymentModePresent=paymentModes.find((mode)=>mode===DEFAULT_PAYMENT_MODE)
    //only one payment mode should be present, then autofill the final amount, if not do not fill
    const otherPaymentModesNotEntered=Object.keys(paymentInputs).length<=1;
    if(finalTotal && paymentModes && DEFAULT_PAYMENT_MODE && defaultPaymentModePresent && otherPaymentModesNotEntered){
      //check if default payment mode is present in paymentModes
      setPaymentInputs((prev)=>({ 
        ...prev,
        [DEFAULT_PAYMENT_MODE]:String(finalTotal) 
      }))
    }
  },[finalTotal,paymentModes])

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
      await assertOfflineActionAllowed('online invoice settlement, card or pay-by-phone');
      const onlineSettle = (ids?: { clientOrderId: string; roundId: string }) => call.post('ury.ury.doctype.ury_order.ury_order.make_invoice', {
        additionalDiscount: appliedDiscount > 0 ? appliedDiscount : null,
        cashier, customer, invoice, owner,
        payments: payments.length === 0 && (canPayOnAccount || fullyRedeemed)
          ? [{ mode_of_payment: accountPaymentMode, amount: 0 }]
          : payments,
        pos_profile: posProfile, table,
        ...(redeemedPoints > 0 ? { loyalty_points: redeemedPoints } : {}),
        ...(ids ? { client_order_id: ids.clientOrderId, round_id: ids.roundId } : {}),
      });
      // Points are never an offline cash payment, including a gateway outage
      // after the online request. The native ledger must validate them now.
      const settlement = redeemedPoints > 0 ? { offline: false, response: await onlineSettle() }
        : await submitSettlementOfflineAware({
        profile: storePosProfile ?? { name: posProfile },
        user: user ?? { name: 'Guest', roles: [] },
        invoice, customer, table, cashier, owner, clientOrderId,
        amount: payments.length === 1 ? payments[0].amount : paymentsTotal,
        due: finalTotal,
        modeOfPayment: payments.length === 1 ? payments[0].mode_of_payment : '',
        discount: appliedDiscount,
        onlineSettle,
      });
      if (settlement.offline) {
        showToast.success(t('offline.saved'));
        onClose();
        clearSelectedOrder();
        return;
      }
      showToast.success(t('payment.success', { amount: formatCurrency(paymentsTotal) }));
      onClose();
      clearSelectedOrder();
      await fetchOrders();
    } catch (err) {
      console.error('Payment failed:', err);
      // parseFrappeError extracts human-readable message from _server_messages (e.g. stock validation error)
      setError(parseFrappeError(err, t('errors.payment_failed')));
    } finally {
      setIsProcessing(false);
    }
  };

  return (
    <Dialog open={true} onOpenChange={onClose}>
      <DialogContent variant="xlarge" className="bg-white w-full max-w-4xl max-h-dialog-max-h flex flex-col md:flex-row p-0" showCloseButton={false}>
        {/* Left Column - Discount and Payment Mode */}
        <div className="md:w-1/2 p-6 border-b md:border-b-0 md:border-r border-gray-200 overflow-y-auto">
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
            </div>
          )}

          {/* Payment Methods Section - Split Payment */}
          {currentQuote?.enabled && (
            <div className="space-y-3 mb-6">
              <h3 className="text-lg font-semibold">{t('payment.loyalty')}</h3>
              <p className="text-sm">{t('payment.points_available', { points: currentQuote.balance })}</p>
              <p className="text-sm text-gray-600">{t('payment.loyalty_bill_only')}</p>
              <div className="flex gap-2">
                <Input type="number" min="0" step="1" max={currentQuote.balance}
                  aria-label={t('payment.points_to_redeem')} value={pointsInput}
                  onChange={e => { ++loyaltyRequest.current; setLoyaltyBusy(false); setPointsInput(e.target.value); }}
                  disabled={isProcessing || loyaltyBusy} size="sm" className="flex-1" />
                <Button onClick={handleRedeem} disabled={isProcessing || loyaltyBusy
                  || !Number.isSafeInteger(requestedPoints) || requestedPoints < 0} size="sm">
                  {t('payment.redeem')}
                </Button>
              </div>
              {redeemedPoints > 0 && <p className="text-sm">{t('payment.loyalty_redeemed', {
                amount: formatCurrency(currentQuote.loyalty_amount),
              })}</p>}
            </div>
          )}
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
            <div className="flex justify-between mt-2 text-sm">
              <span className="font-medium">{t('payment.total_entered')}</span>
              <span className={`${isShort ? 'text-amber-700' : 'text-green-600'} font-semibold flex items-center gap-1`}>
                {formatCurrency(paymentsTotal)} / {formatCurrency(finalTotal)}
                {paymentsTotal > finalTotal && (
                  <span className="text-yellow-700 font-semibold">
                    <Coins className="inline w-4 h-4 ml-1 text-yellow-500" />
                    <span className="text-yellow-500 font-bold ml-1">{formatCurrency(paymentsTotal - finalTotal)}</span>
                  </span>
                )}
              </span>
            </div>
            {isShort && (
              <p role="status" className="text-amber-700 text-sm font-medium">
                {t('payment.short_by', { amount: formatCurrency(shortfall) })}
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
              {showFinalAdjustment && !redeemedPoints && (
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
