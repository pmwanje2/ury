// Ported from the fork's node:test file self-order/src/lib/payment-outcome.test.mjs
// (bad7b4af, 41ef7214). version-16 runs self-order tests with vitest.
import { describe, expect, it } from 'vitest'

async function load() {
  // Imported lazily so each test fails on its own assertion while the module is missing.
  const path = './payment-outcome'
  return (await import(/* @vite-ignore */ path)) as typeof import('./payment-outcome')
}

const base = { payment_request: 'PR-1', currency: 'UGX', payment_url: 'https://psp.example/PR-1' }

describe('guest phone payment outcome', () => {
  it('paid request never redirects, even with a link', async () => {
    const { paymentOutcome } = await load()
    for (const status of ['Paid', 'Partially Paid']) {
      const outcome = paymentOutcome({ ...base, status, amount: 12000, paid_amount: 12000, remaining_amount: 0 })
      expect(outcome.redirect).toBeNull()
      expect(outcome.notice).toEqual({ key: 'payment.paid', params: { amount: 12000, currency: 'UGX' } })
    }
  })

  it('paid phone plus growth sends the remaining balance to the counter, never checkout', async () => {
    const { paymentOutcome } = await load()
    for (const status of ['Paid', 'Partially Paid', 'Requested']) {
      const outcome = paymentOutcome({ ...base, status, amount: 12000, paid_amount: 12000, remaining_amount: 3000 })
      expect(outcome.redirect).toBeNull()
      expect(outcome.notice).toEqual({
        key: 'payment.remaining',
        params: { paid: 12000, remaining: 3000, currency: 'UGX' },
      })
    }
  })

  it('first request redirects without a notice', async () => {
    const { paymentOutcome } = await load()
    const outcome = paymentOutcome({ ...base, status: 'Requested', amount: 12000, paid_amount: 0, remaining_amount: 12000 })
    expect(outcome).toEqual({ redirect: base.payment_url, notice: null })
  })

  it('request without a link keeps the staff-assist notice', async () => {
    const { paymentOutcome } = await load()
    const outcome = paymentOutcome({ ...base, payment_url: null, status: 'Requested', amount: 12000 })
    expect(outcome).toEqual({
      redirect: null,
      notice: { key: 'payment.request_created', params: { amount: 12000, currency: 'UGX' } },
    })
  })

  it('renders the fork English copy for each notice', async () => {
    const { paymentNoticeText } = await load()
    expect(paymentNoticeText({ key: 'payment.paid', params: { amount: 12000, currency: 'UGX' } }))
      .toBe('Paid (12000 UGX) — thank you.')
    expect(paymentNoticeText({ key: 'payment.remaining', params: { paid: 12000, remaining: 3000, currency: 'UGX' } }))
      .toBe("You've paid 12000 UGX by phone. Please pay the remaining 3000 UGX at the counter.")
    expect(paymentNoticeText({ key: 'payment.request_created', params: { amount: 12000, currency: 'UGX' } }))
      .toBe('Payment request created (12000 UGX) — a staff member will assist with payment.')
  })

  it.each([
    ['sw', 'Imelipwa (12000 UGX) — asante.', 'Umelipa 12000 UGX kwa simu. Tafadhali lipa kiasi kilichobaki 3000 UGX kwenye kaunta.'],
    ['lg', 'Kusasuddwa (12000 UGX) — webale.', 'Osasudde 12000 UGX ku ssimu. Nsaba sasula ebisigaddeyo 3000 UGX ku kaunta.'],
  ])('renders each notice in the %s guest language (T6d review B2)', async (code, paid, remaining) => {
    const i18n = await import('../i18n')
    await i18n.initI18n(code, { setCookie() {} })
    try {
      const { paymentNoticeText } = await load()
      expect(paymentNoticeText({ key: 'payment.paid', params: { amount: 12000, currency: 'UGX' } })).toBe(paid)
      expect(paymentNoticeText({ key: 'payment.remaining', params: { paid: 12000, remaining: 3000, currency: 'UGX' } }))
        .toBe(remaining)
      expect(paymentNoticeText({ key: 'payment.request_created', params: { amount: 12000, currency: 'UGX' } }))
        .toBe(i18n.t('payment.request_created', { amount: 12000, currency: 'UGX' }))
    } finally {
      await i18n.initI18n('en', { setCookie() {} })
    }
  })
})
