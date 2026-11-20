// Ported from fork pos/tests/till-close-api.test.mjs (legacy store part:
// 799dc28e, 81c2f646, d36b7728) and urypos/src/stores/posClosing.test.js (264f6146).
// Run with: node --test urypos/src/stores/posClosing.test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const tillMethod = 'ury.ury_pos.api.get_till_invoices';
const params = {
  start: '2026-10-01 08:00:00', end: '2026-10-01 18:00:00',
  pos_profile: 'Till', user: 'cashier2',
};
const invoice = {
  name: 'OTHER-CASHIER', cashier: 'cashier1', owner: 'waiter',
  grand_total: 15000, net_total: 15000, total_qty: 1, taxes: [],
  payments: [{ mode_of_payment: 'Cash', amount: 15000 }],
  modified: '2026-10-01 12:00:00',
};

function loadStore() {
  const source = readFileSync(new URL('./posClosing.js', import.meta.url), 'utf8')
    .replace(/^import .*;\r?\n/gm, '').replace('export const posClosing', 'const posClosing');
  return vm.runInNewContext(`${source}\nposClosing;`, {
    defineStore: (_name, options) => options, console,
  });
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test('main close requests the whole till and consumes full invoice documents', async () => {
  const store = loadStore();
  const calls = [];
  const state = {
    startDate: params.start, periodEndDate: new Date(2026, 9, 1, 18, 0, 0),
    invoiceData: { posProfile: 'Till' }, posProfile: 'Till', cashier: 'cashier2',
    grandTotal: 0, netTotal: 0, totalQty: 0,
    call: { get: async (method, args) => {
      calls.push({ method, args: { ...args } });
      return { message: [invoice] };
    } },
  };
  store.actions.getInvoice.call(state);
  await settle();
  assert.deepEqual(calls, [{ method: tillMethod, args: params }]);
  assert.equal(state.grandTotal, 15000);
  assert.equal(state.payments[0].expected_amount, 15000);
  assert.equal(state.posInvoice[0].pos_invoice, 'OTHER-CASHIER');
});

async function assertOpeningCounts(managerProfile) {
  const store = loadStore();
  const opening = {
    name: 'OPEN-2', owner: 'manager', user: 'cashier2', pos_profile: 'Till',
    period_start_date: params.start,
    balance_details: [{ mode_of_payment: 'Cash', opening_amount: 0 }],
  };
  const calls = [];
  const createdDocs = [];
  const state = {
    periodEndDate: new Date(2026, 9, 1, 18, 0, 0),
    postingDate: '2026-10-01', postingTime: new Date(2026, 9, 1, 18, 0, 0),
    invoiceData: { posProfile: managerProfile, company: 'Restaurant' },
    grandTotal: 0, netTotal: 0, totalQty: 0,
    getInvoice: store.actions.getInvoice,
    call: { get: async (method, args) => {
      calls.push({ method, args: { ...args } });
      return { message: method === 'frappe.client.get' ? opening : [invoice] };
    } },
    db: { createDoc: async (doctype, fields) => {
      createdDocs.push({ doctype, fields });
      return { name: 'CLOSE-2' };
    } },
  };

  store.actions.selectPos.call(state, { name: 'OPEN-2' });
  await settle();
  store.actions.savePosClosing.call(state);
  await settle();

  assert.equal(calls.length, 2);
  assert.equal(createdDocs.length, 1);
  assert.deepEqual({
    invoiceUser: calls[1].args.user,
    closingUser: createdDocs[0].fields.user,
    invoiceProfile: calls[1].args.pos_profile,
    closingProfile: createdDocs[0].fields.pos_profile,
  }, {
    invoiceUser: 'cashier2', closingUser: 'cashier2',
    invoiceProfile: 'Till', closingProfile: 'Till',
  });
  assert.deepEqual(calls, [
    { method: 'frappe.client.get', args: { doctype: 'POS Opening Entry', name: 'OPEN-2' } },
    { method: tillMethod, args: params },
  ]);
  assert.equal(createdDocs[0].doctype, 'POS Closing Entry');
  assert.equal(createdDocs[0].fields.pos_opening_entry, 'OPEN-2');
  assert.equal(Object.hasOwn(createdDocs[0].fields, 'pos_transactions'), false);
  assert.equal(Object.hasOwn(createdDocs[0].fields, 'pos_invoices'), false);
  assert.deepEqual(JSON.parse(JSON.stringify(createdDocs[0].fields.payment_reconciliation)), [{
    mode_of_payment: 'Cash', opening_amount: 0, expected_amount: 15000, difference: -15000,
  }]);
  assert.equal(createdDocs[0].fields.grand_total, 15000);
  assert.equal(createdDocs[0].fields.docstatus, 0);
}

test("opening selection counts the holder's till without posting invoice rows", async () => {
  await assertOpeningCounts('Till');
});

test("manager close uses the selected opening's profile for invoice reads and the closing entry", async () => {
  await assertOpeningCounts('Manager Till');
});

const floatCases = [
  { opening: 50000, sales: 20000, count: 70000, expected: 70000, difference: 0 },
  { opening: 50000, sales: 20000, count: 69000, expected: 70000, difference: -1000 },
  { opening: 50000, sales: 20000, count: 71000, expected: 70000, difference: 1000 },
  { opening: 50000, sales: 20000, count: undefined, expected: 70000, difference: -70000 },
  { opening: 50000, sales: undefined, count: 50000, expected: 50000, difference: 0 },
  { opening: '50000', sales: 20000, count: 70000, expected: 70000, difference: 0 },
  { opening: 0, sales: 20000, count: 20000, expected: 20000, difference: 0 },
];

for (const { opening, sales, count, expected, difference } of floatCases) {
  test(`closing includes opening float: opening ${opening}, sales ${sales}, count ${count}`, async () => {
    const store = loadStore();
    const createdDocs = [];
    const state = {
      postingTime: new Date(2026, 9, 1, 18, 0, 0), postingDate: '2026-10-01',
      grandTotal: 0, netTotal: 0, totalQty: 0,
      invoiceData: { company: 'Test Co', posProfile: 'POS-1' },
      posProfile: 'POS-1',
      alert: { createAlert: () => {} },
      db: { createDoc: async (doctype, fields) => {
        createdDocs.push({ doctype, fields });
        return { name: 'POS-CLO-1' };
      } },
    };
    state.openingBalance = [
      { mode_of_payment: 'Cash', opening_amount: opening, ...(count === undefined ? {} : { closing_amount: count }) },
      { mode_of_payment: 'Mobile Money', opening_amount: 0, closing_amount: 5000 },
    ];
    state.payments = [
      ...(sales === undefined ? [] : [{ mode_of_payment: 'Cash', expected_amount: sales }]),
      { mode_of_payment: 'Mobile Money', expected_amount: 5000 },
    ];

    store.actions.savePosClosing.call(state);
    await settle();

    assert.equal(createdDocs.length, 1);
    assert.equal(createdDocs[0].doctype, 'POS Closing Entry');
    assert.equal(createdDocs[0].fields.docstatus, 0);
    assert.deepEqual(JSON.parse(JSON.stringify(createdDocs[0].fields.payment_reconciliation)), [
      {
        mode_of_payment: 'Cash', opening_amount: opening,
        ...(count === undefined ? {} : { closing_amount: count }),
        expected_amount: expected, difference,
      },
      { mode_of_payment: 'Mobile Money', opening_amount: 0, closing_amount: 5000, expected_amount: 5000, difference: 0 },
    ]);
  });
}
