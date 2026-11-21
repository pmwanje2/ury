import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useAuthStore } from '../src/stores/Auth.js';
import router from '../src/router';

vi.mock('../src/router', () => ({ default: { push: vi.fn().mockResolvedValue(undefined) } }));
vi.mock('../src/stores/Menu.js', () => ({ useMenuStore: () => ({}) }));
vi.mock('../src/stores/Table.js', () => ({ useTableStore: () => ({}) }));
vi.mock('../src/stores/invoiceData.js', () => ({ useInvoiceDataStore: () => ({}) }));
vi.mock('../src/stores/utils/PrintWithQz', () => ({ disconnectQzPrinter: vi.fn() }));
vi.mock('../src/stores/frappeSdk.js', () => ({ default: { call: () => ({}), db: () => ({}), auth: () => ({logout: async () => {}}) } }));
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  vi.stubGlobal('csrf_token', 'test-token');
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}')));
});
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

it('posts once with CSRF; keeps the session on refusal and permits retry', async () => {
  setActivePinia(createPinia());
  const store = useAuthStore();
  store.alert.createAlert = vi.fn();
  let finish;
  fetch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  localStorage.setItem('userAuth', 'true');
  localStorage.setItem('unrelated', 'keep');
  sessionStorage.setItem('ury.setup.configureState', 'private');
  const pending = store.logOut();
  store.logOut();
  expect(fetch).toHaveBeenCalledTimes(1);
  const [url, options] = fetch.mock.calls[0];
  expect(url).toBe('/api/method/logout');
  expect(options.method).toBe('POST');
  expect(new Headers(options.headers).get('X-Frappe-CSRF-Token')).toBe('test-token');
  finish(new Response('{}', { status: 403 }));
  await pending;
  expect(localStorage.getItem('userAuth')).toBe('true');
  expect(sessionStorage.getItem('ury.setup.configureState')).toBe('private');
  expect(router.push).not.toHaveBeenCalled();
  expect(store.alert.createAlert).toHaveBeenCalled();
  await store.logOut();
  expect(router.push).toHaveBeenCalledWith('/login');
  expect(localStorage.getItem('userAuth')).toBeNull();
  expect(sessionStorage.getItem('ury.setup.configureState')).toBeNull();
  expect(localStorage.getItem('unrelated')).toBe('keep');
});
