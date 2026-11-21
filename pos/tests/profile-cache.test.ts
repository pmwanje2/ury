import { beforeEach, expect, test, vi } from 'vitest';

const session = vi.hoisted(() => ({
  get user(): string | null {
    const cookie = document.cookie.split(';').map((part) => part.trim())
      .find((part) => part.startsWith('user_id='));
    return cookie ? decodeURIComponent(cookie.slice('user_id='.length)) : null;
  },
  set user(user: string | null) {
    document.cookie = user === null
      ? 'user_id=; Max-Age=0; Path=/'
      : `user_id=${encodeURIComponent(user)}; Path=/`;
  },
}));
const client = vi.hoisted(() => ({
  auth: { getLoggedInUser: vi.fn(), logout: vi.fn() },
  call: { get: vi.fn(), post: vi.fn() },
  db: { getDoc: vi.fn(), getDocList: vi.fn() },
}));

// Keep storage, auth, profile assembly, and both stores real; replace only HTTP.
vi.mock('../../packages/core/src/frappe/client', () => client);

import { logout } from '../../packages/core/src/frappe/auth';
import { getUserSessionStorageKey } from '../../packages/core/src/storage';
import { usePOSStore } from '../src/store/pos-store';
import { useRootStore } from '../src/store/root-store';
import { getMergeBillCandidates } from '../src/lib/invoice-api';

const cashier = 'cashier@example.invalid';
const trainee = 'trainee@example.invalid';

function profile(name: string, branch = 'Primary', paidLimit = 10) {
  return {
    name, branch, paid_limit: paidLimit,
    owner: 'Administrator', creation: '', modified: '', modified_by: 'Administrator',
    docstatus: 0, idx: 0, company: 'Primary', customer: null, country: 'Uganda',
    disabled: 0, warehouse: 'Stores', campaign: null, company_address: null,
    restaurant: 'Primary Restaurant', currency: 'UGX', role_allowed_for_billing: [],
    waiter: '', cashier: '', enable_payment_without_printing: 0, custom_invoice_warning_time: 0, order_type_customer_map: {}, print_format: null, qz_print: 0, qz_host: null,
    printer: null, print_type: '', tableAttention: 0, disable_rounded_total: 0,
    enable_discount: 0, multiple_cashier: 0,
  };
}

const profiles = {
  [cashier]: profile('Primary POS', 'Primary', 10),
  [trainee]: profile('Primary Training Till', 'Primary Training', 3),
};

beforeEach(() => {
  vi.resetAllMocks();
  session.user = cashier;
  localStorage.clear();
  sessionStorage.clear();
  localStorage.setItem('currencySymbol', 'USh');
  usePOSStore.setState({
    posProfile: null, profileLoading: false, error: null, paymentModes: [],
    customerGroups: [], territories: [], categories: [], isInitializing: true,
  });
  useRootStore.setState({
    user: { name: cashier, roles: ['URY Cashier'] }, posProfile: null,
    isLoading: false, error: null, orders: [], selectedStatus: 'Recently Paid',
    orderSearchQuery: '',
  });
  client.auth.getLoggedInUser.mockImplementation(async () => session.user);
  client.auth.logout.mockResolvedValue(undefined);
  client.call.get.mockImplementation(async (method: string, params?: any) => {
    const current = profiles[session.user as keyof typeof profiles] ?? profiles[cashier];
    if (method === 'ury.ury_pos.api.getPosProfileFull') return { message: profiles[session.user as keyof typeof profiles] ?? profiles[cashier] };
    if (method === 'ury.ury_pos.api.getPosProfile') {
      return { message: { ...current, pos_profile: current.name } };
    }
    if (method === 'ury.ury_pos.api.getModeOfPayment') {
      return { message: [{
        mode_of_payment: session.user === trainee ? 'Training Cash' : 'Cash',
        opening_amount: 0,
      }] };
    }
    if (method === 'ury.ury_pos.api.getMenuCourses') {
      return { message: [{ name: 'Drinks', label: 'Drinks' }] };
    }
    if (method === 'ury.ury_pos.api.getPosInvoice') {
      return { message: { data: [{ name: `paid-limit-${params.limit}` }], next: false } };
    }
    throw new Error(`Unexpected method: ${method}`);
  });
  client.db.getDoc.mockImplementation(async (doctype: string, name: string) => {
    if (doctype === 'POS Profile') {
      return Object.values(profiles).find((entry) => entry.name === name);
    }
    throw new Error(`Unexpected document: ${doctype} ${name}`);
  });
  client.db.getDocList.mockImplementation(async (doctype: string, options: any) => {
    if (doctype === 'Customer Group' || doctype === 'Territory') {
      return [{ name: `${session.user === trainee ? 'Training' : 'Primary'} ${doctype}` }];
    }
    if (doctype === 'POS Invoice') {
      const branch = options.filters?.find(([field]: string[]) => field === 'branch')?.[2];
      return [{ name: `invoice-${branch}` }];
    }
    throw new Error(`Unexpected list: ${doctype}`);
  });
});

