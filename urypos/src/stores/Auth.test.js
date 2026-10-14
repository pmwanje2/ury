import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import { setImmediate as flushPromises } from 'node:timers/promises';
import { useAuthStore } from './Auth.js';
import { useInvoiceDataStore } from './invoiceData.js';
import Login from '../components/Login.vue';
import Header from '../components/Header.vue';
import { clearAppStorage } from '../../../packages/core/src/storage.ts';
import router from '../router';

const boundary = vi.hoisted(() => ({}));
vi.mock('../router', () => ({ default: { push: vi.fn().mockResolvedValue(undefined) } }));
vi.mock('./Table.js', () => ({ useTableStore: () => boundary.table }));
vi.mock('./Menu.js', () => ({ useMenuStore: () => boundary.menu }));
vi.mock('./Customer.js', () => ({ useCustomerStore: () => boundary.customer }));
vi.mock('./Notification.js', () => ({ useNotifications: () => ({ createNotification: vi.fn() }) }));
vi.mock('./recentOrder.js', () => ({ usetoggleRecentOrder: () => boundary.recent }));
vi.mock('./NotificationModal', () => ({ useNotificationModal: () => ({ showModal: vi.fn() }) }));
vi.mock('./Alert.js', () => ({ useAlert: () => ({ createAlert: vi.fn().mockResolvedValue(undefined) }) }));
vi.mock('./posOpening.js', () => ({ posOpening: () => ({}) }));
vi.mock('./posClosing.js', () => ({ posClosing: () => ({}) }));
vi.mock('./bottomTabs.js', () => ({ tabFunctions: () => ({ isLoginPage: false, currentTab: '/Table' }) }));
vi.mock('./utils/PrintWithQz', () => ({
  disconnectQzPrinter: vi.fn(), loadQzPrinter: vi.fn(), printWithQz: vi.fn(),
}));
vi.mock('./frappeSdk.js', async () => {
  const { FrappeApp } = await import('frappe-js-sdk');
  const app = new FrappeApp('http://localhost');
  return { default: { url: 'http://localhost', auth: () => app.auth(),
    db: () => ({ getDoc: vi.fn() }), call: () => ({ get: vi.fn(), post: vi.fn() }) } };
});

let auth;
let invoice;
let app;
let host;
const recentKey = 'ury_recent_waiters';

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  boundary.table = { selectedTable: 'T-05', selectedRoom: 'Dining', takeAwayTable: 0,
    previousOrderdItem: [], fetchTable: vi.fn(), fetchRoom: vi.fn() };
  boundary.menu = { items: [], cart: [{ item: 'Coffee', qty: 1 }], selectedOrderType: 'Dine In', pickOrderType: vi.fn() };
  boundary.customer = { search: 'Walk-in', numberOfPax: 2 };
  boundary.recent = { pastOrderdItem: [], viewRecentOrder: vi.fn() };
  setActivePinia(createPinia());
  auth = useAuthStore();
  invoice = useInvoiceDataStore();
  auth.sessionUser = 'alice@example.com';
  auth.userAuth = true;
  auth.cashier = false;
  invoice.posProfile = 'Salama POS';
  invoice.invoiceDetails = { floor_idle_signout_seconds: 60, floor_signout_after_send: 1 };
  vi.spyOn(auth, 'isPosOpenChecking').mockImplementation(() => {});
  vi.spyOn(auth, 'isPosCloseCheck').mockImplementation(() => {});
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true }));
  vi.stubGlobal('window', new Proxy(window, {
    get: (target, key) => key === 'location' ? boundary.location : Reflect.get(target, key),
  }));
  boundary.location = { href: '/urypos/Table', origin: 'http://localhost' };
  localStorage.setItem('userAuth', 'true');
  localStorage.setItem('selectedRoom', 'Dining');
  sessionStorage.setItem('posProfile', 'cached');
  vi.useFakeTimers();
});

