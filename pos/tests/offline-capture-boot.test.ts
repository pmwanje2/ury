import { afterEach, beforeEach, expect, test, vi } from 'vitest';

const registered: string[] = [];

vi.mock('../../packages/core/src/frappe/client', () => ({
  auth: { getLoggedInUser: vi.fn(async () => 'cashier@ury.com'), logout: vi.fn() },
  call: { get: vi.fn(), post: vi.fn() },
  db: { getDoc: vi.fn(), getDocList: vi.fn() },
}));

import { call, db } from '../../packages/core/src/frappe/client';
import { usePOSStore } from '../src/store/pos-store';
import { useRootStore } from '../src/store/root-store';

const cashier = 'cashier@ury.com';

function limitedProfile() {
  return {
    pos_profile: 'Demo Profile',
    branch: 'Main',
    company: 'URY',
    waiter: cashier,
    warehouse: 'Stores',
    cashier,
    print_format: null,
    qz_print: 0,
    qz_host: null,
    printer: null,
    print_type: 'socket',
    tableAttention: 0,
    paid_limit: 0,
    disable_rounded_total: 0,
    enable_discount: 0,
    multiple_cashier: 0,
    owner: 'Administrator',
  };
}

function fullProfile(flag?: unknown) {
  const doc: Record<string, unknown> = {
    name: 'Demo Profile',
    owner: 'Administrator',
    creation: '',
    modified: '',
    modified_by: 'Administrator',
    docstatus: 0,
    idx: 0,
    company: 'URY',
    customer: null,
    country: 'India',
    disabled: 0,
    warehouse: 'Stores',
    campaign: null,
    company_address: null,
    restaurant: 'URY',
    branch: 'Main',
    currency: 'INR',
    role_allowed_for_billing: [],
  };
  if (flag !== undefined) doc.ury_offline_capture = flag;
  return doc;
}

beforeEach(() => {
  registered.length = 0;
  document.cookie = `user_id=${encodeURIComponent(cashier)}; Path=/`;
  localStorage.clear();
  sessionStorage.clear();
  localStorage.setItem('currencySymbol', '₹');
  usePOSStore.setState({
    posProfile: null,
    profileLoading: false,
    error: null,
    isInitializing: true,
    menuItems: [],
    categories: [],
    paymentModes: ['Cash'],
  });
  useRootStore.setState({
    user: { name: cashier, roles: ['URY Cashier'] },
    posProfile: null,
    isLoading: false,
    error: null,
  });
  vi.stubGlobal('navigator', {
    serviceWorker: {
      controller: {},
      register: async (url: string) => {
        registered.push(url);
        return {};
      },
      ready: Promise.resolve({}),
      addEventListener: () => undefined,
      getRegistrations: async () => [],
    },
  });
  vi.stubGlobal('caches', {
    keys: async () => [],
    delete: async () => true,
  });
  vi.mocked(call.get).mockImplementation(async (method: string) => {
    if (method === 'ury.ury_pos.api.getPosProfile') return { message: limitedProfile() };
    if (method === 'ury.ury_pos.api.getMenuCourses') return { message: [] };
    if (method === 'ury.ury_pos.api.getModeOfPayment') return { message: [{ mode_of_payment: 'Cash', opening_amount: 0 }] };
    if (method === 'ury.ury_pos.api.getRestaurantMenu') return { message: { items: [] } };
    throw new Error(`Unexpected method: ${method}`);
  });
  vi.mocked(db.getDoc).mockImplementation(async (doctype: string) => {
    if (doctype === 'POS Profile') return fullProfile(1);
    throw new Error(`Unexpected document: ${doctype}`);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test('boot registers the service worker from the loaded till flag, not the empty root profile', async () => {
  await usePOSStore.getState().initializeApp();

  expect(useRootStore.getState().posProfile).toBeNull();
  expect(usePOSStore.getState().posProfile).toMatchObject({
    name: 'Demo Profile',
    ury_offline_capture: 1,
  });
  expect(registered).toEqual(['/sw.js']);
});

test('boot leaves offline capture off when the loaded till flag is missing', async () => {
  vi.mocked(db.getDoc).mockImplementation(async (doctype: string) => {
    if (doctype === 'POS Profile') return fullProfile();
    throw new Error(`Unexpected document: ${doctype}`);
  });

  await usePOSStore.getState().initializeApp();

  expect(usePOSStore.getState().posProfile).toMatchObject({ name: 'Demo Profile' });
  expect(usePOSStore.getState().posProfile).not.toHaveProperty('ury_offline_capture');
  expect(registered).toEqual([]);
});
