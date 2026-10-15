import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createApp, nextTick, reactive } from 'vue';
import { setImmediate as flushPromises } from 'node:timers/promises';
import Login from './Login.vue';
import Header from './Header.vue';

const state = vi.hoisted(() => ({}));
vi.mock('../stores/Auth.js', () => ({ useAuthStore: () => state.auth }));
vi.mock('../stores/invoiceData.js', () => ({ useInvoiceDataStore: () => ({ posProfile: 'Salama POS' }) }));
vi.mock('../stores/Table.js', () => ({ useTableStore: () => ({}) }));
vi.mock('../stores/posOpening.js', () => ({ posOpening: () => ({}) }));
vi.mock('../stores/posClosing.js', () => ({ posClosing: () => ({}) }));
vi.mock('../stores/bottomTabs.js', () => ({ tabFunctions: () => ({ isLoginPage: false, currentTab: '/Table' }) }));
let app, host;
beforeEach(() => {
  window.floor_pad = { paired: true, till_open: true };
  state.auth = reactive({ sessionUser: 'manager@example.test', activeDropdown: true, pinSession: false,
    recentWaiters: [{ username: 'alice', displayName: 'Alice Waiter' }],
    pinSignIn: vi.fn().mockRejectedValue(new Error('Refused')), login: vi.fn(),
    getLoginAvatar: () => 'Manager', call: { get: vi.fn(), post: vi.fn() } });
  state.auth.call.get.mockImplementation((method) => Promise.resolve({ message:
    method === 'frappe.client.has_permission' ? { has_permission: true } :
      { name: 'Salama POS', branch: 'Salama', user: [{ user: 'waiter@example.test' }] } }));
  localStorage.clear(); sessionStorage.clear();
});
afterEach(() => { app?.unmount(); host?.remove(); delete window.floor_pad; vi.restoreAllMocks(); });
function mount(component) {
  document.body.append(host = document.createElement('div'));
  (app = createApp(component)).mount(host);
}
function button(text) { return [...host.querySelectorAll('button')].find(node => node.textContent.trim() === text); }
async function click(text) { expect(button(text), `Button ${text}`).toBeDefined(); button(text).click(); await flushPromises(); await nextTick(); }

it('pad shown only when paired and till open, with a username fallback', async () => {
  mount(Login);
  expect(host.querySelector('[aria-label="4-digit PIN"]')).not.toBeNull();
  expect(host.querySelector('input[name="user_id"]')).toBeNull();
  await click('Sign in with username');
  expect(host.querySelector('input[name="user_id"]')).not.toBeNull();
  expect(host.textContent).toContain('Password');
  expect(host.querySelector('a[href="/login#forgot"]').textContent).toBe('Forgot password?');
});
it.each([{ paired: true, till_open: false }, { paired: false, till_open: true }])(
  'username form shown when till closed or tablet unpaired: %j', (context) => {
    window.floor_pad = context; mount(Login);
    expect(host.querySelector('input[name="user_id"]')).not.toBeNull();
    expect(host.querySelector('[aria-label="4-digit PIN"]')).toBeNull();
  });
it('pad auto-submits on fourth digit, masks digits and clears on failure', async () => {
  mount(Login);
  for (const digit of ['0', '5', '2']) await click(digit);
  expect(state.auth.pinSignIn).not.toHaveBeenCalled();
  expect(host.textContent).not.toContain('052');
  await click('7');
  expect(state.auth.pinSignIn).toHaveBeenCalledExactlyOnceWith('0527');
  expect(host.querySelector('[aria-label="4-digit PIN"]').textContent).not.toContain('●');
  expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain('0527');
});
it('recent waiter chips are gone even with a pre-existing name cache', () => {
  localStorage.setItem('ury_recent_waiters', '[{"username":"alice","displayName":"Alice Waiter"}]');
  mount(Login);
  expect(host.textContent).not.toContain('Recent waiters');
  expect(host.textContent).not.toContain('Alice Waiter');
});
it.each([false, true])('manager menu requires native profile write: %s', async (allowed) => {
  state.auth.call.get.mockResolvedValue({ message: { has_permission: allowed } });
  mount(Header); await flushPromises();
  expect(state.auth.call.get).toHaveBeenCalledWith('frappe.client.has_permission', {
    doctype: 'POS Profile', docname: 'Salama POS', perm_type: 'write',
  });
  expect(!!button('Pair this tablet')).toBe(allowed);
  expect(!!button('Staff PINs')).toBe(allowed);
});
it('pairing sends the tablet label to the server and never stores a device credential', async () => {
  state.auth.call.post.mockResolvedValue({ message: {} });
  mount(Header); await flushPromises();
  await click('Pair this tablet');
  const input = host.querySelector('input[name="tablet_label"]');
  expect(input).not.toBeNull(); input.value = 'Floor tablet A'; input.dispatchEvent(new Event('input'));
  await click('Pair tablet');
  expect(state.auth.call.post).toHaveBeenCalledWith('ury.ury_pos.floor_pin.pair_device', {
    pos_profile: 'Salama POS', label: 'Floor tablet A',
  });
  expect(localStorage.length + sessionStorage.length).toBe(0);
});
it('issued PIN is shown once and cleared on Done and close', async () => {
  state.auth.call.post.mockResolvedValue({ message: { pin: '0527' } });
  mount(Header); await flushPromises(); await click('Staff PINs');
  expect(state.auth.call.get).toHaveBeenCalledWith('frappe.client.get', { doctype: 'Branch', name: 'Salama' });
  await click('Issue new PIN');
  expect(state.auth.call.post).toHaveBeenCalledWith('ury.ury_pos.floor_pin.issue_pin', { user: 'waiter@example.test' });
  expect(host.textContent).toContain('0527');
  expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain('0527');
  await click('Done'); expect(host.textContent).not.toContain('0527');
  await click('Close'); await click('Staff PINs');
  expect(host.textContent).not.toContain('0527');
});
it('PIN sessions do not offer password change', async () => {
  state.auth.pinSession = true; mount(Header); await nextTick();
  expect(host.querySelector('a[href="/update-password"]')).toBeNull();
});
