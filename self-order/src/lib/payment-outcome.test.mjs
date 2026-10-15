import assert from 'node:assert/strict'
import test from 'node:test'

async function load() {
  try {
    return await import('./payment-outcome.ts')
  } catch (error) {
    assert.fail(`payment-outcome.ts missing: ${error.code || error.message}`)
  }
}

const base = { payment_request: 'PR-1', currency: 'UGX', payment_url: 'https://psp.example/PR-1' }

test('guest phone payment outcome', async (t) => {
  const { paymentOutcome } = await load()

  await t.test('paid request never redirects, even with a link', () => {
    for (const status of ['Paid', 'Partially Paid']) {
      const outcome = paymentOutcome({ ...base, status, amount: 12000, paid_amount: 12000, remaining_amount: 0 })
      assert.equal(outcome.redirect, null)
      assert.deepEqual(outcome.notice, { key: 'payment.paid', params: { amount: 12000, currency: 'UGX' } })
    }
  })

  await t.test('paid phone plus growth sends the remaining balance to the counter, never checkout', () => {
    for (const status of ['Paid', 'Partially Paid', 'Requested']) {
      const outcome = paymentOutcome({ ...base, status, amount: 12000, paid_amount: 12000, remaining_amount: 3000 })
      assert.equal(outcome.redirect, null)
      assert.deepEqual(outcome.notice, {
        key: 'payment.remaining',
        params: { paid: 12000, remaining: 3000, currency: 'UGX' },
      })
    }
  })

  await t.test('first request redirects without a notice', () => {
    const outcome = paymentOutcome({ ...base, status: 'Requested', amount: 12000, paid_amount: 0, remaining_amount: 12000 })
    assert.deepEqual(outcome, { redirect: base.payment_url, notice: null })
  })

  await t.test('request without a link keeps the staff-assist notice', () => {
    const outcome = paymentOutcome({ ...base, payment_url: null, status: 'Requested', amount: 12000 })
    assert.deepEqual(outcome, {
      redirect: null,
      notice: { key: 'payment.request_created', params: { amount: 12000, currency: 'UGX' } },
    })
  })
})
