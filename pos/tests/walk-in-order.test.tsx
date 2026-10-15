import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { showToast } from '@ury/ui';
import { initI18n } from '../src/i18n';
import OrderPanel from '../src/components/OrderPanel';
import { syncOrder, type POSInvoice } from '../src/lib/order-api';

const state = vi.hoisted(() => ({
  activeOrders: [{ id: 'SOUP', uniqueId: 'ROW-1', name: 'Soup', price: 18000, quantity: 1 }],
  posProfile: { name: 'POS-1', cashier: 'Cashier', owner: 'Owner', customer: undefined as string | undefined },
  selectedOrderType: 'Take Away', selectedTable: null as string | null, selectedRoom: 'Dining',
  selectedCustomer: null as { name: string } | null,
  selectedAggregator: null as { customer: string } | null,
  isUpdatingOrder: false, orderId: 'INV-1', lastModifiedTime: '2026-10-13 10:00:00',
  orderComment: 'No pepper', noOfPax: 2, paymentModes: ['Cash'], orderLoading: false,
  user: { name: 'waiter@example.com' } as { name: string } | null,
  disabled: false, resetOrderState: vi.fn(), clearOrder: vi.fn(), setNoOfPax: vi.fn(),
  removeFromOrder: vi.fn(), updateQuantity: vi.fn(), setSelectedItem: vi.fn(), setOrderComment: vi.fn(),
}));
vi.mock('../src/store/pos-store', () => ({ usePOSStore: () => ({ ...state, isOrderInteractionDisabled: () => state.disabled }) }));
vi.mock('../src/store/root-store', () => ({ useRootStore: (select: (value: { user: typeof state.user }) => unknown) => select({ user: state.user }) }));
vi.mock('../src/lib/order-api', () => ({ syncOrder: vi.fn() }));
// Keep the native submit, cart and Pax controls; unrelated dialogs/selectors have their own tests.
vi.mock('../src/components/CustomerSelect', () => ({ CustomerSelect: () => null }));
vi.mock('../src/components/ProductDialog', () => ({ default: () => null }));
vi.mock('../src/components/OrderTypeSelect', () => ({ default: () => null }));
vi.mock('../src/components/CommentDialog', () => ({ default: () => null }));
vi.mock('../src/components/OrderTabs', () => ({ default: () => null }));
const invoice: POSInvoice = {
  name: 'INV-1', title: 'Order', customer: 'Configured Customer', customer_name: 'Configured Customer',
  mobile_number: '', customer_group: 'Individual', territory: 'Uganda', posting_date: '2026-10-13', posting_time: '10:00:00',
  order_type: 'Take Away', restaurant_table: '', custom_restaurant_room: 'Dining', status: 'Draft',
  total: 18000, grand_total: 18000, rounded_total: 18000, items: [],
};
let host: HTMLDivElement;
let root: Root;
beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  await initI18n('en');
  Object.assign(state, { selectedOrderType: 'Take Away', selectedTable: null, selectedCustomer: null,
    selectedAggregator: null, isUpdatingOrder: false, disabled: false, user: { name: 'waiter@example.com' } });
  state.posProfile.customer = undefined;
  vi.clearAllMocks();
  vi.mocked(syncOrder).mockResolvedValue({ message: invoice });
  vi.spyOn(showToast, 'error').mockImplementation(() => {});
  vi.spyOn(showToast, 'success').mockImplementation(() => {});
  host = document.createElement('div'); document.body.append(host); root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function render() { await act(async () => root.render(<OrderPanel />)); }
function submitButton() {
  const button = [...host.querySelectorAll('button')].find(button => /Add New Order|Update Order|Processing|Updating/i.test(button.textContent || ''));
  expect(button).toBeDefined(); return button!;
}
async function submit() { await render(); await act(async () => submitButton().click()); }

