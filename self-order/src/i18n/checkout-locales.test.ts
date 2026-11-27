import { describe, expect, it } from 'vitest'
import { bundledLocales } from './loader'
import { translate } from './translate'

describe.each(['ar', 'fr', 'es'])('%s checkout', lang => {
  it('provides its own navigation, payment and bill status text', () => {
    for (const key of ['payment.starting', 'bill.requested', 'payment.request_created',
      'payment.remaining', 'payment.paid', 'nav.back_to_cart', 'language.chooser', 'errors.pay_online']) {
      expect(translate(bundledLocales[lang], key)).not.toBe(key)
    }
  })
})
