import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, getActivePinia, setActivePinia } from 'pinia';
import { createApp, nextTick } from 'vue';
import { setImmediate as flushPromises } from 'node:timers/promises';
import { useTableStore } from './Table.js';
import FloorPlan from '../components/Table.vue';
import TakeAwayTable from '../components/takeAwayTable.vue';
import router from '../router';

const transferAuth = vi.hoisted(() => ({ hasAccess: false }));
vi.mock('../router', () => ({ default: { push: vi.fn() } }));
vi.mock('./Menu.js', () => ({ useMenuStore: () => ({ fetchItems: vi.fn() }) }));
vi.mock('./invoiceData.js', () => ({
  useInvoiceDataStore: () => ({ tableAttention: 15 }),
}));
vi.mock('./Auth.js', () => ({ useAuthStore: () => transferAuth }));
vi.mock('./Customer.js', () => ({ useCustomerStore: () => ({}) }));
vi.mock('./Notification.js', () => ({ useNotifications: () => ({}) }));
vi.mock('./Alert.js', () => ({ useAlert: () => ({}) }));
vi.mock('./recentOrder.js', () => ({ usetoggleRecentOrder: () => ({}) }));
vi.mock('./frappeSdk.js', () => ({
  default: {
    call: () => ({ get: vi.fn() }),
    db: () => ({ getDocList: vi.fn() }),
  },
}));

let floorApp;
let floorElement;

beforeEach(() => {
  setActivePinia(createPinia());
  vi.useFakeTimers();
  transferAuth.hasAccess = false;
});

