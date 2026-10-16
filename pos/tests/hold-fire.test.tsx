import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { beforeEach, afterEach, expect, test, vi } from 'vitest';
import { call } from '@ury/core';
import { initI18n } from '../src/i18n';
import { usePOSStore } from '../src/store/pos-store';
import * as api from '../src/lib/order-api';

vi.mock('@ury/core', async (original) => ({ ...await original<object>(), call: { get: vi.fn(), post: vi.fn() } }));

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(true);
  await initI18n('en');
  usePOSStore.setState({ activeOrders: [], orderId: null, posProfile: { name: 'POS A', ury_enable_hold_fire: 1 } as never });
  vi.mocked(call.post).mockReset();
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

test('table hydration preserves native row identity, course and held state without coalescing duplicate dishes', async () => {
  vi.mocked(call.get).mockResolvedValue({ message: { name: 'INV-1', items: [
    { name: 'sent', item_code: 'Fish', item_name: 'Fish', qty: 2, rate: 100, custom_course: 'Main', ury_held: 0 },
    { name: 'held', item_code: 'Fish', item_name: 'Fish', qty: 3, rate: 100, custom_course: 'Main', ury_held: 1 },
  ] } });
  await usePOSStore.getState().loadTableOrder('T-1');
  const rows = usePOSStore.getState().activeOrders;
  expect(rows.map(row => [row.invoiceItemName, row.ury_held, row.course])).toEqual([['sent', 0, 'Main'], ['held', 1, 'Main']]);
  expect(new Set(rows.map(row => row.uniqueId)).size).toBe(2);
});

test('fire wrapper refuses offline before any RPC, and uses the exact native method online', async () => {
  expect(typeof api.fireItems).toBe('function');
  vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false);
  await expect(api.fireItems('INV-1', { line: 'held' })).rejects.toThrow();
  expect(call.post).not.toHaveBeenCalled();
  vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(true);
  await api.fireItems('INV-1', { course: 'Main' });
  expect(call.post).toHaveBeenCalledWith('ury.ury.doctype.ury_order.ury_order.fire_items', { invoice: 'INV-1', course: 'Main' });
});

test('hold store refuses offline and does not merge held and sent lines', () => {
  usePOSStore.setState({ activeOrders: [{ id: 'Fish', uniqueId: 'new', name: 'Fish', price: 100, quantity: 1 } as never] });
  expect(typeof usePOSStore.getState().setItemHeld).toBe('function');
  vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false);
  expect(() => usePOSStore.getState().setItemHeld('new', true)).toThrow();
  expect(usePOSStore.getState().activeOrders[0].ury_held).toBeFalsy();
});

test('adding another of the same dish never merges it into an unsaved held line', async () => {
  usePOSStore.setState({ activeOrders: [] });
  const item = { id: 'Fish', name: 'Fish', price: 100, quantity: 1 };
  await usePOSStore.getState().addToOrder(item as never);
  const uniqueId = usePOSStore.getState().activeOrders[0].uniqueId!;
  usePOSStore.getState().setItemHeld(uniqueId, true);
  await usePOSStore.getState().addToOrder(item as never);
  const rows = usePOSStore.getState().activeOrders;
  expect(rows.map(row => [!!row.ury_held, row.quantity])).toEqual([[true, 1], [false, 1]]);
  expect(new Set(rows.map(row => row.uniqueId)).size).toBe(2);
  expect(rows[0].uniqueId).toBe(uniqueId);
});

// Render the real control with the real store. No server/site/table is contacted.
test('held/sent controls are default-off and hidden offline; fire preserves unsaved cart edits', async () => {
  const modules = import.meta.glob('../src/components/*Controls.tsx');
  const load = modules['../src/components/HoldFireControls.tsx'];
  expect(load, 'native hold/fire controls are missing').toBeDefined();
  const controls = await load() as { HoldFireControls: import('react').ComponentType };
  const host = document.createElement('div'); document.body.append(host);
  const root: Root = createRoot(host);
  const rows = [
    { id: 'Fish', name: 'Fish', uniqueId: 'held', invoiceItemName: 'held', quantity: 3, price: 100, course: 'Main', ury_held: 1 },
    { id: 'Tea', name: 'Tea', uniqueId: 'new', quantity: 2, price: 100, course: 'Dessert' },
  ];
  usePOSStore.setState({ activeOrders: rows as never, orderId: 'INV-1', lastModifiedTime: 'v1' });
  vi.mocked(call.post).mockResolvedValue({ message: { name: 'INV-1', modified: 'v2', items: [{ name: 'held', ury_held: 0 }] } });
  const render = async () => { await act(async () => root.render(<controls.HoldFireControls />)); };
  try {
    await render();
    expect(host.textContent).toContain('Held');
    const fire = [...host.querySelectorAll('button')].find(b => b.textContent === 'Fire line');
    expect(fire).toBeDefined();
    await act(async () => fire!.click());
    expect(usePOSStore.getState().activeOrders.map(row => row.quantity)).toEqual([3, 2]);
    expect(usePOSStore.getState().activeOrders[0].ury_held).toBe(0);
    expect(usePOSStore.getState().lastModifiedTime).toBe('v2');
    expect(host.textContent).toContain('Sent');
    await act(async () => { usePOSStore.setState({ posProfile: { name: 'POS A' } as never }); }); await render();
    expect(host.textContent).toBe('');
    await act(async () => { usePOSStore.setState({ posProfile: { name: 'POS A', ury_enable_hold_fire: 1 } as never }); });
    vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false);
    await act(async () => { window.dispatchEvent(new Event('offline')); }); await render();
    expect(host.querySelector('button')).toBeNull();
    expect(host.querySelector('input')).toBeNull();
  } finally { await act(async () => root.unmount()); host.remove(); }
});
