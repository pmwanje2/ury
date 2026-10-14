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
  auth.staySignedIn = false;
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

function mockRole({ cashier = false, seconds = 60, roles, stayRoles = [] } = {}) {
  const userRoles = roles || [cashier ? 'URY Cashier' : 'URY Captain'];
  auth.db.getDoc.mockResolvedValue({ name: 'alice@example.com', username: 'alice', full_name: 'Alice Waiter',
    roles: userRoles.map((role) => ({ role })) });
  auth.call.get.mockResolvedValue({ message: {
    name: 'Salama POS', role_allowed_for_billing: [{ role: 'URY Cashier' }],
    transfer_role_permissions: [], role_restricted_for_table_order: [],
    custom_floor_stay_signed_in_roles: stayRoles.map((role) => ({ role })),
    custom_floor_idle_signout_seconds: seconds, custom_floor_signout_after_send: 1,
    view_all_status: 0, remove_items: 0, show_image: 0,
  } });
}

async function loadRole(options) {
  mockRole(options);
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

  it('a listed non-billing role stays signed in after idle', async () => {
    await loadRole({ roles: ['URY Captain'], stayRoles: ['URY Captain'] });
    expect(auth.cashier).toBe(false);
    expect(boundary.menu.pickOrderType).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(120000);
    window.dispatchEvent(new Event('focus'));
    expect(fetch).not.toHaveBeenCalled();
    expect(auth.userAuth).toBe(true);
    expect(auth.staySignedIn).toBe(true);
  });

  it('a billing role that is not listed is signed out after idle', async () => {
    await loadRole({ cashier: true, stayRoles: ['URY Manager'] });
    expect(auth.cashier).toBe(true);
    expect(boundary.menu.pickOrderType).toHaveBeenCalled();
    expect(auth.staySignedIn).toBe(false);
    await vi.advanceTimersByTimeAsync(60000);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(window.location.href).toBe('/urypos/login');
  });

  it('an empty stay list signs everyone out on idle when seconds are above zero', async () => {
    await loadRole({ cashier: true, stayRoles: [], seconds: 60 });
    expect(auth.cashier).toBe(true);
    expect(auth.staySignedIn).toBe(false);
    await vi.advanceTimersByTimeAsync(60000);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(window.location.href).toBe('/urypos/login');
  });

  it('zero disables idle sign-out', async () => {
    await loadRole({ seconds: 0 });
    await vi.advanceTimersByTimeAsync(120000);
    expect(fetch).not.toHaveBeenCalled();
  });

  it('refreshing roles replaces the timer and an unlisted billing role does not stop it', async () => {
    await loadRole();
    await loadRole();
    expect(vi.getTimerCount()).toBe(1);
    await loadRole({ cashier: true, stayRoles: [] });
    expect(auth.cashier).toBe(true);
    expect(auth.staySignedIn).toBe(false);
    expect(vi.getTimerCount()).toBe(1);
    await vi.advanceTimersByTimeAsync(60000);
    expect(fetch).toHaveBeenCalledTimes(1);
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

  it.each(['HTTP', 'network'])('%s logout failure still locks the tablet locally', async (failure) => {
    await loadRole();
    const names = [{ username: 'alice', displayName: 'Alice Waiter' }];
    localStorage.setItem(recentKey, JSON.stringify(names));
    auth.currentPassword = '82649371'; // Synthetic credential.
    if (failure === 'network') fetch.mockRejectedValue(new Error('Offline'));
    else fetch.mockResolvedValue({ ok: false, status: 503 });
    await auth.logOut();
    expect(auth.userAuth).toBe(false);
    expect(auth.currentPassword).toBe('');
    expect(invoice.menu.cart).toEqual([]);
    expect(localStorage.getItem('userAuth')).toBeNull();
    expect(localStorage.getItem('selectedRoom')).toBeNull();
    expect(sessionStorage.getItem('posProfile')).toBeNull();
    expect(JSON.parse(localStorage.getItem(recentKey))).toEqual(names);
    expect(window.location.href).toBe('/urypos/login');
    expect(auth.idleStop).toBeNull();
    await auth.logOut();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('a rejected idle logout still discards the cart and goes to the PIN pad', async () => {
    await loadRole();
    fetch.mockRejectedValue(new Error('Offline'));
    await vi.advanceTimersByTimeAsync(60000);
    expect(auth.userAuth).toBe(false);
    expect(invoice.menu.cart).toEqual([]);
    expect(localStorage.getItem('userAuth')).toBeNull();
    expect(window.location.href).toBe('/urypos/login');
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

  it('an unclassified stay state does not sign out after a send', async () => {
    auth.cashier = null;
    auth.staySignedIn = null;
    invoice.call.post.mockResolvedValue({ message: { name: 'POS-INV-TEST', status: 'Draft' } });
    await invoice.invoiceCreation();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('after send keeps a listed role signed in', async () => {
    auth.cashier = false;
    auth.staySignedIn = true;
    invoice.call.post.mockResolvedValue({ message: { name: 'POS-INV-TEST', status: 'Draft' } });
    await invoice.invoiceCreation();
    expect(fetch).not.toHaveBeenCalled();
    expect(auth.userAuth).toBe(true);
    expect(router.push).toHaveBeenCalledWith('/Table');
  });

  it('after send signs out a billing user whose role is not listed', async () => {
    auth.cashier = true;
    auth.staySignedIn = false;
    invoice.call.post.mockResolvedValue({ message: { name: 'POS-INV-TEST', status: 'Draft' } });
    await invoice.invoiceCreation();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(window.location.href).toBe('/urypos/login');
    expect(router.push).not.toHaveBeenCalledWith('/recentOrder');
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

function pageSession({ roles = ['URY Manager'], stayRoles = ['URY Manager'] } = {}) {
  clearAppStorage();
  setActivePinia(createPinia());
  auth = useAuthStore();
  invoice = useInvoiceDataStore();
  vi.spyOn(auth, 'isPosOpenChecking').mockImplementation(() => {});
  vi.spyOn(auth, 'isPosCloseCheck').mockImplementation(() => {});
  const requests = [];
  auth.auth.axios.defaults.adapter = async config => {
    requests.push(config.url);
    return { data: { message: 'manager@example.com' }, status: 200, statusText: 'OK', headers: {}, config };
  };
  vi.spyOn(invoice, 'fetchInvoiceDetails').mockImplementation(async () => {
    invoice.posProfile = 'Salama POS';
  });
  mockRole({ roles, stayRoles, seconds: 0 });
  router.push.mockClear();
  return requests;
}

describe('ERP sessions without a local PIN sign-in marker', () => {
  it('a listed role accepts the live session and reaches /Table without the pad', async () => {
    const requests = pageSession();
    auth.fetchUserDetails();
    await flushPromises();
    expect(requests).toEqual(['/api/method/frappe.auth.get_logged_user']);
    expect(auth.db.getDoc).toHaveBeenCalledWith('User', 'manager@example.com');
    expect(auth.call.get).toHaveBeenCalledWith('frappe.client.get', { doctype: 'POS Profile', name: 'Salama POS' });
    expect(auth.staySignedIn).toBe(true);
    expect(auth.userAuth).toBe(true);
    expect(localStorage.getItem('userAuth')).toBe('true');
    expect(router.push).toHaveBeenCalledWith('/Table');
    expect(router.push).not.toHaveBeenCalledWith('/login');
    expect(fetch).not.toHaveBeenCalled();
  });

  it('an unlisted role ends the live session and sees the PIN pad', async () => {
    pageSession({ roles: ['URY Captain'], stayRoles: ['URY Manager'] });
    auth.fetchUserDetails();
    await flushPromises();
    expect(fetch).toHaveBeenCalledWith('/api/method/logout', expect.objectContaining({ method: 'POST' }));
    expect(auth.userAuth).toBeFalsy();
    expect(localStorage.getItem('userAuth')).toBeNull();
    expect(router.push).not.toHaveBeenCalledWith('/Table');
    expect(boundary.table.fetchRoom).not.toHaveBeenCalled();
    expect(window.location.href).toBe('/urypos/login');
    mount(Login);
    expect(host.querySelector('input[name="currentPassword"]')).not.toBeNull();
  });

  it('an empty stay list ends even a manager session and shows the PIN pad', async () => {
    pageSession({ stayRoles: [] });
    auth.fetchUserDetails();
    await flushPromises();
    expect(fetch).toHaveBeenCalledWith('/api/method/logout', expect.objectContaining({ method: 'POST' }));
    expect(auth.userAuth).toBeFalsy();
    expect(localStorage.getItem('userAuth')).toBeNull();
    expect(router.push).not.toHaveBeenCalledWith('/Table');
    expect(window.location.href).toBe('/urypos/login');
  });

  it('does not admit the session while the POS Profile role list is still loading', async () => {
    pageSession();
    let releaseProfile;
    auth.call.get.mockImplementationOnce(() => new Promise(resolve => { releaseProfile = resolve; }));
    auth.fetchUserDetails();
    await flushPromises();
    expect(auth.call.get).toHaveBeenCalledWith('frappe.client.get', { doctype: 'POS Profile', name: 'Salama POS' });
    expect(auth.userAuth).toBeFalsy();
    expect(localStorage.getItem('userAuth')).toBeNull();
    expect(router.push).not.toHaveBeenCalledWith('/Table');
    releaseProfile({ message: {
      role_allowed_for_billing: [], custom_floor_stay_signed_in_roles: [{ role: 'URY Manager' }],
      transfer_role_permissions: [], role_restricted_for_table_order: [],
    } });
    await flushPromises();
    expect(auth.userAuth).toBe(true);
    expect(router.push).toHaveBeenCalledWith('/Table');
  });

  it('a failed POS Profile lookup cannot reuse a previously listed role', async () => {
    pageSession();
    auth.staySignedIn = true;
    auth.call.get.mockRejectedValue(new Error('POS Profile unavailable'));
    vi.spyOn(console, 'error').mockImplementation(() => {});
    auth.fetchUserDetails();
    await flushPromises();
    expect(fetch).toHaveBeenCalledWith('/api/method/logout', expect.objectContaining({ method: 'POST' }));
    expect(auth.userAuth).toBeFalsy();
    expect(localStorage.getItem('userAuth')).toBeNull();
    expect(router.push).not.toHaveBeenCalledWith('/Table');
    expect(window.location.href).toBe('/urypos/login');
  });

  it('a session bypass never adds the manager to recent waiters', async () => {
    pageSession();
    const names = [{ username: 'alice', displayName: 'Alice Waiter' }];
    localStorage.setItem(recentKey, JSON.stringify(names));
    auth.fetchUserDetails();
    await flushPromises();
    expect(router.push).toHaveBeenCalledWith('/Table');
    expect(JSON.parse(localStorage.getItem(recentKey))).toEqual(names);
  });

  it('PIN login still admits an unlisted waiter and records their recent name', async () => {
    const requests = pageSession({ roles: ['URY Captain'], stayRoles: ['URY Manager'] });
    auth.auth.axios.defaults.adapter = async config => {
      requests.push({ url: config.url, body: config.data ? JSON.parse(config.data) : undefined });
      const data = config.url === '/api/method/login'
        ? { message: 'Logged In', full_name: 'Alice Waiter' }
        : { message: 'alice@example.com' };
      return { data, status: 200, statusText: 'OK', headers: {}, config };
    };
    auth.userId = 'alice';
    auth.currentPassword = '82649371'; // Synthetic credential.
    await auth.login();
    await flushPromises();
    expect(requests).toEqual([
      { url: '/api/method/login', body: { usr: 'alice', pwd: '82649371' } },
      { url: '/api/method/frappe.auth.get_logged_user', body: undefined },
    ]);
    expect(auth.staySignedIn).toBe(false);
    expect(auth.userAuth).toBe(true);
    expect(localStorage.getItem('userAuth')).toBe('true');
    expect(router.push).toHaveBeenCalledWith('/Table');
    expect(auth.currentPassword).toBe('');
    expect(JSON.parse(localStorage.getItem(recentKey))).toEqual([{ username: 'alice', displayName: 'Alice Waiter' }]);
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe('native username and private PIN', () => {
  it('a surviving server session cannot resume after failed logout without a fresh PIN login', async () => {
    fetch.mockRejectedValue(new Error('Offline'));
    await auth.logOut();
    // Simulate the new page while the server still recognizes the old cookie.
    setActivePinia(createPinia());
    auth = useAuthStore();
    invoice = useInvoiceDataStore();
    let serverUser = 'alice@example.com';
    const requests = [];
    auth.auth.axios.defaults.adapter = async config => {
      requests.push({ url: config.url, body: config.data ? JSON.parse(config.data) : undefined });
      if (config.url === '/api/method/login') {
        serverUser = 'bob@example.com';
        return { data: { message: 'Logged In', full_name: 'Bob Waiter' }, status: 200, statusText: 'OK', headers: {}, config };
      }
      return { data: { message: serverUser }, status: 200, statusText: 'OK', headers: {}, config };
    };
    vi.spyOn(invoice, 'fetchInvoiceDetails').mockResolvedValue(undefined);
    mockRole({ roles: ['URY Captain'], stayRoles: ['URY Manager'], seconds: 0 });
    vi.spyOn(auth, 'isPosOpenChecking').mockImplementation(() => {});
    vi.spyOn(auth, 'isPosCloseCheck').mockImplementation(() => {});
    router.push.mockClear();
    auth.fetchUserDetails(); // App.vue's mount-time session check.
    await flushPromises();
    expect(requests).toEqual([{ url: '/api/method/frappe.auth.get_logged_user', body: undefined }]);
    expect(auth.userAuth).toBeFalsy();
    expect(window.location.href).toBe('/urypos/login');
    expect(localStorage.getItem('userAuth')).toBeNull();
    expect(router.push).not.toHaveBeenCalled();

    mount(Login);
    const pin = host.querySelector('input[name="currentPassword"]');
    expect(pin.value).toBe('');
    expect(pin.checkValidity()).toBe(false);
    auth.userId = 'bob';
    auth.currentPassword = '82649371'; // Synthetic credential.
    await auth.login();
    await flushPromises();
    expect(requests).toEqual([
      { url: '/api/method/frappe.auth.get_logged_user', body: undefined },
      { url: '/api/method/login', body: { usr: 'bob', pwd: '82649371' } },
      { url: '/api/method/frappe.auth.get_logged_user', body: undefined },
    ]);
    expect(auth.sessionUser).toBe('bob@example.com');
    expect(auth.currentPassword).toBe('');
    expect(router.push).toHaveBeenCalledWith('/Table');
  });

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

  it('the login screen links Forgot PIN to /login#forgot', () => {
    mount(Login);
    const link = host.querySelector('a[href="/login#forgot"]');
    expect(link?.textContent.trim()).toBe('Forgot PIN?');
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