it.each(['Take Away', 'Dine In'])('submits anonymous %s for native customer resolution', async type => {
  state.selectedOrderType = type; state.selectedTable = type === 'Dine In' ? 'Table 1' : null;
  await submit();
  expect(syncOrder).toHaveBeenCalledWith(expect.objectContaining({ customer: '', order_type: type,
    table: state.selectedTable || undefined, no_of_pax: 2, pos_profile: 'POS-1', waiter: 'waiter@example.com',
    items: [{ item: 'SOUP', item_name: 'Soup', rate: 18000, qty: 1, comment: undefined }] }));
  expect(JSON.parse(JSON.stringify(vi.mocked(syncOrder).mock.calls[0][0]))).toHaveProperty('customer', '');
  expect(state.resetOrderState).toHaveBeenCalledOnce(); expect(showToast.success).toHaveBeenCalledOnce();
});
it.each(['Take Away', 'Dine In'])('online anonymous %s sends an explicit empty customer even when the profile has a default', async type => {
  state.posProfile.customer = 'Native Walk In';
  state.selectedOrderType = type; state.selectedTable = type === 'Dine In' ? 'Table 1' : null;
  await submit();
  expect(syncOrder).toHaveBeenCalledWith(expect.objectContaining({ customer: '', order_type: type }));
  expect(JSON.parse(JSON.stringify(vi.mocked(syncOrder).mock.calls[0][0]))).toHaveProperty('customer', '');
});
it.each([false, true])('preserves explicit customer and invoice metadata (update=%s)', async updating => {
  state.selectedCustomer = { name: 'Named Customer' }; state.isUpdatingOrder = updating;
  await submit();
  expect(syncOrder).toHaveBeenCalledWith(expect.objectContaining({ customer: 'Named Customer',
    invoice: updating ? 'INV-1' : null, last_invoice: updating ? 'INV-1' : null,
    last_modified_time: updating ? state.lastModifiedTime : undefined, room: 'Dining',
    comments: 'No pepper', cashier: 'Cashier', owner: 'Owner', mode_of_payment: 'Cash' }));
});
it('leaves an anonymous invoice update identity to the native endpoint', async () => {
  state.isUpdatingOrder = true; await submit();
  expect(syncOrder).toHaveBeenCalledWith(expect.objectContaining({ customer: '', invoice: 'INV-1', last_invoice: 'INV-1', last_modified_time: state.lastModifiedTime }));
});
it.each(['Delivery', 'Phone In'])('requires a selected customer for %s', async type => {
  state.selectedOrderType = type; await submit();
  expect(syncOrder).not.toHaveBeenCalled(); expect(showToast.error).toHaveBeenCalledWith(expect.stringMatching(/select a customer/i));
});
it('requires an aggregator and forwards its selected customer', async () => {
  state.selectedOrderType = 'Aggregators'; await submit();
  expect(syncOrder).not.toHaveBeenCalled(); expect(showToast.error).toHaveBeenCalledWith(expect.stringMatching(/aggregator/i));
  state.selectedAggregator = { customer: 'Aggregator Customer' }; await submit();
  expect(syncOrder).toHaveBeenCalledWith(expect.objectContaining({ customer: 'Aggregator Customer', aggregator_id: 'Aggregator Customer' }));
});
it('requires a Dine In table even without a named customer', async () => {
  state.selectedOrderType = 'Dine In'; await submit();
  expect(syncOrder).not.toHaveBeenCalled(); expect(showToast.error).toHaveBeenCalledWith(expect.stringMatching(/table/i));
});
it('surfaces missing native default configuration and retains the cart', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
  vi.mocked(syncOrder).mockRejectedValue(new Error('Select a customer or set a default customer in the POS Profile.'));
  await submit();
  expect(syncOrder).toHaveBeenCalledWith(expect.objectContaining({ customer: '' }));
  expect(showToast.error).toHaveBeenCalledWith('Select a customer or set a default customer in the POS Profile.');
  expect(state.resetOrderState).not.toHaveBeenCalled(); expect(state.clearOrder).not.toHaveBeenCalled();
  expect(host.textContent).toContain('Soup'); expect(submitButton().disabled).toBe(false);
});
it('retains the cart when the native endpoint returns Failure', async () => {
  vi.mocked(syncOrder).mockResolvedValue({ message: { status: 'Failure' } }); await submit();
  expect(syncOrder).toHaveBeenCalledOnce(); expect(showToast.error).toHaveBeenCalledOnce();
  expect(state.resetOrderState).not.toHaveBeenCalled(); expect(host.textContent).toContain('Soup');
});
it('disables submission and Pax while pending and prevents a second ordinary click', async () => {
  let finish!: (value: Awaited<ReturnType<typeof syncOrder>>) => void;
  vi.mocked(syncOrder).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  await submit(); expect(submitButton().disabled).toBe(true);
  expect([...host.querySelectorAll('button')].filter(button => ['+', '-'].includes(button.textContent || '')).every(button => button.disabled)).toBe(true);
  await act(async () => submitButton().click()); expect(syncOrder).toHaveBeenCalledOnce();
  await act(async () => finish({ message: invoice })); expect(state.resetOrderState).toHaveBeenCalledOnce();
});
it('preserves the session guard', async () => {
  state.user = null; vi.spyOn(console, 'error').mockImplementation(() => {}); await submit();
  expect(syncOrder).not.toHaveBeenCalled(); expect(showToast.error).toHaveBeenCalledOnce();
});
it('respects existing interaction restrictions', async () => {
  state.disabled = true; await submit(); expect(submitButton().disabled).toBe(true); expect(syncOrder).not.toHaveBeenCalled();
});
