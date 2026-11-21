import { afterEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useAuthStore } from '../src/stores/Auth.js';
import router from '../src/router';

vi.mock('../src/router', () => ({ default: { push: vi.fn().mockResolvedValue(undefined) } }));
vi.mock('../src/stores/Menu.js', () => ({ useMenuStore: () => ({}) }));
vi.mock('../src/stores/Table.js', () => ({ useTableStore: () => ({}) }));
vi.mock('../src/stores/invoiceData.js', () => ({ useInvoiceDataStore: () => ({}) }));
vi.mock('../src/stores/utils/PrintWithQz', () => ({ disconnectQzPrinter: vi.fn() }));
vi.mock('../src/stores/frappeSdk.js', () => ({ default: { call: () => ({}), db: () => ({}), auth: () => ({logout: async () => {}}) } }));
afterEach(() => vi.unstubAllGlobals());
it('forgets cached POS reads before navigating after legacy POS logout', async () => {
  setActivePinia(createPinia());
  const remove = vi.fn().mockResolvedValue(true);
  vi.stubGlobal('caches', {keys: async () => ['ury-pos-shell', 'other'], delete: remove});
  await useAuthStore().logOut();
  await vi.waitFor(() => expect(router.push).toHaveBeenCalledWith('/login'));
  expect(remove.mock.calls).toEqual([['ury-pos-shell']]);
  expect(remove.mock.invocationCallOrder[0]).toBeLessThan(router.push.mock.invocationCallOrder[0]);
});
