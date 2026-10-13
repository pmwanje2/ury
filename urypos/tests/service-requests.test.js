import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { createApp, nextTick } from 'vue';
import { setImmediate as flushPromises } from 'node:timers/promises';
import { useTableStore } from '../src/stores/Table.js';
import Table from '../src/components/Table.vue';

vi.mock('../src/router', () => ({ default: { push: vi.fn() } }));
vi.mock('../src/stores/Menu.js', () => ({ useMenuStore: () => ({ fetchItems: vi.fn() }) }));
vi.mock('../src/stores/invoiceData.js', () => ({ useInvoiceDataStore: () => ({ tableAttention: 15 }) }));
vi.mock('../src/stores/Auth.js', () => ({ useAuthStore: () => ({ cashier: false }) }));
vi.mock('../src/stores/Customer.js', () => ({ useCustomerStore: () => ({}) }));
vi.mock('../src/stores/Notification.js', () => ({ useNotifications: () => ({}) }));
vi.mock('../src/stores/Alert.js', () => ({ useAlert: () => ({}) }));
vi.mock('../src/stores/recentOrder.js', () => ({ usetoggleRecentOrder: () => ({}) }));
vi.mock('../src/stores/frappeSdk.js', () => ({ default: {
  call: () => ({ get: vi.fn() }), db: () => ({ getDocList: vi.fn() }),
} }));

const requests = [
  { name: 'HELP-1', table: 'T-05', invoice: 'POS-1', request_type: 'Assistance', status: 'Open', requested_at: '2026-10-13 12:00:00' },
  { name: 'BILL-1', table: 'T-06', invoice: 'POS-2', request_type: 'Bill', status: 'Acknowledged', requested_at: '2026-10-13 12:01:00' },
  { name: 'DONE-1', table: 'T-05', invoice: 'POS-1', request_type: 'Assistance', status: 'Resolved', requested_at: '2026-10-13 11:00:00' },
  { name: 'OTHER-1', table: 'Other branch table', invoice: 'POS-3', request_type: 'Assistance', status: 'Open', requested_at: '2026-10-13 12:02:00' },
];
let app, host, store, pinia;

beforeEach(() => {
  pinia = createPinia();
  setActivePinia(pinia);
  vi.useFakeTimers();
  store = useTableStore();
  store.selectedRoom = 'Dining';
  store.call.get.mockResolvedValue({ message: '2026-10-13 12:03:00' });
  store.db.getDocList.mockImplementation(async (doctype, options) => {
    if (doctype === 'URY Table') return [
      { name: 'T-05', occupied: 1, is_take_away: 0 }, { name: 'T-06', occupied: 1, is_take_away: 0 },
    ];
    if (doctype === 'POS Invoice') return [];
    if (doctype === 'URY Service Request') {
      const tables = options.filters.find(filter => filter[0] === 'table')?.[2] || [];
      const types = options.filters.find(filter => filter[0] === 'request_type')?.[2] || [];
      const unresolved = options.filters.some(filter => filter[0] === 'status' && filter[1] === '!=' && filter[2] === 'Resolved');
      return requests.filter(request => tables.includes(request.table) && types.includes(request.request_type)
        && (!unresolved || request.status !== 'Resolved'));
    }
    throw Error(`Unexpected doctype ${doctype}`);
  });
});
afterEach(() => { app?.unmount(); host?.remove(); app = undefined; host = undefined; vi.useRealTimers(); vi.restoreAllMocks(); });

async function render() {
  store.fetchTable();
  await flushPromises();
  host = document.createElement('div');
  document.body.appendChild(host);
  app = createApp(Table);
  app.use(pinia);
  app.mount(host);
  await flushPromises();
  await nextTick();
}
function requestList() { return host.querySelector('[aria-label="Service requests"]'); }

