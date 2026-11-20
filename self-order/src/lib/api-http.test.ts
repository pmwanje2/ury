// Ported from the fork's node:test file (8f2b6dee). version-16 runs self-order tests with vitest.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const source = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'api.ts'), 'utf8')

function functionBody(name: string) {
  const start = source.indexOf(`export async function ${name}`)
  expect(start, `${name} must remain part of the guest API`).not.toBe(-1)
  const end = source.indexOf('\nexport ', start + 1)
  return source.slice(start, end === -1 ? source.length : end)
}

describe('guest API transport', () => {
  it('bill-bearing guest reads use POST bodies, while bootstrap remains GET-compatible', () => {
    // version-16 names the product read getCustomerProduct (version-2: getProductAddons).
    for (const name of ['getMenu', 'getCustomerProduct', 'getCurrentOrder', 'getStatus', 'getPaymentStatus']) {
      const body = functionBody(name)
      expect(body, `${name} must not put the session credential in a query string`).toMatch(/call\.post/)
      expect(body, `${name} must not use GET for a bill-bearing credential`).not.toMatch(/call\.get/)
    }

    expect(functionBody('bootstrap')).toMatch(/call\.get/)
    expect(functionBody('bootstrapDevice')).toMatch(/call\.get/)
  })
})
