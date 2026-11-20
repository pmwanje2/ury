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

// English copy from the fork catalog; the guest-language catalogs can key on PaymentNotice.key.
const NOTICE_TEXT: Record<PaymentNotice['key'], string> = {
  'payment.paid': 'Paid ({{amount}} {{currency}}) — thank you.',
  'payment.remaining':
    "You've paid {{paid}} {{currency}} by phone. Please pay the remaining {{remaining}} {{currency}} at the counter.",
  'payment.request_created': 'Payment request created ({{amount}} {{currency}}) — a staff member will assist with payment.',
}

export function paymentNoticeText(notice: PaymentNotice): string {
  return NOTICE_TEXT[notice.key].replace(/\{\{(\w+)\}\}/g, (_, name: string) => String(notice.params[name] ?? ''))
}

/** The notice text for a payment request result, or null when there is nothing to say. */
export function paymentNoticeFor(result: PaymentRequestResult | null | undefined): string | null {
  if (!result) return null
  const { notice } = paymentOutcome(result)
  return notice ? paymentNoticeText(notice) : null
}