afterEach(() => {
  app?.unmount();
  host?.remove();
  app = undefined;
  host = undefined;
  auth.stopIdle?.();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function loadRole({ cashier = false, seconds = 60 } = {}) {
  auth.db.getDoc.mockResolvedValue({ name: 'alice@example.com', username: 'alice', full_name: 'Alice Waiter',
    roles: [{ role: cashier ? 'URY Cashier' : 'URY Captain' }] });
  auth.call.get.mockResolvedValue({ message: {
    name: 'Salama POS', role_allowed_for_billing: [{ role: 'URY Cashier' }],
    transfer_role_permissions: [], role_restricted_for_table_order: [],
    custom_floor_idle_signout_seconds: seconds, custom_floor_signout_after_send: 1,
    view_all_status: 0, remove_items: 0, show_image: 0,
  } });
  auth.fetchUserRole();
  await flushPromises();
}

function mount(component) {
  document.body.append(host = document.createElement('div'));
  app = createApp(component);
  app.mount(host);
}

describe('floor tablet sign-out', () => {
  it('the waiter deadline ends the native session, discards the cart and returns to the in-app pad', async () => {
    await loadRole();
    await vi.advanceTimersByTimeAsync(59999);
    expect(fetch).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(fetch).toHaveBeenCalledWith('/api/method/logout', expect.objectContaining({ method: 'POST' }));
    expect(localStorage.getItem('userAuth')).toBeNull();
    expect(sessionStorage.getItem('posProfile')).toBeNull();
    expect(invoice.menu.cart).toEqual([]);
    expect(window.location.href).toBe('/urypos/login');
    expect(auth.idleStop).toBeNull();
    await vi.advanceTimersByTimeAsync(120000);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('a waiter waking after the deadline is signed out immediately', async () => {
    await loadRole();
    vi.setSystemTime(Date.now() + 120000);
    document.dispatchEvent(new Event('visibilitychange'));
    await flushPromises();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(window.location.href).toBe('/urypos/login');
  });

  it('billing users never auto-sign out, including on wake', async () => {
    await loadRole({ cashier: true });
    await vi.advanceTimersByTimeAsync(120000);
    window.dispatchEvent(new Event('focus'));
    expect(fetch).not.toHaveBeenCalled();
    expect(auth.userAuth).toBe(true);
  });

  it('zero disables idle sign-out', async () => {
    await loadRole({ seconds: 0 });
    await vi.advanceTimersByTimeAsync(120000);
    expect(fetch).not.toHaveBeenCalled();
  });

  it('refreshing roles replaces the timer and becoming a cashier stops it', async () => {
    await loadRole();
    await loadRole();
    expect(vi.getTimerCount()).toBe(1);
    await loadRole({ cashier: true });
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(120000);
    expect(fetch).not.toHaveBeenCalled();
  });

  it('Switch waiter uses native logout and preserves recent names only', async () => {
    const names = [{ username: 'alice', displayName: 'Alice Waiter' }];
    localStorage.setItem(recentKey, JSON.stringify(names));
    expect(auth.switchWaiter).toBeTypeOf('function');
    await auth.switchWaiter();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(window.location.href).toBe('/urypos/login');
    expect(JSON.parse(localStorage.getItem(recentKey))).toEqual(names);
  });

  it('failed logout keeps the current session and unsent cart for retry', async () => {
    fetch.mockResolvedValue({ ok: false });
    await auth.logOut();
    expect(auth.userAuth).toBe(true);
    expect(invoice.menu.cart).toHaveLength(1);
    expect(localStorage.getItem('userAuth')).toBe('true');
    expect(window.location.href).toBe('/urypos/Table');
    expect(auth.loggingOut).toBe(false);
    expect(auth.alert.createAlert).toHaveBeenCalled();
  });
});

describe('successful send is the only after-send sign-out path', () => {
  it('successful sync_order signs a waiter out', async () => {
    invoice.call.post.mockResolvedValue({ message: { name: 'POS-INV-TEST', status: 'Draft' } });
    await invoice.invoiceCreation();
    expect(invoice.call.post).toHaveBeenCalledWith('ury.ury.doctype.ury_order.ury_order.sync_order', expect.any(Object));
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(window.location.href).toBe('/urypos/login');
  });

  it.each(['server refusal', 'network failure'])('%s does not sign a waiter out or lose the cart', async (failure) => {
    if (failure === 'server refusal') invoice.call.post.mockResolvedValue({ message: { status: 'Failure' } });
    else invoice.call.post.mockRejectedValue(new Error('Offline'));
    await invoice.invoiceCreation();
    expect(fetch).not.toHaveBeenCalled();
    expect(invoice.menu.cart).toHaveLength(1);
    expect(auth.userAuth).toBe(true);
  });

  it.each([true, null])('billing or unclassified users (%s) stay signed in after a send', async (cashier) => {
    auth.cashier = cashier;
    invoice.call.post.mockResolvedValue({ message: { name: 'POS-INV-TEST', status: 'Draft' } });
    await invoice.invoiceCreation();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('disabled after-send retains normal floor navigation', async () => {
    invoice.invoiceDetails.floor_signout_after_send = 0;
    invoice.call.post.mockResolvedValue({ message: { name: 'POS-INV-TEST', status: 'Draft' } });
    await invoice.invoiceCreation();
    expect(fetch).not.toHaveBeenCalled();
    expect(router.push).toHaveBeenCalledWith('/Table');
  });
});

function nativeLogin() {
  const requests = [];
  auth.auth.axios.defaults.adapter = async config => {
    requests.push({ url: config.url, body: JSON.parse(config.data) });
    return { data: { message: 'Logged In', full_name: 'Alice Waiter' }, status: 200, statusText: 'OK', headers: {}, config };
  };
  vi.spyOn(auth, 'fetchUserDetails').mockResolvedValue(undefined);
  return requests;
}

describe('native username and private PIN', () => {
  it('leaves validation of existing cashier passwords to native Frappe login', async () => {
    nativeLogin();
    auth.userId = 'cashier';
    mount(Login);
    const pin = host.querySelector('input[name="currentPassword"]');
    pin.value = 'CashierTestOnly42';
    pin.dispatchEvent(new Event('input'));
    expect(pin.checkValidity()).toBe(true);
    expect(pin.maxLength).toBe(-1);
  });

  it('posts only usr and pwd to native login and clears the transient PIN', async () => {
    const requests = nativeLogin();
    auth.userId = 'alice';
    auth.currentPassword = '82649371'; // Synthetic credential, never a real PIN.
    await auth.login();
    expect(requests).toEqual([{ url: '/api/method/login', body: { usr: 'alice', pwd: '82649371' } }]);
    expect(auth.currentPassword).toBe('');
  });

  it('stores only usernames and display names, max six, most recent first, and survives clearing', async () => {
    nativeLogin();
    for (let n = 0; n < 8; n++) {
      auth.userId = `waiter${n}`;
      auth.currentPassword = '82649371';
      await auth.login();
    }
    auth.userId = 'waiter4';
    auth.currentPassword = '82649371';
    await auth.login();
    clearAppStorage();
    const names = JSON.parse(localStorage.getItem(recentKey));
    expect(names).toHaveLength(6);
    expect(names.map(row => row.username)).toEqual(['waiter4', 'waiter7', 'waiter6', 'waiter5', 'waiter3', 'waiter2']);
    for (const row of names) expect(Object.keys(row).sort()).toEqual(['displayName', 'username']);
    expect(names[0].displayName).toBe('Alice Waiter');
    expect(JSON.stringify(names)).not.toContain('82649371');
    expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain('82649371');
  });

  it('failed login does not record a recent waiter and clears the entered PIN', async () => {
    nativeLogin();
    auth.auth.axios.defaults.adapter = async () => { throw { response: {
      status: 401, statusText: 'Unauthorized', data: { message: 'Invalid login credentials' },
    } }; };
    auth.userAuth = false;
    auth.userId = 'alice';
    auth.currentPassword = '82649371';
    await auth.login();
    expect(localStorage.getItem(recentKey)).toBeNull();
    expect(auth.currentPassword).toBe('');
    expect(auth.alert.createAlert).toHaveBeenCalledWith('Message', 'Invalid login credentials', 'OK');
  });

  it('the pad labels username and numeric eight-digit PIN and recent chips select a name, not a secret', async () => {
    nativeLogin();
    auth.userId = 'alice';
    auth.currentPassword = '82649371';
    await auth.login();
    auth.currentPassword = '11111111';
    mount(Login);
    const username = host.querySelector('input[name="user_id"]');
    const pin = host.querySelector('input[name="currentPassword"]');
    expect(host.textContent).toContain('Username');
    expect(host.textContent).toContain('PIN');
    expect(pin.getAttribute('inputmode')).toBe('numeric');
    expect(pin.getAttribute('autocomplete')).toBe('off');
    expect(host.textContent).toContain('Recent waiters on this tablet');
    const chip = [...host.querySelectorAll('button')].find(button => button.textContent.includes('Alice Waiter'));
    expect(chip).toBeDefined();
    chip.click();
    await nextTick();
    expect(username.value).toBe('alice');
    expect(pin.value).toBe('');
    expect(document.activeElement).toBe(pin);
  });

  it('the pad reads recent usernames after a new page load', () => {
    localStorage.setItem(recentKey, JSON.stringify([{ username: 'alice', displayName: 'Alice Waiter' }]));
    setActivePinia(createPinia());
    auth = useAuthStore();
    mount(Login);
    expect(host.textContent).toContain('Alice Waiter');
    expect(host.textContent).toContain('alice');
  });

  it('a corrupt recent-name cache does not prevent sign-in', () => {
    localStorage.setItem(recentKey, 'invalid JSON');
    setActivePinia(createPinia());
    auth = useAuthStore();
    mount(Login);
    expect(host.querySelector('input[name="user_id"]')).not.toBeNull();
  });

  it('the header exposes Switch waiter directly and Change my PIN links to native self-service', async () => {
    mount(Header);
    const switchButton = [...host.querySelectorAll('button')].find(button => button.textContent.trim() === 'Switch waiter');
    expect(switchButton).toBeDefined();
    switchButton.click();
    await flushPromises();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(window.location.href).toBe('/urypos/login');
    const change = host.querySelector('a[href="/update-password"]');
    expect(change?.textContent.trim()).toBe('Change my PIN');
  });
});
