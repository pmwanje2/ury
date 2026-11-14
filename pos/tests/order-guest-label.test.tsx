import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { showToast } from '@ury/ui';
import { initI18n } from '../src/i18n';
import OrderPanel from '../src/components/OrderPanel';
import { syncOrder, type POSInvoice } from '../src/lib/order-api';

// M03: a short guest label (Susan, David) on the order, sent with sync_order
// so it reaches the kitchen ticket, with no Customer master created.

const state = vi.hoisted(() => ({
  activeOrders: [{ id: 'SOUP', uniqueId: 'ROW-1', name: 'Soup', price: 18000, quantity: 1 }],
  posProfile: { name: 'POS-1', cashier: 'Cashier', owner: 'Owner' },
  selectedOrderType: 'Take Away', selectedTable: null, selectedRoom: 'Dining',
  selectedCustomer: null, selectedAggregator: null,
  isUpdatingOrder: false, orderId: null, lastModifiedTime: null,
  orderComment: '', guestLabel: '', noOfPax: 1, paymentModes: ['Cash'], orderLoading: false,
  user: { name: 'waiter@example.com' },
  resetOrderState: vi.fn(), clearOrder: vi.fn(), setNoOfPax: vi.fn(), removeFromOrder: vi.fn(),
  updateQuantity: vi.fn(), setSelectedItem: vi.fn(), setOrderComment: vi.fn(), setGuestLabel: vi.fn(),
}));
vi.mock('../src/store/pos-store', () => ({ usePOSStore: () => ({ ...state, isOrderInteractionDisabled: () => false }) }));
vi.mock('../src/store/root-store', () => ({ useRootStore: (select: (value: { user: typeof state.user }) => unknown) => select({ user: state.user }) }));
vi.mock('../src/lib/order-api', () => ({ syncOrder: vi.fn() }));
vi.mock('../src/components/CustomerSelect', () => ({ CustomerSelect: () => null }));
vi.mock('../src/components/ProductDialog', () => ({ default: () => null }));
vi.mock('../src/components/OrderTypeSelect', () => ({ default: () => null }));
vi.mock('../src/components/CommentDialog', () => ({ default: () => null }));
vi.mock('../src/components/OrderTabs', () => ({ default: () => null }));

let host: HTMLDivElement;
let root: Root;
beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  await initI18n('en');
  vi.clearAllMocks();
  vi.mocked(syncOrder).mockResolvedValue({ message: { name: 'INV-1', items: [] } as unknown as POSInvoice });
  vi.spyOn(showToast, 'error').mockImplementation(() => {});
  vi.spyOn(showToast, 'success').mockImplementation(() => {});
  host = document.createElement('div'); document.body.append(host); root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const submit = async () => {
  await act(async () => root.render(<OrderPanel />));
  const button = [...host.querySelectorAll('button')].find((b) => /Add New Order/i.test(b.textContent || ''))!;
  await act(async () => button.click());
};

it('sends the guest label with the order', async () => {
  state.guestLabel = 'Susan';
  await submit();
  expect(syncOrder).toHaveBeenCalledWith(expect.objectContaining({ guest_label: 'Susan', customer: '' }));
});

it('the cart has a short guest label box bound to the store', async () => {
  state.guestLabel = '';
  await act(async () => root.render(<OrderPanel />));
  const box = host.querySelector<HTMLInputElement>('input[name="guest_label"]')!;
  expect(box).not.toBeNull();
  expect(box.maxLength).toBe(40);
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(box, 'David');
    box.dispatchEvent(new Event('input', { bubbles: true }));
  });
  expect(state.setGuestLabel).toHaveBeenCalledWith('David');
});

it('no label sends none', async () => {
  state.guestLabel = '';
  await submit();
  expect(vi.mocked(syncOrder).mock.calls[0][0]).not.toHaveProperty('guest_label', expect.anything());
});
