import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createApp, nextTick, reactive } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';
import BottomTabs from '../src/components/bottomTabs.vue';

const state = vi.hoisted(() => ({}));
vi.mock('../src/stores/Auth.js', () => ({ useAuthStore: () => state.auth }));
vi.mock('../src/stores/invoiceData.js', () => ({ useInvoiceDataStore: () => state.invoice }));
vi.mock('../src/stores/bottomTabs.js', () => ({ tabFunctions: () => state.tabs }));
let app, host, router;
beforeEach(() => {
  state.auth = reactive({ cashier: false });
  state.invoice = reactive({ invoiceUpdating: false });
  state.tabs = reactive({ isLoginPage: false, currentTab: '/Table', clickMenuTab: vi.fn(), checkActiveTable: vi.fn() });
});
afterEach(() => { app?.unmount(); host?.remove(); });
async function mount() {
  router = createRouter({ history: createMemoryHistory(), routes: ['/Table', '/Menu', '/Customer', '/Cart', '/recentOrder'].map(path => ({ path, component: { template: '<div />' } })) });
  await router.push('/Table'); await router.isReady();
  document.body.append(host = document.createElement('div'));
  app = createApp(BottomTabs); app.use(router); app.mount(host); await nextTick();
}
function links() { return [...host.querySelectorAll('a')]; }
function link(text) { return links().find(node => node.textContent.trim() === text); }
it('offers waiter Table, Menu and Cart without Customer registration or OrderLog', async () => {
  await mount(); expect(links().map(node => node.textContent.trim())).toEqual(['Table', 'Menu', 'Cart']);
  expect(links().map(node => node.getAttribute('href'))).toEqual(['/Table', '/Menu', '/Cart']);
  link('Menu').click(); await nextTick(); expect(state.tabs.clickMenuTab).toHaveBeenCalledOnce();
  link('Cart').click(); await nextTick(); expect(state.tabs.checkActiveTable).toHaveBeenCalledOnce();
});
it('retains cashier Customer and OrderLog routes', async () => {
  state.auth.cashier = true; await mount();
  expect(links().map(node => node.textContent.trim())).toEqual(['Table', 'Menu', 'Customer', 'Cart', 'OrderLog']);
  expect(link('Customer').getAttribute('href')).toBe('/Customer'); expect(link('OrderLog').getAttribute('href')).toBe('/recentOrder');
  link('Cart').click(); await nextTick(); expect(state.tabs.checkActiveTable).not.toHaveBeenCalled();
});
it.each([false, true])('preserves invoice-update navigation guards (cashier=%s)', async cashier => {
  state.auth.cashier = cashier; state.invoice.invoiceUpdating = true; await mount();
  for (const node of links().filter(node => node.textContent.trim() !== 'Cart')) expect(node.getAttribute('href')).toBe('/Table#');
  expect(link('Cart').getAttribute('href')).toBe('/Cart');
});
it('hides navigation on the login page', async () => { state.tabs.isLoginPage = true; await mount(); expect(links()).toHaveLength(0); });
