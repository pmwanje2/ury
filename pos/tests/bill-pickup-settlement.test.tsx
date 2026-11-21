import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { call, formatCurrency } from '@ury/core';
import { initI18n } from '../src/i18n';
import Orders from '../src/pages/Orders';
import { useRootStore } from '../src/store/root-store';

const invoice = {
  name: 'PRIMARY', branch: 'A', restaurant_table: 'Table A', status: 'Draft', docstatus: 0,
  rounded_total: 40, grand_total: 40, net_total: 40, total_taxes_and_charges: 0, invoice_printed: 1,
  custom_merged_pos_invoice: 'LINKED', custom_merged_total: 25, custom_split_group: null,
  order_type: 'Dine In', posting_date: '2026-11-20', posting_time: '10:00:00', customer: '',
};
let root: Root, host: HTMLDivElement;
let branch: string, linkedInvoice: string | null;
let splitRead: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  await initI18n('en');
  document.cookie = 'user_id=cashier; Path=/';
  useRootStore.setState(useRootStore.getInitialState());
  branch = 'A'; linkedInvoice = invoice.name;
  splitRead = vi.fn(async () => ({ message: { group: null, current: invoice.name, invoices: [] } }));
  vi.spyOn(call, 'post').mockRejectedValue(Error('No writes expected'));
  vi.spyOn(call, 'get').mockImplementation(async (method, args: any) => {
    if (method === 'ury.ury_pos.api.getBranch') return { message: branch };
    if (method === 'ury.ury_pos.api.getModeOfPayment') return { message: [{ mode_of_payment: 'Cash' }] };
    if (method === 'ury.ury_pos.api.get_split_group') return splitRead();
    if (method === 'ury.ury_pos.api.getPosInvoiceItems') return { message: [[], []] };
    if (method === 'frappe.client.has_permission') return { message: { has_permission: false } };
    if (method.endsWith('.get_server_time')) return { message: '2026-11-20 10:05:00' };
    if (args?.doctype === 'URY Table') return { message: [{ name: `Table ${args.filters.branch}` }] };
    if (args?.doctype === 'URY Service Request') {
      const [table] = args.filters.find(([field]: string[]) => field === 'table')[2];
      return { message: [{ name: `REQ-${table}`, table, invoice: linkedInvoice, status: 'Pending', requested_at: '10:00' }] };
    }
    if (args?.doctype === 'POS Invoice Item') return { message: [] };
    if (args?.doctype === 'POS Invoice') {
      const row = args.filters.branch === 'A' ? invoice : { ...invoice, name: 'QUEUE-B', branch: 'B', restaurant_table: 'Table B' };
      return { message: Object.entries(args.filters).every(([k, v]) => row[k as keyof typeof row] === v)
        ? [Object.fromEntries(args.fields.map((k: keyof typeof row) => [k, row[k]]))] : [] };
    }
    throw Error(`Unexpected read: ${method}`);
  });
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount()); host.remove();
  expect(call.post).not.toHaveBeenCalled(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});
async function render() { await act(async () => root.render(<MemoryRouter><Orders /></MemoryRouter>)); }
async function click(el: HTMLElement | null | undefined) {
  expect(el).toBeTruthy(); await act(async () => el!.click());
}
const strip = () => host.querySelector<HTMLElement>('[aria-label="Bill requests"]')!;
async function payment() {
  await click([...host.querySelectorAll('button')].find(b => b.textContent === 'Settle bill'));
  const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
  const total = [...dialog.querySelectorAll('span')].find(s => s.textContent === 'Total')?.nextElementSibling?.textContent;
  const pay = [...dialog.querySelectorAll('button')].find(b => b.textContent?.startsWith('Pay '))?.textContent;
  await click(dialog.querySelector<HTMLButtonElement>('button:has(svg.lucide-x)'));
  return { total, pay };
}

it.each(['exact invoice', 'table only'])('settles the same unsplit merged check from Outstanding and %s bill pickup', async entry => {
  if (entry === 'table only') linkedInvoice = null;
  await render();
  await click(host.querySelector('h3[title="PRIMARY"]'));
  const outstanding = await payment();
  expect(outstanding).toEqual({ total: formatCurrency(65), pay: `Pay ${formatCurrency(65)}` });
  await click(strip().querySelector('li button'));
  expect(await payment()).toEqual(outstanding);
  expect(useRootStore.getState().selectedOrder?.custom_merged_pos_invoice).toBe('LINKED');
});

it('discards previous bill rows and a pending split lookup when Outstanding first confirms a new branch', async () => {
  linkedInvoice = 'OLD-BILL';
  let release!: (value: unknown) => void;
  splitRead.mockImplementation(() => new Promise(resolve => { release = resolve; }));
  await render();
  await click(strip().querySelector('li button'));
  expect(splitRead).toHaveBeenCalledOnce();
  branch = 'B';
  await act(async () => useRootStore.getState().fetchOrders());
  expect(host.querySelector('h3[title="QUEUE-B"]')).toBeTruthy();
  expect.soft(strip().textContent).not.toContain('Table A');
  await act(async () => release({ message: { group: 'GROUP', invoices: [{ ...invoice, name: 'SPLIT-A' }] } }));
  expect(strip().textContent).not.toContain('SPLIT-A');
  expect(strip().textContent).toContain('Table B');
});