test('a same-tab session change loads the new cashier profile without app logout', async () => {
  await usePOSStore.getState().fetchPosProfile();
  expect(usePOSStore.getState().posProfile?.name).toBe('Primary POS');

  session.user = trainee;
  await usePOSStore.getState().fetchPosProfile();

  expect(usePOSStore.getState().posProfile?.name).toBe('Primary Training Till');
  expect(JSON.parse(sessionStorage.getItem(`posProfile:${trainee}`)!)).toMatchObject({
    name: 'Primary Training Till',
  });
});

test('the live cookie wins over a stale identity response and boot user', async () => {
  session.user = trainee;
  client.auth.getLoggedInUser.mockResolvedValue(cashier);
  sessionStorage.setItem(`posProfile:${cashier}`, JSON.stringify(profiles[cashier]));
  sessionStorage.setItem(`posProfile:${trainee}`, JSON.stringify(profiles[trainee]));

  await usePOSStore.getState().fetchPosProfile();

  expect(usePOSStore.getState().posProfile?.name).toBe('Primary Training Till');
  expect(client.auth.getLoggedInUser).not.toHaveBeenCalled();
});

test('legacy unowned profile cache is discarded rather than used for the training till', async () => {
  session.user = trainee;
  sessionStorage.setItem('posProfile', JSON.stringify(profiles[cashier]));
  sessionStorage.setItem(`posProfile:${cashier}`, JSON.stringify(profiles[cashier]));

  await usePOSStore.getState().fetchPosProfile();

  expect(usePOSStore.getState().posProfile?.name).toBe('Primary Training Till');
  expect(sessionStorage.getItem('posProfile')).toBeNull();
});

test('v16 revalidates the current user cache without an identity request', async () => {
  session.user = trainee;
  sessionStorage.setItem(`posProfile:${trainee}`, JSON.stringify(profiles[trainee]));
  await usePOSStore.getState().fetchPosProfile();
  expect(usePOSStore.getState().posProfile?.name).toBe('Primary Training Till');
  expect(client.call.get).toHaveBeenCalledWith('ury.ury_pos.api.getPosProfileFull', { pos_profile: 'Primary Training Till' });
  expect(client.auth.getLoggedInUser).not.toHaveBeenCalled();
});

test.each([null, 'Guest'])('an unidentified session (%s) does not cache a POS profile', async (user) => {
  session.user = user;
  await usePOSStore.getState().fetchPosProfile();

  expect(sessionStorage.length).toBe(0);
});

test('access checks use the current session profile rather than the previous user cache', async () => {
  session.user = trainee;
  sessionStorage.setItem('posProfile', JSON.stringify(profiles[cashier]));
  sessionStorage.setItem(`posProfile:${cashier}`, JSON.stringify(profiles[cashier]));

  await useRootStore.getState().fetchPosProfile();

  expect(useRootStore.getState().posProfile?.name).toBe('Primary Training Till');
  expect(sessionStorage.getItem('posProfile')).toBeNull();
});

test('access checks reuse the current user cache', async () => {
  session.user = trainee;
  sessionStorage.setItem('posProfile', 'stale legacy entry');
  sessionStorage.setItem(`posProfile:${trainee}`, JSON.stringify(profiles[trainee]));
  client.call.get.mockRejectedValue(new Error('Profile request should not be needed'));

  await useRootStore.getState().fetchPosProfile();

  expect(useRootStore.getState().posProfile?.name).toBe('Primary Training Till');
  expect(useRootStore.getState().error).toBeNull();
  expect(sessionStorage.getItem('posProfile')).toBeNull();
  expect(client.auth.getLoggedInUser).not.toHaveBeenCalled();
});

test('a forced access recheck updates only the current user profile cache', async () => {
  session.user = trainee;
  sessionStorage.setItem(`posProfile:${cashier}`, JSON.stringify(profiles[cashier]));
  sessionStorage.setItem(`posProfile:${trainee}`, JSON.stringify(profile('Old Training Till')));

  await useRootStore.getState().fetchPosProfile(true);

  expect(JSON.parse(sessionStorage.getItem(`posProfile:${trainee}`)!)).toMatchObject({
    name: 'Primary Training Till',
  });
  expect(JSON.parse(sessionStorage.getItem(`posProfile:${cashier}`)!)).toMatchObject({
    name: 'Primary POS',
  });
});

