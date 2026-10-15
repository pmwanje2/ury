import type { PaymentRequestResult } from './api'

export interface PaymentNotice {
  key: 'payment.paid' | 'payment.remaining' | 'payment.request_created'
  params: Record<string, string | number>
}

export interface PaymentOutcome {
  redirect: string | null
  notice: PaymentNotice | null
}

/**
 * What the guest page does with a create_payment_request response. A paid
 * request never sends the guest back to checkout, even if a link exists.
 */
export function paymentOutcome(result: PaymentRequestResult): PaymentOutcome {
  const currency = result.currency
  if (result.paid_amount && (result.remaining_amount ?? 0) > 0) {
    return {
      redirect: null,
      notice: {
        key: 'payment.remaining',
        params: { paid: result.paid_amount, remaining: result.remaining_amount!, currency },
      },
    }
  }
  if (result.status === 'Paid' || result.status === 'Partially Paid') {
    return {
      redirect: null,
      notice: { key: 'payment.paid', params: { amount: result.paid_amount || result.amount, currency } },
    }
  }
  const redirect = result.payment_url || null
  if (!redirect) {
    return { redirect, notice: { key: 'payment.request_created', params: { amount: result.amount, currency } } }
  }
  return { redirect, notice: null }
}
