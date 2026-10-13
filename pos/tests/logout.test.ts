import assert from 'node:assert/strict';
import test from 'node:test';
import { resolve } from 'node:path';
import { environment, loadModule } from '../../packages/core/test/logout-harness.mjs';

function header(env: ReturnType<typeof environment>) {
  let state: unknown[] = [true];
  let index = 0;
  const react = {
    useState(initial: unknown) {
      const slot = index++;
      if (!(slot in state)) state[slot] = initial;
      return [state[slot], (value: unknown) => { state[slot] = value; }];
    },
    useRef() {
      const slot = index++;
      if (!(slot in state)) state[slot] = { current: null };
      return state[slot];
    },
    useEffect() {},
    createElement: (type: unknown, props: any, ...children: unknown[]) => ({ type, props, children }),
  };
  const ui = { Button: 'button', Input: 'input', showToast: {
    error: (message: string) => env.errors.push(message),
  } };
  // Use the real core implementation (the Frappe SDK is a network boundary).
  const auth = loadModule(resolve('packages/core/src/frappe/auth.ts'), env.globals, {
    './client': { auth: { logout: () => env.globals.fetch('/api/method/logout', { method: 'POST' }) } },
  });
  const core = loadModule(resolve('packages/core/src/index.ts'), env.globals, {
    './frappe/client': {}, './frappe/auth': auth, './frappe/roles': {},
    './frappe/errors': {}, './types': {}, './format': {}, './print/qz': {},
    './utils/validateField': {},
  });
  const store = { user: { name: 'alice@example.com', roles: ['URY Cashier'] }, searchQuery: '', orderSearchQuery: '' };
  const Header = loadModule(resolve('pos/src/components/Header.tsx'), env.globals, {
    react, '@ury/ui': ui, '@ury/core': core,
    '../i18n': { t: (key: string) => key },
    'react-router-dom': { Link: 'a', useLocation: () => ({ pathname: '/dashboard' }) },
    '../store/root-store': { useRootStore: (select?: any) => select ? select(store) : store },
    '../store/pos-store': { usePOSStore: () => store },
  }).default;
  return () => {
    index = 0;
    const buttons: any[] = [];
    const visit = (node: any) => {
      if (!node || typeof node !== 'object') return;
      if (node.type === 'button') buttons.push(node);
      node.children?.flat(Infinity).forEach(visit);
    };
    visit(Header());
    return (label: string) => buttons.find((button) => button.children.includes(label));
  };
}

test('POS: logout includes CSRF, clears only URY state and retains only the till return path', async () => {
  const env = environment();
  env.globals.window.location.href = '/pos/orders?invoice=PRIVATE-BILL';
  await header(env)()('header.logout').props.onClick();
  assert.equal(env.calls[0][1].headers?.['X-Frappe-CSRF-Token'], 'session-token');
  assert.equal(env.globals.window.location.href, '/login?redirect-to=%2Fpos');
  assert.equal(env.globals.localStorage.getItem('posOrderTabsData'), null);
  assert.equal(env.globals.sessionStorage.getItem('posProfile'), null);
  assert.equal(env.globals.sessionStorage.getItem('ury.setup.configureState'), null);
  assert.equal(env.globals.localStorage.getItem('ury_active_branch_id'), null);
  assert.equal(env.globals.localStorage.getItem('ury_language'), 'fr');
  assert.equal(env.globals.localStorage.getItem('deskPreference'), 'keep');
});

test('POS: successful logout stays disabled and blocks requests while navigating', async () => {
  const env = environment();
  const render = header(env);
  await render()('header.logout').props.onClick();
  const button = render()('header.logout');
  const disabled = button.props.disabled;
  await button.props.onClick();
  assert.equal(disabled, true);
  assert.equal(env.calls.length, 1);
  assert.equal(env.globals.window.location.href, '/login?redirect-to=%2Fpos');
});

for (const failure of ['HTTP', 'network']) {
  test(`POS: rejected ${failure} logout re-enables its button and reports an error`, async () => {
    const env = environment();
    env.globals.fetch = async () => {
      if (failure === 'network') throw new Error('Network unavailable');
      return { ok: false, status: 403 };
    };
    const render = header(env);
    await render()('header.logout').props.onClick();
    assert.equal(env.globals.window.location.href, '/current');
    assert.ok(env.errors[0]);
    assert.equal(env.globals.localStorage.getItem('posOrderTabsData'), 'legacy carts');
    assert.equal(Boolean(render()('header.logout').props.disabled), false);
  });
}

test('POS: pending logout disables its button and sends only one request', async () => {
  const env = environment();
  let complete: (value: unknown) => void;
  env.globals.fetch = (...args: unknown[]) => {
    env.calls.push(args);
    return new Promise((resolve) => { complete = resolve; });
  };
  const render = header(env);
  const click = render()('header.logout').props.onClick;
  const first = click();
  const second = click();
  const disabled = render()('header.logout').props.disabled;
  const requests = env.calls.length;
  complete!({ ok: true, status: 200 });
  if (requests === 1) await Promise.all([first, second]);
  assert.equal(disabled, true);
  assert.equal(requests, 1);
});