test('profile-specific payment modes switch with the session', async () => {
  await usePOSStore.getState().fetchPaymentModes();
  expect(usePOSStore.getState().paymentModes).toEqual(['Cash']);
  session.user = trainee;

  await usePOSStore.getState().fetchPaymentModes();

  expect(usePOSStore.getState().paymentModes).toEqual(['Training Cash']);
  expect(sessionStorage.getItem('payment_modes')).toBeNull();
  expect(JSON.parse(sessionStorage.getItem(`payment_modes:${trainee}`)!)).toEqual(['Training Cash']);
});

test('legacy payment modes are ignored', async () => {
  session.user = trainee;
  sessionStorage.setItem('payment_modes', '["Cash"]');
  await usePOSStore.getState().fetchPaymentModes();

  expect(usePOSStore.getState().paymentModes).toEqual(['Training Cash']);
  expect(sessionStorage.getItem('payment_modes')).toBeNull();
});

test('matching user payment modes are reused', async () => {
  sessionStorage.setItem('payment_modes', '["stale legacy entry"]');
  sessionStorage.setItem(`payment_modes:${cashier}`, '["Cash"]');
  client.call.get.mockRejectedValue(new Error('Payment modes request should not be needed'));

  await usePOSStore.getState().fetchPaymentModes();

  expect(usePOSStore.getState().paymentModes).toEqual(['Cash']);
  expect(sessionStorage.getItem('payment_modes')).toBeNull();
  expect(client.auth.getLoggedInUser).not.toHaveBeenCalled();
});

test.each([
  ['fetchCustomerGroups', 'customerGroups', 'Primary Customer Group'],
  ['fetchTerritories', 'territories', 'Primary Territory'],
] as const)('%s reuses its cookie-owned cache without an identity request', async (fetch, field, value) => {
  sessionStorage.setItem(`${field}:${cashier}`, JSON.stringify([value]));
  sessionStorage.setItem(field, '["stale legacy entry"]');
  client.db.getDocList.mockRejectedValue(new Error('Cached list must not need HTTP'));

  await usePOSStore.getState()[fetch]();

  expect(usePOSStore.getState()[field]).toEqual([value]);
  expect(sessionStorage.getItem(field)).toBeNull();
  expect(client.auth.getLoggedInUser).not.toHaveBeenCalled();
});

test.each([cashier, 'Guest', null, ''])(
  'building a user cache key for %s never mutates storage', (user) => {
    sessionStorage.setItem('posProfile', 'legacy');
    sessionStorage.setItem(`posProfile:${cashier}`, 'owned');
    sessionStorage.setItem('other-app', 'keep');

    expect(getUserSessionStorageKey('posProfile', user)).toBe(
      user && user !== 'Guest' ? `posProfile:${user}` : null,
    );
    expect(Object.fromEntries(Object.entries(sessionStorage))).toEqual({
      posProfile: 'legacy', [`posProfile:${cashier}`]: 'owned', 'other-app': 'keep',
    });
  },
);

test.each([
  ['fetchCustomerGroups', 'customerGroups', 'Customer Group'],
  ['fetchTerritories', 'territories', 'Territory'],
] as const)('%s does not reuse another user permission-filtered list', async (fetch, field, doctype) => {
  await usePOSStore.getState()[fetch]();
  session.user = trainee;
  await usePOSStore.getState()[fetch]();

  expect(usePOSStore.getState()[field]).toEqual([`Training ${doctype}`]);
  expect(sessionStorage.getItem(field)).toBeNull();
  expect(JSON.parse(sessionStorage.getItem(`${field}:${trainee}`)!)).toEqual([`Training ${doctype}`]);
});

test('site-wide menu categories remain shared across users', async () => {
  await usePOSStore.getState().fetchCategories();
  session.user = trainee;
  client.call.get.mockRejectedValue(new Error('Shared categories should be cached'));
  await usePOSStore.getState().fetchCategories();

  expect(usePOSStore.getState().categories).toEqual([{ name: 'Drinks', label: 'Drinks' }]);
  expect(sessionStorage.getItem('menuCategories')).not.toBeNull();
});

test('paid order listing takes the limit from the current session cache', async () => {
  session.user = trainee;
  sessionStorage.setItem('posProfile', JSON.stringify(profiles[cashier]));
  sessionStorage.setItem(`posProfile:${trainee}`, JSON.stringify(profiles[trainee]));

  await useRootStore.getState().fetchOrders();

  expect(useRootStore.getState().orders[0]?.name).toBe('paid-limit-3');
  expect(sessionStorage.getItem('posProfile')).toBeNull();
  expect(client.auth.getLoggedInUser).not.toHaveBeenCalled();
});

