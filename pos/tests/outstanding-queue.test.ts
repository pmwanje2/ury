import { expect, test, vi } from 'vitest';
import { call } from '@ury/core';
import { getPOSInvoices } from '../src/lib/invoice-api';

test('pages branch drafts oldest first and batches previews for visible checks only', async () => {
  const get = vi.spyOn(call, 'get').mockImplementation(async (method, args) => {
    if (method === 'ury.ury_pos.api.getBranch') return { message: 'Branch A' };
    if (method.endsWith('get_server_time')) return { message: '2026-10-01 12:00:00' };
    if (args?.doctype === 'POS Invoice') return { message: ['A', 'B'].map(name => ({ name, creation: '2026-10-01 11:15:00' })) };
    if (args?.doctype === 'POS Invoice Item') return { message: [{ parent: 'A', item_name: 'Tea', qty: 2 }] };
    return { message: { data: [], next: false } };
  });
  try {
    const result = await getPOSInvoices({ status: 'Outstanding', limit: 1, limit_start: 4 });
    expect(result).toMatchObject({ invoices: [{ name: 'A', age_minutes: 45, items_preview: [{ item_name: 'Tea', qty: 2 }] }], hasMore: true });
    expect(get).toHaveBeenCalledWith('frappe.client.get_list', expect.objectContaining({ doctype: 'POS Invoice', filters: { branch: 'Branch A', docstatus: 0, status: 'Draft' }, order_by: 'creation asc, name asc', limit_start: 4, limit_page_length: 2 }));
    expect(get).toHaveBeenCalledWith('frappe.client.get_list', expect.objectContaining({ doctype: 'POS Invoice Item', parent: 'POS Invoice', filters: { parenttype: 'POS Invoice', parent: ['in', ['A']] } }));
  } finally { get.mockRestore(); }
});

test.each(['clock', 'preview'])('retains authorized pagination when %s is unavailable', async failure => {
  const get = vi.spyOn(call, 'get').mockImplementation(async (method, args) => {
    if (method.endsWith('.getBranch')) return { message: 'A' };
    if (args?.doctype === 'POS Invoice') return { message: ['I','J'].map(name => ({ name, creation: '2026-10-01 11:00:00', custom_split_group: 'S' })) };
    if (method.endsWith('get_server_time') && failure !== 'clock') return { message: '2026-10-01 12:00:00' };
    if (args?.doctype === 'POS Invoice Item' && failure !== 'preview') return { message: [] };
    throw new Error('Enrichment unavailable');
  });
  try {
    const { invoices, hasMore } = await getPOSInvoices({ status: 'Outstanding', limit: 1 });
    expect(hasMore).toBe(true); expect(invoices).toHaveLength(1);
    expect(invoices[0]).toMatchObject({ name: 'I', custom_split_group: 'S' });
    expect(invoices[0].age_minutes).toBe(failure === 'clock' ? undefined : 60);
    expect(invoices[0].items_preview).toEqual(failure === 'preview' ? undefined : []);
  } finally { get.mockRestore(); }
});
