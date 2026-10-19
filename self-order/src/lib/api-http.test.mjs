import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const source = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'api.ts'), 'utf8')

function functionBody(name) {
  const start = source.indexOf(`export async function ${name}`)
  assert.notEqual(start, -1, `${name} must remain part of the guest API`)
  const end = source.indexOf('\nexport ', start + 1)
  return source.slice(start, end === -1 ? source.length : end)
}

test('bill-bearing guest reads use POST bodies, while bootstrap remains GET-compatible', () => {
  for (const name of ['getMenu', 'getProductAddons', 'getCurrentOrder', 'getStatus', 'getPaymentStatus']) {
    const body = functionBody(name)
    assert.match(body, /call\.post/, `${name} must not put the session credential in a query string`)
    assert.doesNotMatch(body, /call\.get/, `${name} must not use GET for a bill-bearing credential`)
  }

  assert.match(functionBody('bootstrap'), /call\.get/)
  assert.match(functionBody('bootstrapDevice'), /call\.get/)
})
