import { afterEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { getPOSInvoices, mapSplitGroupInvoiceToPOSInvoice, searchPosInvoice, type SplitGroupInvoice } from '../src/lib/invoice-api';

// Simphony's open-check list shows how long every open check has been open
// (and what is on it), whichever queue the cashier is looking at, and the
// covers on each check. Draft and Unbilled are open checks too.

const SERVER_NOW = '2026-10-02 10:45:00';

function serve(rows: Array<Record<string, unknown>>) {
  return vi.spyOn(call, 'get').mockImplementation(async (method, args?: any) => {
    if (method === 'ury.ury_pos.api.getBranch') return { message: 'Branch A' };
    if (method === 'ury.ury_pos.api.getPosInvoice') return { message: { data: rows.map((row) => ({ ...row })), next: false } };
    if (method === 'ury.ury_pos.api.searchPosInvoice') return { message: { data: rows.map((row) => ({ ...row })), next: false } };
    if (method === 'ury.ury.api.ury_server_time.get_server_time') return { message: SERVER_NOW };
    if (method === 'frappe.client.get_list' && args?.doctype === 'POS Invoice Item') {
      return { message: [{ parent: 'INV-1', item_name: 'Tea', qty: 2 }, { parent: 'INV-2', item_name: 'Soup', qty: 1 }] };
    }
    if (method === 'frappe.client.get_list' && args?.doctype === 'POS Invoice') return { message: rows.map((row) => ({ ...row })) };
    throw new Error(`Unexpected Frappe call: ${method}`);
  });
}

const rows = [
  { name: 'INV-1', creation: '2026-10-02 10:00:00', status: 'Draft', no_of_pax: 3 },
  { name: 'INV-2', creation: '2026-10-02 10:30:00', status: 'Draft', no_of_pax: 2 },
];

afterEach(() => vi.restoreAllMocks());

describe('open checks on the Draft and Unbilled queues', () => {
  it.each(['Draft', 'Unbilled'] as const)('shows each %s check its age and its items', async (status) => {
    serve(rows);
    const { invoices } = await getPOSInvoices({ status, limit: 10, limit_start: 0 });
    expect(invoices).toMatchObject([
      { name: 'INV-1', age_minutes: 45, items_preview: [{ item_name: 'Tea', qty: 2 }] },
      { name: 'INV-2', age_minutes: 15, items_preview: [{ item_name: 'Soup', qty: 1 }] },
    ]);
  });

  it('gives searched Draft checks their age too', async () => {
    serve(rows);
    const { data } = await searchPosInvoice('INV', 'Draft');
    expect(data).toMatchObject([{ name: 'INV-1', age_minutes: 45 }, { name: 'INV-2', age_minutes: 15 }]);
  });

  it('does not age a paid check', async () => {
    const get = serve(rows.map((row) => ({ ...row, status: 'Paid' })));
    const { invoices } = await getPOSInvoices({ status: 'Paid', limit: 10, limit_start: 0 });
    expect(invoices.every((invoice) => invoice.age_minutes === undefined)).toBe(true);
    expect(get).not.toHaveBeenCalledWith('ury.ury.api.ury_server_time.get_server_time');
  });

  it('keeps the list when the clock cannot be read', async () => {
    vi.spyOn(call, 'get').mockImplementation(async (method) => {
      if (method === 'ury.ury_pos.api.getPosInvoice') return { message: { data: rows.map((row) => ({ ...row })), next: true } };
      throw new Error('Enrichment unavailable');
    });
    const { invoices, hasMore } = await getPOSInvoices({ status: 'Draft', limit: 10, limit_start: 0 });
    expect(hasMore).toBe(true);
    expect(invoices.map((invoice) => [invoice.name, invoice.age_minutes])).toEqual([['INV-1', undefined], ['INV-2', undefined]]);
  });
});

describe('covers on open checks', () => {
  it('reads the covers and the customer name with the Outstanding queue', async () => {
    const get = serve(rows);
    await getPOSInvoices({ status: 'Outstanding', limit: 10, limit_start: 0, branch: 'Branch A' });
    const listCall = get.mock.calls.find(([method, args]) => method === 'frappe.client.get_list' && (args as any)?.doctype === 'POS Invoice');
    expect((listCall![1] as { fields: string[] }).fields).toEqual(expect.arrayContaining(['no_of_pax', 'customer_name']));
  });

  it('finds an Outstanding check by its customer name, not only by a naming-series id', async () => {
    const get = serve(rows);
    await searchPosInvoice('elena', 'Outstanding', 'Branch A');
    const listCall = get.mock.calls.find(([method, args]) => method === 'frappe.client.get_list' && (args as any)?.doctype === 'POS Invoice');
    expect((listCall![1] as { or_filters: unknown[] }).or_filters).toContainEqual(['customer_name', 'like', '%elena%']);
  });

  it('keeps the covers when a split sibling replaces a queue row', () => {
    const sibling = { name: 'INV-2', no_of_pax: 2, customer: 'CUST-1', customer_name: 'Acme' } as unknown as SplitGroupInvoice;
    expect(mapSplitGroupInvoiceToPOSInvoice(sibling)).toMatchObject({ no_of_pax: 2, customer_name: 'Acme' });
  });
});
