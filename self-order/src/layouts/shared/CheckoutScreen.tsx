import { useState } from 'react'
import type { CartEntry } from '../../hooks/useOrderingSession'
import type { OrderingCapabilities, PaymentRequestResult } from '../../lib/api'
import { getActiveLanguage, t } from '../../i18n'
import { paymentNoticeFor } from '../../lib/payment-outcome'

// Local, UI-only concept — `pay_at_counter_enabled` has no backend action of
// its own (see useOrderingSession's payOnline/handleRequestBill: neither
// covers "pay at counter"). Selecting it just means "place the order without
// attempting online payment", so it is tracked here purely to drive the
// picker's selected state, never sent anywhere.
type PaymentMethod = 'online' | 'counter' | 'bill'

export interface CheckoutScreenProps {
  /** Capability flags gating which payment options are offered. */
  capabilities: Pick<
    OrderingCapabilities,
    'customer_payment_enabled' | 'pay_at_counter_enabled' | 'request_bill_enabled' | 'tip_prompt'
  >
  /** Cart line items to summarize. */
  cartItems: CartEntry[]
  /** Item count across all cart lines (sum of qty). */
  cartCount: number
  /** Cart total, matching useOrderingSession's cartTotal. */
  cartTotal: number
  /** True while an order submission is in flight (from useOrderingSession's submitting). */
  submitting: boolean
  /** True while an online payment request is being created (payingOnline). */
  payingOnline: boolean
  /** True once a bill has already been requested for this order (billRequested). */
  billRequested: boolean
  /** Result of a created payment request, if any (paymentRequest). */
  paymentRequest: PaymentRequestResult | null
  /** Places the order — mirrors useOrderingSession's submitCart. */
  onSubmitCart: (tip?: string) => void
  /** Starts online payment — mirrors useOrderingSession's payOnline. */
  onPayOnline: (tip?: string) => void
  /** Requests the bill — mirrors useOrderingSession's handleRequestBill. */
  onRequestBill: () => void
}

/**
 * Controlled checkout screen: order summary + payment method picker. Mirrors
 * the inline pay-online / request-bill wiring in MobileQRLayout.tsx, but
 * takes everything as props instead of calling useOrderingSession directly
 * so it can be reused across layout shells.
 *
 * When no payment capability is enabled at all (gateway disabled), the
 * picker renders nothing and the place-order button simply submits the cart — the
 * pure "pay/settle at counter later" path the plan requires.
 */