it('shows a neutral count and the type, table and status of both unresolved request types', async () => {
  await render();
  expect(host.textContent).toContain('Requests waiting: 2');
  const rows = requestList().querySelectorAll('li');
  expect(rows).toHaveLength(2);
  expect(rows[0].textContent).toContain('Assistance');
  expect(rows[0].textContent).toContain('T-05');
  expect(rows[0].textContent).toContain('Open');
  expect(rows[1].textContent).toContain('Bill');
  expect(rows[1].textContent).toContain('T-06');
  expect(rows[1].textContent).toContain('Acknowledged');
  expect(requestList().textContent).not.toContain('Bill requests waiting');
  expect(requestList().textContent).not.toContain('Resolved');
  expect(requestList().textContent).not.toContain('Other branch table');
});

it('uses permission-filtered native reads limited to the loaded room tables and both request types', async () => {
  await render();
  expect(store.db.getDocList).toHaveBeenCalledWith('URY Service Request', {
    fields: ['name', 'table', 'invoice', 'request_type', 'status', 'requested_at'],
    filters: [['table', 'in', ['T-05', 'T-06']], ['request_type', 'in', ['Bill', 'Assistance']], ['status', '!=', 'Resolved']],
    orderBy: { field: 'requested_at', order: 'asc' }, limit: 0,
  });
  expect(requestList()?.querySelector('a')?.getAttribute('href')).toBe('/app/ury-service-request/HELP-1');
  expect([...requestList().querySelectorAll('button')].some(button => /Acknowledge|Resolve/.test(button.textContent))).toBe(false);
});

it('shows a new assistance request within 20s without reloading the floor', async () => {
  await render();
  expect(host.textContent).toContain('Requests waiting: 2');
  const floorReads = store.db.getDocList.mock.calls.filter(([doctype]) => doctype === 'URY Table').length;
  const arrived = {
    name: 'HELP-2', table: 'T-05', invoice: 'POS-9', request_type: 'Assistance', status: 'Open', requested_at: '2026-10-13 12:05:00',
  };
  store.db.getDocList.mockImplementation(async doctype => doctype === 'URY Service Request' ? [arrived] : []);
  vi.advanceTimersByTime(19999);
  await flushPromises();
  await nextTick();
  expect(host.textContent).toContain('Requests waiting: 2');
  expect(host.textContent).not.toContain('HELP-2');
  vi.advanceTimersByTime(1);
  await flushPromises();
  await nextTick();
  expect(host.textContent).toContain('Requests waiting: 1');
  expect(host.textContent).toContain('Assistance');
  expect(host.textContent).toContain('T-05');
  expect(host.textContent).toContain('Open');
  expect(store.db.getDocList.mock.calls.filter(([doctype]) => doctype === 'URY Table')).toHaveLength(floorReads);
});

it('refreshes idle requests without reloading floor data and clears the polling timer on unmount', async () => {
  await render();
  const floorReads = store.db.getDocList.mock.calls.filter(([doctype]) => doctype === 'URY Table').length;
  const requestReads = store.db.getDocList.mock.calls.filter(([doctype]) => doctype === 'URY Service Request').length;
  store.db.getDocList.mockImplementation(async doctype => doctype === 'URY Service Request' ? [] : []);
  vi.advanceTimersByTime(30000);
  await flushPromises();
  await nextTick();
  expect(store.db.getDocList.mock.calls.filter(([doctype]) => doctype === 'URY Service Request')).toHaveLength(requestReads + 1);
  expect(store.db.getDocList.mock.calls.filter(([doctype]) => doctype === 'URY Table')).toHaveLength(floorReads);
  expect(requestList()?.textContent).toContain('Requests waiting: 0');
  app.unmount(); app = undefined;
  const reads = store.db.getDocList.mock.calls.length;
  vi.advanceTimersByTime(60000);
  await flushPromises();
  expect(store.db.getDocList).toHaveBeenCalledTimes(reads);
});