afterEach(() => {
  floorApp?.unmount();
  floorElement?.remove();
  floorApp = undefined;
  floorElement = undefined;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const activeBranch = 'Salama Kampala';

describe.each([
  ['dine-in', FloorPlan, 0],
  ['takeaway', TakeAwayTable, 1],
])('%s captain transfer menu', (_name, component, isTakeAway) => {
  async function mountMenu() {
    const store = useTableStore();
    transferAuth.hasAccess = true;
    store.tables = [{ name: 'Table 1', occupied: 1, is_take_away: isTakeAway }];
    store.call.get.mockImplementation(async (method, args) => {
      if (method.endsWith('.get_order_invoice')) {
        expect(args).toEqual({ table: 'Table 1' });
        return { message: { name: 'POS-INV-1', waiter: 'current@fixture.invalid' } };
      }
      if (method.endsWith('.get_captain_transfer_candidates')) {
        expect(args).toEqual({ invoice: 'POS-INV-1' });
        return { message: [] };
      }
      throw new Error(`Unexpected endpoint: ${method}`);
    });
    floorElement = document.createElement('div');
    document.body.appendChild(floorElement);
    floorApp = createApp(component);
    floorApp.use(getActivePinia());
    floorApp.mount(floorElement);
    await nextTick();
    return store;
  }

  function transferLink() {
    return [...floorElement.querySelectorAll('a')].find(el => el.textContent.trim() === 'Captain Transfer');
  }

  it('hides transfer with no candidates and requests the active invoice candidates', async () => {
    const store = await mountMenu();
    await store.toggleDropdown('Table 1');
    await nextTick();
    expect(transferLink()).toBeUndefined();
    expect(store.call.get).toHaveBeenCalledWith(
      'ury.ury.doctype.ury_order.ury_order.get_captain_transfer_candidates', { invoice: 'POS-INV-1' },
    );
  });

  it('shows transfer for eligible candidates and opens the existing picker', async () => {
    const store = await mountMenu();
    store.call.get.mockImplementation(async method => ({ message: method.endsWith('.get_order_invoice')
      ? { name: 'POS-INV-1', waiter: 'current@fixture.invalid' }
      : [{ name: 'waiter@fixture.invalid', full_name: 'Eligible Waiter' }] }));
    await store.toggleDropdown('Table 1');
    await nextTick();
    expect(transferLink()).toBeDefined();
    transferLink().click();
    await nextTick();
    expect(store.showModalCaptainTransfer).toBe(true);
    await store.toggleDropdown('Table 1');
    store.call.get.mockImplementation(async method => ({ message: method.endsWith('.get_order_invoice')
      ? { name: 'POS-INV-1', waiter: 'current@fixture.invalid' } : [] }));
    await store.toggleDropdown('Table 1');
    await nextTick();
    expect(transferLink()).toBeUndefined();
  });

  it('fails closed when the candidate read is refused', async () => {
    const store = await mountMenu();
    store.captain = [{ name: 'stale@fixture.invalid', full_name: 'Stale Captain' }];
    vi.spyOn(console, 'error').mockImplementation(() => {});
    store.call.get.mockImplementation(async method => {
      if (method.endsWith('.get_order_invoice')) return { message: { name: 'POS-INV-1' } };
      throw new Error('Candidate access denied');
    });
    await store.toggleDropdown('Table 1');
    await nextTick();
    expect(transferLink()).toBeUndefined();
    expect(store.captain).toEqual([]);
  });

  it('retains the role gate when eligible candidates exist', async () => {
    const store = await mountMenu();
    // The component and store share the authenticated user's role context.
    floorApp.unmount();
    transferAuth.hasAccess = false;
    store.captain = [{ name: 'waiter@fixture.invalid', full_name: 'Eligible Waiter' }];
    floorApp = createApp(component);
    floorApp.use(getActivePinia());
    floorApp.mount(floorElement);
    await store.toggleDropdown('Table 1');
    await nextTick();
    expect(transferLink()).toBeUndefined();
    expect(store.call.get.mock.calls.some(([method]) => method.endsWith('.get_captain_transfer_candidates'))).toBe(false);
  });

  it('does not restore candidates from a closed menu after reopening', async () => {
    const store = await mountMenu();
    let resolveOld;
    const oldResponse = new Promise(resolve => { resolveOld = resolve; });
    let reads = 0;
    store.call.get.mockImplementation(async method => {
      if (method.endsWith('.get_order_invoice')) return { message: { name: 'POS-INV-1' } };
      return ++reads === 1 ? oldResponse : { message: [] };
    });
    const firstOpen = store.toggleDropdown('Table 1');
    await flushPromises();
    await store.toggleDropdown('Table 1');
    await store.toggleDropdown('Table 1');
    resolveOld({ message: [{ name: 'stale@fixture.invalid', full_name: 'Stale Captain' }] });
    await firstOpen;
    await nextTick();
    expect(transferLink()).toBeUndefined();
    expect(store.captain).toEqual([]);
    expect(reads).toBe(2);
  });
});

it('fetchCaptain loads eligible captains for the active invoice', async () => {
  const store = useTableStore();
  store.tableName = 'Table 1';
  store.invoiceNumber = 'STALE-INVOICE';
  const eligible = [{ name: 'waiter3@fixture.invalid', full_name: 'Test Waiter Three' }];
  let resolveInvoice;
  store.call.get.mockImplementation((method, args) => {
    if (method === 'ury.ury.doctype.ury_order.ury_order.get_order_invoice') {
      expect(args).toEqual({ table: 'Table 1' });
      return new Promise(resolve => { resolveInvoice = resolve; });
    }
    if (method === 'ury.ury.doctype.ury_order.ury_order.get_captain_transfer_candidates') {
      expect(args).toEqual({ invoice: 'POS-INV-1' });
      return Promise.resolve({ message: eligible });
    }
    throw new Error(`Unexpected endpoint: ${method}`);
  });
  store.db.getDocList.mockResolvedValue([{ name: 'unrelated@fixture.invalid' }]);
  const loading = store.fetchCaptain();
  await flushPromises();
  expect(store.call.get).toHaveBeenCalledExactlyOnceWith(
    'ury.ury.doctype.ury_order.ury_order.get_order_invoice', { table: 'Table 1' },
  );
  resolveInvoice({ message: { name: 'POS-INV-1', waiter: 'current@fixture.invalid' } });
  await loading;
  expect(store.captain).toEqual(eligible);
  expect(store.db.getDocList).not.toHaveBeenCalled();
});

it('searchCaptian matches full name as well as user id', () => {
  const store = useTableStore();
  store.captain = [
    { name: 'waiter3@fixture.invalid', full_name: 'Test Waiter Three' },
    { name: 'other@fixture.invalid', full_name: 'Amina Nakato' },
  ];
  for (const search of ['THREE', 'waiter3@']) {
    store.newCaptain = search;
    expect(store.searchCaptian).toEqual([store.captain[0]]);
  }
});

it('shows captain full names and falls back to user ids in the transfer picker', async () => {
  const store = useTableStore();
  store.showModalCaptainTransfer = true;
  store.showCaptain = true;
  store.captain = [
    { name: 'waiter3@fixture.invalid', full_name: 'Test Waiter Three' },
    { name: 'unnamed@fixture.invalid', full_name: '' },
  ];
  floorElement = document.createElement('div');
  document.body.appendChild(floorElement);
  floorApp = createApp(FloorPlan);
  floorApp.use(getActivePinia());
  floorApp.mount(floorElement);
  await nextTick();
  const labels = [...floorElement.querySelectorAll('h1')].map(el => el.textContent.trim());
  expect(labels).toContain('Test Waiter Three');
  expect(labels).toContain('unnamed@fixture.invalid');
});

it('clears old captains when the candidate read is refused', async () => {
  const store = useTableStore();
  store.captain = [{ name: 'stale@fixture.invalid', full_name: 'Stale Captain' }];
  vi.spyOn(console, 'error').mockImplementation(() => {});
  store.call.get.mockImplementation(async method => {
    if (method.endsWith('.get_order_invoice')) return { message: { name: 'POS-INV-1' } };
    throw new Error('You are not permitted to transfer captains.');
  });
  store.db.getDocList.mockRejectedValue(new Error('Global User read must not be used'));
  await store.fetchCaptain();
  await flushPromises();
  expect(store.captain).toEqual([]);
});

it('does not load captains for a stale invoice when the active invoice read is refused', async () => {
  const store = useTableStore();
  store.tableName = 'Table 2';
  store.invoiceNumber = 'STALE-INVOICE';
  store.captain = [{ name: 'stale@fixture.invalid', full_name: 'Stale Captain' }];
  store.db.getDocList.mockResolvedValue([]);
  vi.spyOn(console, 'error').mockImplementation(() => {});
  store.call.get.mockRejectedValue({ _server_messages: 'Invoice access denied' });
  await store.fetchCaptain();
  expect(store.call.get).toHaveBeenCalledExactlyOnceWith(
    'ury.ury.doctype.ury_order.ury_order.get_order_invoice', { table: 'Table 2' },
  );
  expect(store.captain).toEqual([]);
});

function activeInvoiceQuery(tableNames) {
  return {
    fields: ['name', 'restaurant_table', 'creation', 'custom_merged_tables', 'waiter'],
    filters: [
      ['branch', '=', activeBranch],
      ['docstatus', '=', 0],
    ],
    orFilters: [
      ['restaurant_table', 'in', tableNames],
      ...tableNames.map((name) => ['custom_merged_tables', 'like', `%${name}%`]),
    ],
    orderBy: { field: 'creation', order: 'desc' },
    limit: 0,
  };
}

function invoiceMatches(row, options = {}) {
  const filters = options.filters || [];
  const orFilters = options.orFilters || [];
  const andOk = filters.every(([field, operator, value]) => operator === '=' && row[field] === value);
  if (!andOk) return false;
  if (!orFilters.length) return true;
  return orFilters.some(([field, operator, value]) => {
    if (operator === 'in') return (value || []).includes(row[field]);
    if (operator === 'like') {
      const needle = String(value).split('%').join('');
      return String(row[field] || '').includes(needle);
    }
    return false;
  });
}

async function fetchTables(tables, serverNow, invoices = []) {
  const store = useTableStore();
  store.selectedRoom = 'Dining';
  store.invoiceData.branch = activeBranch;
  store.db.getDocList.mockImplementation((doctype) => {
    if (doctype === 'URY Table') return Promise.resolve(tables);
    if (doctype === 'POS Invoice') return Promise.resolve(invoices);
    if (doctype === 'URY Service Request') return Promise.resolve([]);
    throw new Error(`Unexpected doctype: ${doctype}`);
  });
  store.call.get.mockImplementation((method) => {
    if (method !== 'ury.ury.api.ury_server_time.get_server_time') {
      throw new Error(`Unexpected endpoint: ${method}`);
    }
    return Promise.resolve({ message: serverNow });
  });
  store.fetchTable();
  await flushPromises();
  return store;
}

async function fetchTable(table, serverNow, invoices = []) {
  return fetchTables([table], serverNow, invoices);
}

function roomStore(tables, serverTime) {
  const store = useTableStore();
  store.selectedRoom = 'Dining';
  store.invoiceData.multipleCashier = true;
  store.invoiceData.posProfile = 'Dining POS';
  store.db.getDocList.mockReturnValue(tables);
  store.call.get.mockImplementation((method) => {
    switch (method) {
      case 'ury.ury.api.ury_server_time.get_server_time':
        return serverTime;
      case 'ury.ury_pos.api.getRestaurantMenu':
        return Promise.resolve({
          message: { items: [{ item: 'Coffee' }], name: 'Dining Menu', modified: '2026-10-02 12:00:00' },
        });
      case 'ury.ury_pos.api.getCashier':
        return Promise.resolve({ message: 'Dining Cashier' });
      default:
        throw new Error(`Unexpected endpoint: ${method}`);
    }
  });
  return store;
}

describe('room changes load menu and cashier independently of floor-plan requests', () => {
  it.each(['table list', 'server clock'])('loads the menu and cashier while the %s is pending', async (pending) => {
    const unresolved = new Promise(() => {});
    const store = roomStore(
      pending === 'table list' ? unresolved : Promise.resolve([]),
      pending === 'server clock' ? unresolved : Promise.resolve({ message: '2026-10-02T12:00:00' }),
    );

    store.handleRoomChange();
    await flushPromises();

    expect(store.call.get).toHaveBeenCalledWith('ury.ury_pos.api.getRestaurantMenu', {
      room: 'Dining', pos_profile: 'Dining POS',
    });
    expect(store.tableMenu).toEqual([{ item: 'Coffee' }]);
    expect(store.cashier).toBe('Dining Cashier');
  });

  it('loads the menu and cashier without propagating a rejected table list', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const store = roomStore(
      Promise.reject(new Error('Table list unavailable')),
      Promise.resolve({ message: '2026-10-02T12:00:00' }),
    );
    let roomError;
    const change = store.handleRoomChange().catch((error) => { roomError = error; });
    await flushPromises();
    await change;

    expect(roomError).toBeUndefined();
    expect(store.call.get).toHaveBeenCalledWith('ury.ury_pos.api.getRestaurantMenu', {
      room: 'Dining', pos_profile: 'Dining POS',
    });
    expect(store.tableMenu).toEqual([{ item: 'Coffee' }]);
    expect(store.cashier).toBe('Dining Cashier');
  });
});

describe('floor-plan table age uses the site clock', () => {
  it('shows 0:00 when the invoice time is two seconds ahead of site now', async () => {
    vi.setSystemTime(new Date(2026, 9, 2, 11, 0, 0));
    const store = await fetchTable(
      { name: 'Table 1', occupied: 1, latest_invoice_time: '12:00:02' },
      '2026-10-02T12:00:00',
    );

    expect(store.getTimeDifference(store.tables[0])).toBe('0:00');
    expect(store.getBadgeText(store.tables[0])).toBe('Occupied');
    expect(store.getBadgeType(store.tables[0])).toBe('yellow');
  });

  it('shows 0:00 while the site offset is not yet loaded', () => {
    vi.setSystemTime(new Date(2026, 9, 2, 11, 0, 0));
    const store = useTableStore();

    expect(store.getTimeDifference({
      name: 'Table 1', occupied: 1, latest_invoice_time: '11:00:02',
    })).toBe('0:00');
  });

  it('does not roll back a day when the invoice is exactly twelve hours ahead', async () => {
    vi.setSystemTime(new Date(2026, 9, 3, 0, 10, 0));
    const store = await fetchTable(
      { name: 'Table 1', occupied: 1, latest_invoice_time: '12:10:00' },
      '2026-10-03T00:10:00',
    );

    expect(store.getTimeDifference(store.tables[0])).toBe('0:00');
  });

  it('shows 0:05 when the browser is one hour behind the server', async () => {
    vi.setSystemTime(new Date(2026, 9, 2, 11, 0, 0));
    const store = await fetchTable(
      { name: 'Table 1', occupied: 1, latest_invoice_time: '11:55:00' },
      '2026-10-02T12:00:00.000000',
    );

    expect(store.getTimeDifference(store.tables[0])).toBe('0:05');
    expect(store.getBadgeText(store.tables[0])).toBe('Occupied');
    expect(store.getBadgeType(store.tables[0])).toBe('yellow');
  });

  it.each([
    { browserNow: [2026, 9, 2, 23, 10, 0], clock: 'one hour behind' },
    { browserNow: [2026, 9, 3, 0, 10, 0], clock: 'matching the site' },
  ])('shows 0:20 after site midnight with the browser $clock', async ({ browserNow }) => {
    vi.setSystemTime(new Date(...browserNow));
    const store = await fetchTable(
      { name: 'Table 1', occupied: 1, latest_invoice_time: '23:50:00' },
      '2026-10-03T00:10:00',
    );

    expect(store.getTimeDifference(store.tables[0])).toBe('0:20');
    expect(store.getBadgeText(store.tables[0])).toBe('Attention');
    expect(store.getBadgeType(store.tables[0])).toBe('red');
  });

  it('keeps advancing across site midnight without refetching', async () => {
    vi.setSystemTime(new Date(2026, 9, 2, 22, 55, 0));
    const store = await fetchTable(
      { name: 'Table 1', occupied: 1, latest_invoice_time: '23:50:00' },
      '2026-10-02T23:55:00',
    );

    expect(store.getTimeDifference(store.tables[0])).toBe('0:05');
    vi.advanceTimersByTime(20 * 60 * 1000);
    expect(store.getTimeDifference(store.tables[0])).toBe('0:25');
  });

  it('uses padded site time for unoccupied tables and keeps free badges unchanged', async () => {
    vi.setSystemTime(new Date(2026, 9, 2, 11, 0, 0));
    const store = await fetchTable(
      { name: 'Table 1', occupied: 0, latest_invoice_time: '11:55:00' },
      '2026-10-02T12:00:00',
    );

    expect(store.getTimeDifference(store.tables[0])).toBe('12:00');
    expect(store.getBadgeText(store.tables[0])).toBe('Free');
    expect(store.getBadgeType(store.tables[0])).toBe('green');
  });

  it('still loads the floor plan if refreshing server time fails', async () => {
    vi.setSystemTime(new Date(2026, 9, 2, 11, 0, 0));
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const store = await fetchTable(
      { name: 'Table 1', occupied: 1, latest_invoice_time: '11:55:00' },
      '2026-10-02T12:00:00',
    );
    store.call.get.mockRejectedValue(new Error('Server clock unavailable'));
    store.db.getDocList.mockResolvedValue([
      { name: 'Table 10', occupied: 0 },
      { name: 'Table 2', occupied: 1, latest_invoice_time: '11:55:00' },
    ]);

    store.fetchTable();
    await flushPromises();

    expect(store.tables.map((table) => table.name)).toEqual(['Table 2', 'Table 10']);
    expect(store.getTimeDifference(store.tables[0])).toBe('0:05');
  });
});

describe('floor-plan age uses the oldest open bill creation', () => {
  it.each([
    { creation: '2026-10-02 11:05:00.000000', age: '13:05' },
    { creation: '2026-10-01 11:05:00', age: '37:05' },
  ])('shows $age across midnight instead of guessing from a time-only field', async ({ creation, age }) => {
    vi.setSystemTime(new Date(2026, 9, 2, 23, 10, 0));
    const store = await fetchTable(
      { name: 'Table 1', occupied: 1, latest_invoice_time: '11:05:00' },
      '2026-10-03 00:10:00',
      [{ restaurant_table: 'Table 1', creation, custom_merged_tables: '' }],
    );

    expect(store.getTimeDifference(store.tables[0])).toBe(age);
    expect(store.getBadgeText(store.tables[0])).toBe('Attention');
    expect(store.getBadgeType(store.tables[0])).toBe('red');
    vi.advanceTimersByTime(20 * 60 * 1000);
    expect(store.getTimeDifference(store.tables[0])).toBe(age === '13:05' ? '13:25' : '37:25');
  });

  it('uses the oldest draft creation after a transfer, not a reset table time or a newer bill', async () => {
    vi.setSystemTime(new Date(2026, 9, 2, 23, 10, 0));
    const store = await fetchTables([
      { name: 'Table 2', occupied: 1, latest_invoice_time: '00:05:00' },
      { name: 'Table 10', occupied: 0, latest_invoice_time: '11:05:00' },
      { name: 'Table 3', occupied: 1, latest_invoice_time: '00:08:00' },
    ], '2026-10-03 00:10:00', [
      { restaurant_table: 'Table 2', creation: '2026-10-02 11:05:00', custom_merged_tables: '' },
      { restaurant_table: 'Table 2', creation: '2026-10-01 11:05:00', custom_merged_tables: '' },
    ]);

    expect(store.getTimeDifference(store.tables[0])).toBe('37:05');
    expect(store.getTimeDifference(store.tables[1])).toBe('0:02');
    expect(store.getBadgeText(store.tables[2])).toBe('Free');
    // Verify the external read contract: active branch, this room's tables,
    // drafts only, merged partners, and no default 20-row pagination truncation.
    expect(store.db.getDocList).toHaveBeenCalledWith('POS Invoice', activeInvoiceQuery(['Table 2', 'Table 3']));
  });

  it.each(['newest first', 'reversed'])('selects the oldest open check with the list returned %s', async (order) => {
    vi.setSystemTime(new Date(2026, 9, 3, 0, 10, 0));
    const drafts = [
      { restaurant_table: 'Table 1', creation: '2026-10-03 00:05:00', custom_merged_tables: '' },
      { restaurant_table: 'Table 1', creation: '2026-10-01 11:05:00', custom_merged_tables: '' },
      { restaurant_table: 'Table 1', creation: '2026-10-02 11:05:00', custom_merged_tables: '' },
    ];
    if (order === 'reversed') drafts.reverse();
    const store = await fetchTable(
      { name: 'Table 1', occupied: 1, latest_invoice_time: '00:05:00' },
      '2026-10-03 00:10:00', drafts,
    );

    expect(store.getTimeDifference(store.tables[0])).toBe('37:05');
    expect(store.getBadgeText(store.tables[0])).toBe('Attention');
    expect(store.getBadgeType(store.tables[0])).toBe('red');
  });

  it('keeps seating age and Attention after a split inserts a newer sibling draft', async () => {
    vi.setSystemTime(new Date(2026, 9, 3, 0, 10, 0));
    const source = { restaurant_table: 'Table 1', creation: '2026-10-03 00:00:00', custom_merged_tables: '' };
    const store = await fetchTable(
      { name: 'Table 1', occupied: 1, latest_invoice_time: '00:00:00' },
      '2026-10-03 00:10:00', [source],
    );
    expect(store.getTimeDifference(store.tables[0])).toBe('0:10');
    vi.advanceTimersByTime(10 * 60 * 1000);
    store.call.get.mockResolvedValue({ message: '2026-10-03 00:20:00' });
    store.db.getDocList.mockImplementation((doctype) => Promise.resolve(doctype === 'URY Table'
      ? [{ name: 'Table 1', occupied: 1, latest_invoice_time: '00:19:00' }]
      : [{ ...source, creation: '2026-10-03 00:19:00' }, source]));

    store.fetchTable();
    await flushPromises();

    expect(store.getTimeDifference(store.tables[0])).toBe('0:20');
    expect(store.getBadgeText(store.tables[0])).toBe('Attention');
    expect(store.getBadgeType(store.tables[0])).toBe('red');
  });

  it('takes the oldest primary or exact merged-partner draft even when the primary is outside the room', async () => {
    vi.setSystemTime(new Date(2026, 9, 3, 0, 10, 0));
    const store = await fetchTable(
      { name: 'Table 2', occupied: 1, latest_invoice_time: '00:05:00', merged_with: 'Other Room Table' },
      '2026-10-03 00:10:00', [
        { restaurant_table: 'Table 2', creation: '2026-10-03 00:05:00', custom_merged_tables: '' },
        { restaurant_table: 'Other Room Table', creation: '2026-10-03 00:04:00', custom_merged_tables: ' Table 2, Table 3 ' },
        { restaurant_table: 'Other Room Table', creation: '2026-10-01 11:05:00', custom_merged_tables: ' Table 2, Table 3 ' },
        { restaurant_table: 'Other Table', creation: '2026-09-30 11:05:00', custom_merged_tables: 'Table 20, Table 21' },
      ],
    );

    expect(store.getTimeDifference(store.tables[0])).toBe('37:05');
    expect(store.getBadgeText(store.tables[0])).toBe('Attention');
    expect(store.getBadgeType(store.tables[0])).toBe('red');
    expect(store.db.getDocList).toHaveBeenCalledWith('POS Invoice', activeInvoiceQuery(['Table 2']));
  });

  it('uses the primary bill for merged partners with exact, trimmed table names', async () => {
    vi.setSystemTime(new Date(2026, 9, 3, 0, 10, 0));
    const store = await fetchTables([
      { name: 'Table 1', occupied: 1, latest_invoice_time: '00:05:00' },
      { name: 'Table 2', occupied: 1, latest_invoice_time: '00:05:00', merged_with: 'Table 1' },
      { name: 'Table 20', occupied: 1, latest_invoice_time: '00:05:00' },
      { name: 'Table 3', occupied: 0, latest_invoice_time: '00:05:00' },
    ], '2026-10-03 00:10:00', [
      { restaurant_table: 'Table 1', creation: '2026-10-02 11:05:00', custom_merged_tables: ' Table 2, Table 3, Other Room Table ' },
    ]);

    expect(store.tables.map((table) => store.getTimeDifference(table))).toEqual(['13:05', '13:05', '0:10', '0:05']);
    expect(store.getBadgeText(store.tables[1])).toBe('Attention');
    expect(store.getBadgeText(store.tables[2])).toBe('Free');
  });

  it('does not let a stale time-only value age a future bill', async () => {
    vi.setSystemTime(new Date(2026, 9, 2, 11, 0, 0));
    const store = await fetchTable(
      { name: 'Table 1', occupied: 1, latest_invoice_time: '10:00:00' },
      '2026-10-02 12:00:00',
      [{ restaurant_table: 'Table 1', creation: '2026-10-02 12:00:02', custom_merged_tables: '' }],
    );

    expect(store.getTimeDifference(store.tables[0])).toBe('0:00');
    expect(store.getBadgeText(store.tables[0])).toBe('Occupied');
  });

  it('can age an occupied table even when its time-only field is empty', async () => {
    vi.setSystemTime(new Date(2026, 9, 3, 0, 10, 0));
    const store = await fetchTable(
      { name: 'Table 1', occupied: 1, latest_invoice_time: null },
      '2026-10-03 00:10:00',
      [{ restaurant_table: 'Table 1', creation: '2026-10-02 11:05:00', custom_merged_tables: '' }],
    );
    expect(store.getTimeDifference(store.tables[0])).toBe('13:05');
  });

  it('falls back to the time-only field for an invalid bill datetime', async () => {
    vi.setSystemTime(new Date(2026, 9, 3, 0, 10, 0));
    const store = await fetchTable(
      { name: 'Table 1', occupied: 1, latest_invoice_time: '23:50:00' },
      '2026-10-03 00:10:00',
      [{ restaurant_table: 'Table 1', creation: 'invalid', custom_merged_tables: '' }],
    );
    expect(store.getTimeDifference(store.tables[0])).toBe('0:20');
  });

  it('does not query invoices when the room has no occupied tables', async () => {
    vi.setSystemTime(new Date(2026, 9, 3, 0, 10, 0));
    const store = await fetchTable(
      { name: 'Table 1', occupied: 0 }, '2026-10-03 00:10:00',
    );
    expect(store.db.getDocList.mock.calls.map(([doctype]) => doctype)).toEqual(['URY Table', 'URY Service Request']);
    expect(store.getBadgeText(store.tables[0])).toBe('Free');
  });

  it('clears a previous bill timestamp on refresh when an occupied table no longer has an open bill', async () => {
    vi.setSystemTime(new Date(2026, 9, 3, 0, 10, 0));
    const store = await fetchTable(
      { name: 'Table 1', occupied: 1, latest_invoice_time: '00:05:00' },
      '2026-10-03 00:10:00',
      [{ restaurant_table: 'Table 1', creation: '2026-10-02 11:05:00', custom_merged_tables: '' }],
    );
    expect(store.getTimeDifference(store.tables[0])).toBe('13:05');

    store.db.getDocList.mockImplementation((doctype) => Promise.resolve(doctype === 'URY Table'
      ? [{ name: 'Table 1', occupied: 1, latest_invoice_time: '00:05:00' }] : []));
    store.fetchTable();
    await flushPromises();

    expect(store.getTimeDifference(store.tables[0])).toBe('0:05');
  });

  it('keeps floor, menu and cashier usable while the open-bill query is pending or denied', async () => {
    vi.setSystemTime(new Date(2026, 9, 3, 0, 10, 0));
    const errorLog = vi.spyOn(console, 'error').mockImplementation(() => {});
    let deny;
    const billQuery = new Promise((resolve, reject) => { deny = reject; });
    const store = roomStore(Promise.resolve([]), Promise.resolve({ message: '2026-10-03 00:10:00' }));
    store.db.getDocList.mockImplementation((doctype) => doctype === 'URY Table'
      ? Promise.resolve([{ name: 'Table 1', occupied: 1, latest_invoice_time: '23:50:00' }]) : billQuery);

    await store.handleRoomChange();
    await flushPromises();
    expect(store.tables.map((table) => table.name)).toEqual(['Table 1']);
    expect(store.tableMenu).toEqual([{ item: 'Coffee' }]);
    expect(store.cashier).toBe('Dining Cashier');
    expect(store.db.getDocList).toHaveBeenCalledWith('POS Invoice', expect.any(Object));
    expect(store.getTimeDifference(store.tables[0])).toBe('0:20');

    deny(new Error('POS Invoice read denied'));
    await flushPromises();
    expect(errorLog).toHaveBeenCalledWith(expect.objectContaining({ message: 'POS Invoice read denied' }));
    expect(store.getTimeDifference(store.tables[0])).toBe('0:20');
  });

  it('does not apply a pending old-room bill to the newly loaded room', async () => {
    vi.setSystemTime(new Date(2026, 9, 3, 0, 10, 0));
    let finishBills;
    const oldBills = new Promise((resolve) => { finishBills = resolve; });
    const store = useTableStore();
    store.selectedRoom = 'Dining';
    store.call.get.mockResolvedValue({ message: '2026-10-03 00:10:00' });
    store.db.getDocList.mockImplementation((doctype) => doctype === 'URY Table'
      ? Promise.resolve([{ name: 'Table 1', occupied: 1, latest_invoice_time: '00:05:00' }]) : oldBills);
    store.fetchTable();
    await flushPromises();
    expect(store.db.getDocList).toHaveBeenCalledWith('POS Invoice', expect.any(Object));

    store.selectedRoom = 'Terrace';
    store.db.getDocList.mockResolvedValue([{ name: 'Terrace 1', occupied: 0 }]);
    store.fetchTable();
    await flushPromises();
    finishBills([{ restaurant_table: 'Table 1', creation: '2026-10-02 11:05:00', custom_merged_tables: 'Terrace 1' }]);
    await flushPromises();

    expect(store.tables.map((table) => table.name)).toEqual(['Terrace 1']);
    expect(store.getBadgeText(store.tables[0])).toBe('Free');
    expect(store.getTimeDifference(store.tables[0])).toBe('0:10');
  });
});

describe('idle floor-plan rendering', () => {
  it.each([false, true])('advances the rendered age and Attention badge without refetching (takeaway=%s)', async (takeaway) => {
    vi.setSystemTime(new Date(2026, 9, 2, 11, 0, 0));
    const store = await fetchTable(
      { name: 'Table 1', occupied: 1, latest_invoice_time: '11:46:00', is_take_away: takeaway ? 1 : 0 },
      '2026-10-02 12:00:00',
      [{ restaurant_table: 'Table 1', creation: '2026-10-02 11:46:00', custom_merged_tables: '' }],
    );
    store.isTakeaeay = takeaway;
    floorElement = document.createElement('div');
    document.body.appendChild(floorElement);
    floorApp = createApp(FloorPlan);
    floorApp.use(getActivePinia());
    const timerCount = vi.getTimerCount();
    floorApp.mount(floorElement);

    expect(floorElement.textContent).toContain('0:14');
    expect(floorElement.textContent).toContain('Occupied');
    const listReads = store.db.getDocList.mock.calls.length;
    const serverReads = store.call.get.mock.calls.length;

    vi.advanceTimersByTime(60 * 1000);
    await nextTick();
    expect(floorElement.textContent).toContain('0:15');
    expect(floorElement.textContent).toContain('Occupied');
    vi.advanceTimersByTime(60 * 1000);
    await nextTick();
    expect(floorElement.textContent).toContain('0:16');
    expect(floorElement.textContent).toContain('Attention');
    expect(floorElement.querySelector('.bg-red-100')).not.toBeNull();
    expect(store.db.getDocList.mock.calls.slice(listReads).every(([doctype]) => doctype === 'URY Service Request')).toBe(true);
    expect(store.call.get).toHaveBeenCalledTimes(serverReads);

    floorApp.unmount();
    floorApp = undefined;
    expect(vi.getTimerCount()).toBe(timerCount);
  });
});

describe('floor plan shows the table owner', () => {
  const waiter = 'amina.nakato@salama.example';
  const owner = 'Amina Nakato';
  const draft = {
    name: 'POS-INV-2026-00005',
    restaurant_table: 'T-05',
    custom_merged_tables: '',
    waiter,
    creation: '2026-10-14 12:00:00',
  };

  async function loadFloor(tables, invoices, users, applyQuery = false) {
    vi.setSystemTime(new Date(2026, 9, 14, 12, 0, 0));
    const store = useTableStore();
    store.selectedRoom = 'Dining';
    store.invoiceData.branch = activeBranch;
    store.db.getDocList.mockImplementation((doctype, options) => {
      if (doctype === 'URY Table') return Promise.resolve(tables);
      if (doctype === 'POS Invoice') {
        const rows = applyQuery ? invoices.filter((row) => invoiceMatches(row, options)) : invoices;
        return Promise.resolve(rows);
      }
      if (doctype === 'User') {
        if (users instanceof Error) return Promise.reject(users);
        return Promise.resolve(users);
      }
      if (doctype === 'URY Service Request') return Promise.resolve([]);
      throw new Error(`Unexpected doctype: ${doctype}`);
    });
    store.call.get.mockResolvedValue({ message: '2026-10-14 12:00:00' });
    store.fetchTable();
    await flushPromises();
    return store;
  }

  async function mountFloor() {
    floorElement = document.createElement('div');
    document.body.appendChild(floorElement);
    floorApp = createApp(FloorPlan);
    floorApp.use(getActivePinia());
    floorApp.mount(floorElement);
    await nextTick();
  }

  function cardText(name) {
    const heading = [...floorElement.querySelectorAll('h5')].find((node) => node.textContent.includes(name));
    return heading.closest('div.flex-col').textContent;
  }

  it('shows the owner on an occupied card', async () => {
    const store = await loadFloor(
      [{ name: 'T-05', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' }],
      [draft],
      [{ name: waiter, full_name: owner }],
    );

    await mountFloor();

    expect(cardText('T-05')).toContain(owner);
    expect(cardText('T-05')).not.toContain(waiter);
    expect(store.db.getDocList).toHaveBeenCalledWith('POS Invoice', activeInvoiceQuery(['T-05']));
    expect(store.db.getDocList).toHaveBeenCalledWith('User', {
      fields: ['name', 'full_name'],
      filters: [['name', 'in', [waiter]]],
      limit: 1,
    });
  });

  it('shows the same owner on a merged partner table', async () => {
    await loadFloor(
      [
        { name: 'T-05', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' },
        { name: 'T-06', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00', merged_with: 'T-05' },
      ],
      [{ ...draft, custom_merged_tables: ' T-06 ' }],
      [{ name: waiter, full_name: owner }],
    );

    await mountFloor();

    expect(cardText('T-05')).toContain(owner);
    expect(cardText('T-06')).toContain(owner);
    expect(cardText('T-06')).not.toContain(waiter);
  });

  it('names the owner in the assigned-table refusal', async () => {
    const store = await loadFloor(
      [{ name: 'T-05', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' }],
      [draft],
      [{ name: waiter, full_name: owner }],
    );
    store.auth.hasAccess = false;
    store.auth.cashier = false;
    store.auth.sessionUser = 'betty.namuli@salama.example';
    store.alert.createAlert = vi.fn(() => new Promise(() => {}));
    store.tableMenu = [];
    store.menu.cart = [];
    store.call.get.mockImplementation((method) => {
      if (method === 'ury.ury.doctype.ury_order.ury_order.get_order_invoice') {
        return Promise.resolve({
          message: {
            name: 'POS-INV-2026-00005',
            waiter,
            items: [],
            invoice_printed: 0,
            custom_comments: '',
            modified: '',
            grand_total: 0,
            mobile_number: '',
            customer: '',
          },
        });
      }
      return Promise.resolve({ message: '2026-10-14 12:00:00' });
    });

    store.loadSelectedTable({ name: 'T-05', occupied: 1, is_take_away: 0 });
    await flushPromises();

    expect(store.alert.createAlert).toHaveBeenCalledWith(
      'Message',
      `Table is assigned to ${owner}`,
      'OK',
    );
  });

  it('falls back to the id before @ when the User read is refused', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    await loadFloor(
      [{ name: 'T-05', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' }],
      [draft],
      new Error('User read denied'),
    );

    await mountFloor();

    expect(cardText('T-05')).toContain('amina.nakato');
    expect(cardText('T-05')).not.toContain(waiter);
    expect(cardText('T-05')).not.toContain(owner);
  });

  it('reads POS Invoice once while refreshing several occupied tables', async () => {
    const store = await loadFloor(
      [
        { name: 'T-07', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' },
        { name: 'T-05', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' },
        { name: 'T-08', occupied: 0, is_take_away: 0, latest_invoice_time: '12:00:00' },
        { name: 'T-06', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' },
      ],
      [
        { name: 'POS-05', restaurant_table: 'T-05', custom_merged_tables: '', waiter, creation: '2026-10-14 12:00:00' },
        { name: 'POS-06', restaurant_table: 'T-06', custom_merged_tables: '', waiter: 'betty.namuli@salama.example', creation: '2026-10-14 11:30:00' },
        { name: 'POS-07', restaurant_table: 'T-07', custom_merged_tables: '', waiter, creation: '2026-10-14 11:00:00' },
      ],
      [
        { name: waiter, full_name: owner },
        { name: 'betty.namuli@salama.example', full_name: 'Betty Namuli' },
      ],
    );

    const invoiceReads = store.db.getDocList.mock.calls.filter(([doctype]) => doctype === 'POS Invoice');
    expect(invoiceReads).toHaveLength(1);
    expect(invoiceReads[0][1]).toEqual(activeInvoiceQuery(['T-05', 'T-06', 'T-07']));
  });

  it('does not take an owner from another branch or a lookalike table id', async () => {
    const betty = 'betty.namuli@salama.example';
    const foreignWaiter = 'foreign.waiter@other.example';
    const collider = 'collider.waiter@salama.example';
    const store = await loadFloor(
      [
        { name: 'T-05', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' },
        { name: 'T-06', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00', merged_with: 'Patio-9' },
      ],
      [
        {
          name: 'POS-LOCAL', branch: activeBranch, docstatus: 0, restaurant_table: 'T-05',
          custom_merged_tables: '', waiter, creation: '2026-10-14 12:00:00',
        },
        {
          name: 'POS-LOOKALIKE', branch: activeBranch, docstatus: 0, restaurant_table: 'T-050',
          custom_merged_tables: '', waiter: collider, creation: '2026-10-14 08:00:00',
        },
        {
          name: 'POS-MERGED', branch: activeBranch, docstatus: 0, restaurant_table: 'Patio-9',
          custom_merged_tables: ' T-06 ', waiter: betty, creation: '2026-10-14 11:00:00',
        },
        {
          name: 'POS-FOREIGN', branch: 'Other Branch', docstatus: 0, restaurant_table: 'T-05',
          custom_merged_tables: '', waiter: foreignWaiter, creation: '2026-10-14 12:30:00',
        },
      ],
      [
        { name: waiter, full_name: owner },
        { name: betty, full_name: 'Betty Namuli' },
        { name: foreignWaiter, full_name: 'Foreign Waiter' },
        { name: collider, full_name: 'Collider Waiter' },
      ],
      true,
    );

    await mountFloor();

    expect(cardText('T-05')).toContain(owner);
    expect(cardText('T-05')).not.toContain('Foreign Waiter');
    expect(cardText('T-05')).not.toContain('Collider Waiter');
    expect(cardText('T-06')).toContain('Betty Namuli');
    expect(store.db.getDocList).toHaveBeenCalledWith('POS Invoice', activeInvoiceQuery(['T-05', 'T-06']));
    expect(store.db.getDocList).toHaveBeenCalledWith('User', {
      fields: ['name', 'full_name'],
      filters: [['name', 'in', [waiter, betty]]],
      limit: 2,
    });
    expect(store.db.getDocList.mock.calls.filter(([doctype]) => doctype === 'POS Invoice')).toHaveLength(1);
  });
});

describe('refused order invoice keeps another waiter on the floor', () => {
  const ownerName = 'Test Waiter Two';
  const invoiceMethod = 'ury.ury.doctype.ury_order.ury_order.get_order_invoice';
  const permissionError = {
    httpStatus: 403,
    exc_type: 'PermissionError',
    exception: 'frappe.exceptions.PermissionError: Not permitted to view this order',
    message: 'Not permitted to view this order',
  };

  function waiterStore() {
    const store = useTableStore();
    store.auth.hasAccess = false;
    store.auth.cashier = false;
    store.auth.sessionUser = 'waiter.three@salama.example';
    store.tableMenu = [];
    store.menu.cart = [];
    store.alert.createAlert = vi.fn(() => Promise.resolve());
    store.notification.createNotification = vi.fn();
    router.push.mockReset();
    router.push.mockImplementation(() => new Promise(() => {}));
    return store;
  }

  function occupiedTable(extra = {}) {
    return { name: 'T-05', occupied: 1, is_take_away: 0, owner_name: ownerName, ...extra };
  }

  it('shows the floor owner and returns to the floor when the order read is refused', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const store = waiterStore();
    store.call.get.mockRejectedValue(permissionError);

    store.loadSelectedTable(occupiedTable());
    await flushPromises();

    expect(store.alert.createAlert).toHaveBeenCalledWith(
      'Message',
      `Table is assigned to ${ownerName}`,
      'OK',
    );
    expect(router.push).toHaveBeenCalledWith('/Table');
    expect(router.push).not.toHaveBeenCalledWith('/Menu');
    expect(store.menu.cart).toEqual([]);
  });

  it('uses the loaded owner label when the card has no owner name', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const waiter = 'waiter.two@salama.example';
    const store = waiterStore();
    store.ownerNames[waiter] = ownerName;
    store.call.get.mockRejectedValue(permissionError);

    store.loadSelectedTable(occupiedTable({ owner_name: '', waiter }));
    await flushPromises();

    expect(store.alert.createAlert).toHaveBeenCalledWith(
      'Message',
      `Table is assigned to ${ownerName}`,
      'OK',
    );
    expect(router.push).toHaveBeenCalledWith('/Table');
    expect(store.menu.cart).toEqual([]);
  });

  it('says another waiter when no owner name is known', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const store = waiterStore();
    store.call.get.mockRejectedValue(permissionError);

    store.loadSelectedTable(occupiedTable({ owner_name: '  ' }));
    await flushPromises();

    expect(store.alert.createAlert).toHaveBeenCalledWith(
      'Message',
      'Table is assigned to another waiter',
      'OK',
    );
    expect(router.push).toHaveBeenCalledWith('/Table');
    expect(router.push).not.toHaveBeenCalledWith('/Menu');
    expect(store.menu.cart).toEqual([]);
  });

  it('does not show the owner alert for a non-permission error', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const store = waiterStore();
    store.call.get.mockRejectedValue({
      httpStatus: 500,
      exc_type: 'ValidationError',
      message: 'Could not load the order',
    });

    store.loadSelectedTable(occupiedTable());
    await flushPromises();

    expect(store.alert.createAlert).not.toHaveBeenCalled();
    expect(router.push).not.toHaveBeenCalledWith('/Table');
    expect(router.push).not.toHaveBeenCalledWith('/Menu');
  });

  it.each([
    ['manager', { hasAccess: true, cashier: false }],
    ['cashier', { hasAccess: false, cashier: true }],
  ])('does not show the owner alert when a %s is refused', async (_role, access) => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const store = waiterStore();
    Object.assign(store.auth, access);
    store.call.get.mockRejectedValue(permissionError);

    store.loadSelectedTable(occupiedTable());
    await flushPromises();

    expect(store.alert.createAlert).not.toHaveBeenCalled();
    expect(router.push).not.toHaveBeenCalledWith('/Table');
  });

  it("still loads the signed-in waiter's own table", async () => {
    const waiter = 'waiter.two@salama.example';
    const store = waiterStore();
    store.auth.sessionUser = waiter;
    const item = { item_code: 'COFFEE', item_name: 'Coffee', qty: 1, rate: 5000, comment: '' };
    store.tableMenu = [{ item: 'COFFEE', qty: '', comment: '' }];
    store.call.get.mockImplementation((method) => {
      if (method === invoiceMethod) {
        return Promise.resolve({
          message: {
            name: 'POS-INV-2026-00005',
            waiter,
            items: [item],
            invoice_printed: 0,
            custom_comments: '',
            modified: '2026-10-14 12:00:00',
            grand_total: 5000,
            mobile_number: '',
            customer: '',
          },
        });
      }
      return Promise.resolve({ message: '2026-10-14 12:00:00' });
    });

    store.loadSelectedTable(occupiedTable());
    await flushPromises();

    expect(store.alert.createAlert).not.toHaveBeenCalled();
    expect(store.invoiceNo).toBe('POS-INV-2026-00005');
    expect(store.previousOrderdItem).toEqual([item]);
    expect(store.notification.createNotification).toHaveBeenCalledWith('Past Order Fetched');
    expect(store.menu.cart.map((row) => row.item)).toEqual(['COFFEE']);
    expect(router.push).not.toHaveBeenCalledWith('/Table');
    expect(router.push).not.toHaveBeenCalledWith('/Menu');
  });

  it('still returns to the floor when the route back is rejected', async () => {
    const rejections = [];
    const onUnhandled = (reason) => {
      rejections.push(reason instanceof Error ? reason.message : String(reason));
    };
    process.on('unhandledRejection', onUnhandled);
    try {
      vi.spyOn(console, 'error').mockImplementation(() => {});
      const store = waiterStore();
      store.call.get.mockRejectedValue(permissionError);
      router.push.mockReset();
      router.push.mockRejectedValueOnce(new Error('navigation rejected'));

      store.loadSelectedTable(occupiedTable());
      await flushPromises();
      await new Promise((resolve) => process.nextTick(resolve));

      expect(rejections).toEqual([]);
      expect(router.push).toHaveBeenCalledWith('/Table');
      expect(router.push).not.toHaveBeenCalledWith('/Menu');
    } finally {
      process.off('unhandledRejection', onUnhandled);
    }
  });
});

