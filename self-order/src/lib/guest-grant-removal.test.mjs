import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

const root = new URL('../../../', import.meta.url)
const file = (relative) => new URL(relative, root)

test('guest phone QR and fragment grant surfaces are absent', () => {
  assert.equal(fs.existsSync(file('pos/src/components/GuestPhoneQrDialog.tsx')), false)
  assert.equal(fs.existsSync(file('pos/src/lib/guest-phone-qr-api.ts')), false)
  assert.equal(fs.existsSync(file('pos/tests/guest-phone-qr.test.tsx')), false)
  assert.equal(fs.existsSync(file('pos/tests/guest-grant-ordering.test.tsx')), false)

  const api = fs.readFileSync(file('self-order/src/lib/api.ts'), 'utf8')
  const session = fs.readFileSync(file('self-order/src/hooks/useOrderingSession.ts'), 'utf8')
  assert.equal(api.includes('bootstrapGrantedSession'), false)
  assert.equal(session.includes('bootstrapGrantedSession'), false)
  assert.equal(session.includes('#g='), false)
})