it('shows unavailable rather than a false zero when the native request read genuinely fails', async () => {
  store.db.getDocList.mockImplementation(async doctype => {
    if (doctype === 'URY Service Request') throw { httpStatus: 503, message: 'Service unavailable' };
    if (doctype === 'URY Table') return [{ name: 'T-05', occupied: 0, is_take_away: 0 }];
    return [];
  });
  await render();
  expect(host.textContent).toContain('Requests unavailable');
  expect(requestList()?.textContent).not.toContain('Requests waiting: 0');
  expect(host.textContent).toContain('T-05');
});

it.each([
  { httpStatus: 403, exc_type: 'PermissionError', message: 'Not permitted' },
  { httpStatus: 403, message: 'Not permitted' },
  { exc_type: 'PermissionError', message: 'Not permitted' },
])('hides the entire requests block for a native permission denial: %j', async denial => {
  store.db.getDocList.mockImplementation(async doctype => {
    if (doctype === 'URY Service Request') throw denial;
    if (doctype === 'URY Table') return [{ name: 'T-05', occupied: 0, is_take_away: 0 }];
    return [];
  });
  await render();
  expect(requestList()).toBeNull();
  expect(host.textContent).not.toContain('Requests unavailable');
  expect(host.textContent).toContain('T-05');
});

it('clears visible requests on denial and distinguishes later genuine failure and recovery', async () => {
  await render();
  expect(requestList().textContent).toContain('Assistance');
  store.db.getDocList.mockRejectedValue({ httpStatus: 403, exc_type: 'PermissionError' });
  await store.fetchServiceRequests();
  await nextTick();
  expect(requestList()).toBeNull();
  expect(host.textContent).not.toContain('Assistance');
  store.db.getDocList.mockRejectedValue({ httpStatus: 500, exc_type: 'ValidationError' });
  await store.fetchServiceRequests();
  await nextTick();
  expect(requestList().textContent).toContain('Requests unavailable');
  store.db.getDocList.mockResolvedValue([requests[0]]);
  await store.fetchServiceRequests();
  await nextTick();
  expect(requestList().textContent).toContain('Requests waiting: 1');
  expect(requestList().textContent).toContain('Assistance');
});

it('does not publish a late request response from the previous room', async () => {
  let finish;
  store.db.getDocList.mockImplementation(async doctype => {
    if (doctype === 'URY Table') return [{ name: 'T-05', occupied: 0, is_take_away: 0 }];
    if (doctype === 'URY Service Request') return new Promise(resolve => { finish = resolve; });
    return [];
  });
  store.fetchTable();
  await flushPromises();
  store.selectedRoom = 'Terrace';
  store.db.getDocList.mockImplementation(async doctype => doctype === 'URY Table' ? [{ name: 'Terrace 1', occupied: 0, is_take_away: 0 }] : []);
  store.fetchTable();
  await flushPromises();
  expect(finish, 'The first floor load must request service requests').toBeDefined();
  finish([requests[0]]);
  await flushPromises();
  expect(store.serviceRequests).toEqual([]);
});

it('does not poll the old room tables while the new room floor is still loading', async () => {
  await render();
  const reads = store.db.getDocList.mock.calls.filter(([doctype]) => doctype === 'URY Service Request').length;
  store.selectedRoom = 'Terrace';
  store.db.getDocList.mockImplementation(async doctype => doctype === 'URY Table' ? new Promise(() => {}) : [requests[0]]);
  store.fetchTable();
  vi.advanceTimersByTime(30000);
  await flushPromises();
  await nextTick();
  expect(store.db.getDocList.mock.calls.filter(([doctype]) => doctype === 'URY Service Request')).toHaveLength(reads);
  expect(requestList().textContent).toContain('Loading requests');
  expect(requestList().textContent).not.toContain('T-05');
});

it('shows requests unavailable when loading the room tables is denied, not loading forever', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
  store.db.getDocList.mockRejectedValue(Error('Room access denied'));
  await render();
  expect(host.textContent).toContain('Requests unavailable');
  expect(host.textContent).not.toContain('Loading requests');
  expect(store.db.getDocList.mock.calls.some(([doctype]) => doctype === 'URY Service Request')).toBe(false);
});