test('POS: Clear Cache preserves Desk storage and self-ordering credentials', () => {
  const env = environment();
  header(env)()('header.clear_cache').props.onClick();
  assert.equal(env.globals.localStorage.getItem('deskPreference'), 'keep');
  assert.equal(env.globals.localStorage.getItem('ury_device_credential'), 'keep');
  assert.equal(env.globals.sessionStorage.getItem('deskSession'), 'keep');
  assert.equal(env.globals.localStorage.getItem('posOrderTabsData'), null);
  assert.equal(env.globals.sessionStorage.getItem('posProfile'), null);
  assert.equal(env.globals.sessionStorage.getItem('ury.setup.configureState'), null);
  assert.equal(env.globals.localStorage.getItem('ury_active_branch_id'), null);
  assert.equal(env.globals.localStorage.getItem('ury_language'), 'fr');
});

function openingScreen(env: ReturnType<typeof environment>, occupied: boolean) {
  const context = {
    company: 'Test Co', user: 'alice@example.com', selected_profile: 'POS-1',
    allowed_profiles: [{ name: 'POS-1' }], payment_modes: [],
    permissions: { create: true, submit: true }, daily_close_pending: false,
    multi_cashier: { enabled: false },
    occupied_entry: occupied ? { name: 'OPEN-1', user_full_name: 'Opener',
      period_start_date: '2026-10-11 08:00:00', joinable: false } : null,
  };
  const state: unknown[] = [context, false, false, null, [], false];
  let index = 0;
  const react = {
    useState(initial: unknown) {
      const slot = index++;
      if (!(slot in state)) state[slot] = initial;
      return [state[slot], (value: unknown) => { state[slot] = value; }];
    },
    useRef(initial: unknown) {
      const slot = index++;
      if (!(slot in state)) state[slot] = { current: initial };
      return state[slot];
    },
    useMemo: (compute: () => unknown) => compute(),
    useEffect() {},
    createElement: (type: unknown, props: any, ...children: unknown[]) => ({ type, props, children }),
  };
  const core = loadModule(resolve('packages/core/src/index.ts'), env.globals, {
    './frappe/client': {}, './frappe/auth': {}, './frappe/roles': {},
    './frappe/errors': {}, './types': {}, './format': { formatCurrency: String },
    './print/qz': {}, './utils/validateField': {},
  });
  const Screen = loadModule(resolve('pos/src/components/POSOpeningScreen.tsx'), env.globals, {
    react, '@ury/core': core,
    '@ury/ui': { Button: 'button', Badge: 'span', showToast: { error: (message: string) => env.errors.push(message) } },
    '../store/root-store': { useRootStore: () => ({ user: null }) },
    '../lib/pos-opening-api': {},
    '../i18n': { t: (key: string) => key, getActiveLanguage: () => 'en' },
    './POSOpeningPaymentTable': {},
  }).default;
  return () => {
    index = 0;
    const buttons: any[] = [];
    const visit = (node: any) => {
      if (!node || typeof node !== 'object') return;
      if (node.type === 'button') buttons.push(node);
      node.children?.flat(Infinity).forEach(visit);
    };
    visit(Screen({}));
    const button = buttons.find((button) => button.children.includes('header.logout'));
    assert.ok(button, 'Opening and occupied screens must expose Log out');
    return button;
  };
}

for (const occupied of [false, true]) {
  const screen = occupied ? 'occupied' : 'opening';
  test(`POS ${screen}: logout POSTs with CSRF, clears named keys and retains the till return path`, async () => {
    const env = environment();
    const render = openingScreen(env, occupied);
    await render().props.onClick();
    assert.equal(env.calls[0][0], '/api/method/logout');
    assert.equal(env.calls[0][1].method, 'POST');
    assert.equal(env.calls[0][1].headers?.['X-Frappe-CSRF-Token'], 'session-token');
    assert.equal(env.globals.window.location.href, '/login?redirect-to=%2Fpos');
    assert.equal(env.globals.localStorage.getItem('posOrderTabsData'), null);
    assert.equal(env.globals.sessionStorage.getItem('posProfile'), null);
    assert.equal(env.globals.localStorage.getItem('deskPreference'), 'keep');
    assert.equal(env.globals.localStorage.getItem('ury_language'), 'fr');
    await render().props.onClick();
    assert.equal(render().props.disabled, true);
    assert.equal(env.calls.length, 1);
  });

  for (const failure of ['HTTP', 'network']) {
    test(`POS ${screen}: ${failure} logout failure stays put, preserves carts and allows retry`, async () => {
      const env = environment();
      env.globals.fetch = async () => {
        if (failure === 'network') throw new Error('Offline');
        return { ok: false, status: 403 };
      };
      const render = openingScreen(env, occupied);
      await render().props.onClick();
      assert.equal(env.globals.window.location.href, '/current');
      assert.equal(env.globals.localStorage.getItem('posOrderTabsData'), 'legacy carts');
      assert.equal(env.errors[0], 'errors.failed_logout');
      assert.equal(Boolean(render().props.disabled), false);
    });
  }

  test(`POS ${screen}: pending logout disables its button and ignores double submits`, async () => {
    const env = environment();
    let complete: (value: unknown) => void;
    env.globals.fetch = (...args: unknown[]) => {
      env.calls.push(args);
      return new Promise((resolve) => { complete = resolve; });
    };
    const render = openingScreen(env, occupied);
    const click = render().props.onClick;
    const first = click();
    const second = click();
    assert.equal(env.calls.length, 1);
    assert.equal(render().props.disabled, true);
    complete!({ ok: true, status: 200 });
    await Promise.all([first, second]);
  });
}