function CheckoutScreen({
  capabilities,
  cartItems,
  cartCount,
  cartTotal,
  submitting,
  payingOnline,
  billRequested,
  paymentRequest,
  onSubmitCart,
  onPayOnline,
  onRequestBill,
}: CheckoutScreenProps) {
  const showPayOnline = capabilities.customer_payment_enabled
  const showPayAtCounter = capabilities.pay_at_counter_enabled
  const showRequestBill = capabilities.request_bill_enabled
  const hasAnyPaymentOption = showPayOnline || showPayAtCounter || showRequestBill
  const paymentNotice = paymentNoticeFor(paymentRequest)

  // "Pay at Counter" is purely informational — selecting it doesn't call
  // anything, it just reflects the customer's choice not to pay online.
  // Defaults to counter when offered, otherwise no method is pre-selected.
  const [selectedMethod, setSelectedMethod] = useState<PaymentMethod | null>(
    showPayAtCounter ? 'counter' : null,
  )

  const prompt = capabilities.tip_prompt
  const [percent, setPercent] = useState<number | null>(0)
  const [custom, setCustom] = useState('')
  const amount = percent === null ? Number(custom) : cartTotal * percent / 100
  const invalid = !Number.isFinite(amount) || amount < 0 || amount > cartTotal
  const tip = prompt && !invalid ? amount.toLocaleString('en-US', {
    useGrouping: false, minimumFractionDigits: prompt.precision, maximumFractionDigits: prompt.precision,
  }) : undefined
  const total = prompt ? new Intl.NumberFormat(getActiveLanguage(), {
    style: 'currency', currency: prompt.currency,
    minimumFractionDigits: prompt.precision, maximumFractionDigits: prompt.precision,
  }).format(cartTotal + Number(tip || 0)) : cartTotal

  return (
    <div className="mx-4 mt-4">
      <section className="rounded-lg border p-3">
        <h2 className="mb-2 text-sm font-medium text-muted-foreground">{t('checkout.summary')}</h2>
        <ul className="space-y-1 text-sm">
          {cartItems.map((entry) => (
            <li key={entry.key ?? entry.item.item} className="flex justify-between">
              <span>
                {entry.item.item_name} × {entry.qty}
              </span>
              <span>{entry.qty * entry.item.rate}</span>
            </li>
          ))}
        </ul>
        <div className="mt-2 flex justify-between border-t pt-2 text-sm font-semibold">
          <span>{t(cartCount === 1 ? 'cart.count_one' : 'cart.count_many', { count: cartCount })}</span>
          <span>{total}</span>
        </div>
      </section>

      {prompt && (
        <fieldset className="mt-4 min-w-0 rounded-lg border p-3">
          <legend>{t('tip.title')}</legend>
          <div className="flex flex-wrap gap-2">
            {[0, ...prompt.presets].map(value => (
              <button key={value} type="button" aria-pressed={percent === value}
                className="rounded border px-3 py-2 aria-pressed:border-primary"
                onClick={() => setPercent(value)}>
                {value ? `${value}%` : t('tip.none')}
              </button>
            ))}
          </div>
          <label className="mt-2 block">{t('tip.custom')} ({prompt.currency})
            <input type="number" min="0" max={cartTotal} step={10 ** -prompt.precision}
              className="mt-1 w-full rounded border bg-background p-2"
              value={percent === null ? custom : ''} onChange={event => {
                setCustom(event.target.value)
                setPercent(null)
              }} />
          </label>
          <output>{tip} {prompt.currency}</output>
        </fieldset>
      )}

      {hasAnyPaymentOption && (
        <section className="mt-4 rounded-lg border p-3">
          <h2 className="mb-2 text-sm font-medium text-muted-foreground">{t('checkout.payment_method')}</h2>
          <div className="space-y-2">
            {showPayOnline && (
              <button
                type="button"
                onClick={() => setSelectedMethod('online')}
                className={`w-full rounded-md border py-2 text-sm font-medium ${
                  selectedMethod === 'online' ? 'border-primary bg-primary/10' : ''
                }`}
              >
                {t('payment.pay_online')}
              </button>
            )}
            {showPayAtCounter && (
              <button
                type="button"
                onClick={() => setSelectedMethod('counter')}
                className={`w-full rounded-md border py-2 text-sm font-medium ${
                  selectedMethod === 'counter' ? 'border-primary bg-primary/10' : ''
                }`}
              >
                {t('payment.pay_at_counter')}
              </button>
            )}
            {showRequestBill && (
              <button
                type="button"
                disabled={billRequested}
                onClick={() => {
                  setSelectedMethod('bill')
                  onRequestBill()
                }}
                className={`w-full rounded-md border py-2 text-sm font-medium disabled:opacity-50 ${
                  selectedMethod === 'bill' ? 'border-primary bg-primary/10' : ''
                }`}
              >
                {billRequested ? t('bill.requested') : t('bill.request')}
              </button>
            )}
          </div>
          {paymentNotice && (
            <p role="status" className="mt-2 text-xs text-muted-foreground">
              {paymentNotice}
            </p>
          )}
        </section>
      )}

      <div className="mt-4">
        {showPayOnline && selectedMethod === 'online' ? (
          <button
            type="button"
            onClick={() => onPayOnline(tip)}
            disabled={payingOnline || invalid}
            className="w-full rounded-md bg-primary py-3 font-medium text-primary-foreground disabled:opacity-50"
          >
            {payingOnline ? t('payment.starting') : t('payment.pay_online')}
          </button>
        ) : (
          <button
            type="button"
            onClick={() => onSubmitCart(tip)}
            disabled={invalid || submitting || cartItems.length === 0}
            className="w-full rounded-md bg-primary py-3 font-medium text-primary-foreground disabled:opacity-50"
          >
            {submitting ? t('order.placing') : t('order.place')}
          </button>
        )}
      </div>
    </div>
  )
}

export default CheckoutScreen