test('bill merge candidates take their branch from the current session cache', async () => {
  session.user = trainee;
  sessionStorage.setItem('posProfile', JSON.stringify(profiles[cashier]));
  sessionStorage.setItem(`posProfile:${trainee}`, JSON.stringify(profiles[trainee]));

  const result = await getMergeBillCandidates({ primaryInvoice: 'INV-1', linkedSecondaries: [] });

  expect(result.data[0]?.name).toBe('invoice-Primary Training');
  expect(sessionStorage.getItem('posProfile')).toBeNull();
  expect(client.auth.getLoggedInUser).not.toHaveBeenCalled();
});

test('logout removes all user-scoped application caches but preserves unrelated storage', async () => {
  for (const user of [cashier, trainee]) {
    for (const key of ['posProfile', 'payment_modes', 'customerGroups', 'territories']) {
      sessionStorage.setItem(`${key}:${user}`, 'cached');
    }
  }
  sessionStorage.setItem('posProfile', 'legacy');
  sessionStorage.setItem('menuCategories', 'shared');
  sessionStorage.setItem('other-app', 'keep');
  localStorage.setItem('userAuth', 'cached');
  localStorage.setItem('other-app', 'keep');

  await logout();

  expect(Object.keys(sessionStorage)).toEqual(['other-app']);
  expect(Object.keys(localStorage)).toEqual(['other-app']);
});

test('failed logout leaves storage intact', async () => {
  sessionStorage.setItem(`posProfile:${cashier}`, 'cached');
  client.auth.logout.mockRejectedValue(new Error('Offline'));

  await logout().catch(() => undefined);

  expect(sessionStorage.getItem(`posProfile:${cashier}`)).toBe('cached');
});

test('profile-only readers cannot obtain an unowned legacy key when the cookie is missing or Guest', async () => {
  const { sessionStorageKey } = await import('../../packages/core/src/storage');
  for (const identity of [null, 'Guest']) {
    session.user = identity;
    sessionStorage.setItem('posProfile', JSON.stringify({ branch: 'Prior branch' }));
    expect(sessionStorageKey('posProfile')).toBeNull();
  }
});

test('discards in-flight Outstanding rows across a cookie handoff', async () => {
  let finish!: (v: unknown) => void;
  client.call.get.mockImplementation(async (method, args) => {
    if (method.endsWith('.getBranch')) return { message: 'Branch A' };
    if (args?.doctype === 'POS Invoice') return new Promise(resolve => { finish = resolve; });
    if (args?.doctype === 'POS Invoice Item') return { message: [] };
    return { message: '2026-10-01 12:00:00' };
  });
  useRootStore.setState({ selectedStatus: 'Outstanding' });
  const pending = useRootStore.getState().fetchOrders();
  await vi.waitFor(() => expect(finish).toBeTypeOf('function'));
  session.user = trainee;
  finish({ message: [{ name: 'OLD', creation: '2026-10-01 11:00:00' }] });
  await pending;
  expect(useRootStore.getState().orders).toEqual([]);
  expect(useRootStore.getState().orderLoading).toBe(false);
});

test('selection reports refused item reads and never retains previously loaded lines', async () => {
  useRootStore.setState({ selectedOrderItems: [{ name: 'previous' } as never] });
  client.call.get.mockRejectedValue({ httpStatus: 403 });
  expect(await useRootStore.getState().selectOrder({ name: 'DENIED' } as never)).toBe(false);
  expect(useRootStore.getState().selectedOrderError).toBeTruthy();
  expect(useRootStore.getState().selectedOrderItems).toEqual([]);
});

test('a failed branch-B primary list clears branch-A Outstanding rows for the same owner', async () => {
  let branch = 'A';
  client.call.get.mockImplementation(async (method, args) => {
    if (method.endsWith('.getBranch')) return { message: branch };
    if (branch === 'B') throw new Error('Denied');
    if (args?.doctype === 'POS Invoice') return { message: [{ name: 'A-1', creation: '2026-10-01 11:00:00' }] };
    if (args?.doctype === 'POS Invoice Item') return { message: [] };
    return { message: '2026-10-01 12:00:00' };
  });
  useRootStore.setState({ selectedStatus: 'Outstanding' });
  await useRootStore.getState().fetchOrders();
  expect(useRootStore.getState().orders[0]?.name).toBe('A-1');
  branch = 'B'; await useRootStore.getState().fetchOrders();
  expect(useRootStore.getState().orders).toEqual([]); expect(useRootStore.getState().error).toBeTruthy();
});
