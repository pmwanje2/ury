import assert from 'node:assert/strict'
import test from 'node:test'

async function load() {
  try {
    return await import('./guest-lines.ts')
  } catch (error) {
    assert.fail(`guest-lines.ts missing: ${error.code || error.message}`)
  }
}

const fish = { item: 'FISH', item_name: 'Fish', rate: 25000 }
const cheese = { item: 'CHEESE', item_name: 'Extra cheese', rate: 3000 }

test('guest item notes and add-ons', async (t) => {
  const lines = await load()

  await t.test('the same dish with different notes stays separate and both notes travel', () => {
    let cart = {}
    cart = lines.addGuestLine(cart, fish, 'no onions', true)
    cart = lines.addGuestLine(cart, fish, 'extra chili', true)
    const payload = lines.guestOrderPayload(cart)
    assert.equal(payload.length, 2)
    assert.deepEqual(payload.map((row) => row.item), ['FISH', 'FISH'])
    assert.deepEqual(payload.map((row) => row.qty), [1, 1])
    assert.deepEqual(payload.map((row) => row.comment).sort(), ['extra chili', 'no onions'])
    for (const row of payload) {
      assert.equal('rate' in row, false)
      assert.equal('price' in row, false)
    }
    assert.equal(lines.qtyForItem(cart, 'FISH'), 2)
  })

  await t.test('repeating the same note is one line, and a blank note is not sent', () => {
    let cart = {}
    cart = lines.addGuestLine(cart, fish, '  no onions  ', true)
    cart = lines.addGuestLine(cart, fish, 'no onions', true)
    assert.deepEqual(lines.guestOrderPayload(cart), [{ item: 'FISH', qty: 2, comment: 'no onions' }])
    const blank = lines.addGuestLine({}, fish, '   ', true)
    assert.deepEqual(lines.guestOrderPayload(blank), [{ item: 'FISH', qty: 1 }])
  })

  await t.test('a profile that does not allow notes drops them and merges the dish', () => {
    let cart = {}
    cart = lines.addGuestLine(cart, fish, 'no onions', false)
    cart = lines.addGuestLine(cart, fish, 'extra chili', false)
    assert.deepEqual(lines.guestOrderPayload(cart), [{ item: 'FISH', qty: 2 }])
  })

  await t.test('a note stops at 140 characters', () => {
    const cart = lines.addGuestLine({}, fish, 'n'.repeat(180), true)
    const [row] = lines.guestOrderPayload(cart)
    assert.equal(row.comment, 'n'.repeat(140))
  })

  await t.test('removing one note leaves the other note in the order', () => {
    let cart = lines.addGuestLine({}, fish, 'no onions', true)
    cart = lines.addGuestLine(cart, fish, 'extra chili', true)
    cart = lines.addGuestLine(cart, fish, 'no onions', true)
    const onions = Object.values(cart).find((line) => line.note === 'no onions')
    cart = lines.decrementGuestLine(cart, onions.key)
    assert.equal(lines.guestOrderPayload(cart).find((row) => row.comment === 'no onions').qty, 1)
    cart = lines.decrementGuestLine(cart, onions.key)
    const payload = lines.guestOrderPayload(cart)
    assert.equal(payload.some((row) => row.comment === 'no onions'), false)
    assert.deepEqual(payload, [{ item: 'FISH', qty: 1, comment: 'extra chili' }])
  })

  await t.test('add-on prices come from the server and are never invented', () => {
    const choices = lines.addonChoices([
      { item_code: 'CHEESE', item_name: 'Extra cheese', rate: 4500 },
      { item_code: 'FISH', item_name: 'Fish', rate: null },
      { item_code: 'SAUCE', item_name: 'Sauce' },
      { item_code: 'NAPKIN', item_name: 'Napkin', rate: 0 },
    ], [fish, cheese])
    assert.deepEqual(choices, [
      { item_code: 'CHEESE', item_name: 'Extra cheese', rate: 4500 },
      { item_code: 'FISH', item_name: 'Fish', rate: 25000 },
      { item_code: 'SAUCE', item_name: 'Sauce', rate: null },
      { item_code: 'NAPKIN', item_name: 'Napkin', rate: 0 },
    ])
    const sauce = lines.addonLineItem(choices[2], [fish, cheese])
    const sauceLine = lines.addGuestLine({}, sauce, '', false)
    assert.equal(lines.linePrice(Object.values(sauceLine)[0]), null)
    assert.deepEqual(lines.guestOrderPayload(sauceLine), [{ item: 'SAUCE', qty: 1 }])

    const cheeseItem = lines.addonLineItem(choices[0], [fish, cheese])
    const cheeseLine = lines.addGuestLine({}, cheeseItem, '', false)
    assert.equal(lines.linePrice(Object.values(cheeseLine)[0]), 4500)
    assert.deepEqual(lines.guestOrderPayload(cheeseLine), [{ item: 'CHEESE', qty: 1 }])
  })
})
