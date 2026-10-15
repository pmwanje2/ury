import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import TableView from '../src/pages/Table';
import { initI18n } from '../src/i18n';

const boundary = vi.hoisted(() => ({
  get: vi.fn(), getDocList: vi.fn(),
  user: { name: 'manager@fixture.invalid', roles: ['URY Manager'] },
  posProfile: { branch: 'Branch A', transfer_role_permissions: [{ role: 'URY Manager' }] },
}));
vi.mock('@ury/core', async (importOriginal) => ({
  ...await importOriginal<typeof import('@ury/core')>(),
  call: { get: boundary.get }, db: { getDocList: boundary.getDocList },
}));
vi.mock('../src/store/pos-store', () => ({ usePOSStore: () => ({ posProfile: boundary.posProfile }) }));
vi.mock('../src/store/root-store', () => ({ useRootStore: (select: Function) => select({ user: boundary.user }) }));

const method = 'ury.ury.doctype.ury_order.ury_order.get_captain_transfer_candidates';
const eligible = [{ name: 'waiter@fixture.invalid', full_name: 'Eligible Waiter' }];
let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.resetAllMocks();
  sessionStorage.clear();
  boundary.user.roles = ['URY Manager'];
  await initI18n('en');
  boundary.getDocList.mockImplementation(async (doctype, options) => {
    if (doctype === 'URY Room') return [{ name: 'Dining', branch: 'Branch A' }];
    if (options.fields[0] === 'count(name) as count') return [{ count: 2 }];
    if (doctype === 'URY Table') return ['Table 1', 'Table 2'].map(name => ({
      name, occupied: 1, latest_invoice_time: null, is_take_away: 0,
      restaurant_room: 'Dining', table_shape: 'Square',
    }));
    throw new Error(`Unexpected doctype: ${doctype}`);
  });
  boundary.get.mockImplementation(async (endpoint, args) => {
    if (endpoint.endsWith('.get_order_invoice')) return { message: {
      name: `INV-${args.table}`, waiter: 'current@fixture.invalid', docstatus: 0,
    } };
    if (endpoint === method) return { message: [] };
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function render() {
  await act(async () => root.render(<MemoryRouter><TableView /></MemoryRouter>));
}

async function toggle(index = 0) {
  await act(async () => container.querySelectorAll<HTMLButtonElement>('button[aria-expanded]')[index].click());
}

function transferButton() {
  return [...container.querySelectorAll('button')].find(button => button.textContent === 'Transfer captain');
}

it('hides Transfer captain when the active invoice has no eligible candidates', async () => {
  await render();
  await toggle();
  expect(transferButton()).toBeUndefined();
  expect(boundary.get).toHaveBeenCalledWith(method, { invoice: 'INV-Table 1' });
});

it('shows Transfer captain only after eligible candidates load', async () => {
  let resolveCandidates!: (value: unknown) => void;
  boundary.get.mockImplementation(async (endpoint) => endpoint === method
    ? new Promise(resolve => { resolveCandidates = resolve; })
    : { message: { name: 'INV-Table 1', waiter: 'current@fixture.invalid', docstatus: 0 } });
  await render();
  await toggle();
  expect(transferButton()).toBeUndefined();
  await act(async () => resolveCandidates({ message: eligible }));
  expect(transferButton()).toBeDefined();
});

it('keeps Transfer captain hidden when the candidate read is refused', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
  boundary.get.mockImplementation(async endpoint => {
    if (endpoint === method) throw new Error('Transfer refused');
    return { message: { name: 'INV-Table 1', waiter: 'current@fixture.invalid', docstatus: 0 } };
  });
  await render();
  await toggle();
  expect(transferButton()).toBeUndefined();
});

it('retains the transfer-role gate even if candidates would exist', async () => {
  boundary.user.roles = ['URY Captain'];
  boundary.get.mockResolvedValue({ message: eligible });
  await render();
  await toggle();
  expect(transferButton()).toBeUndefined();
  expect(boundary.get).not.toHaveBeenCalledWith(method, expect.anything());
});

it('rechecks candidates on reopening instead of retaining a stale available action', async () => {
  boundary.get.mockImplementation(async endpoint => ({ message: endpoint === method
    ? eligible : { name: 'INV-Table 1', waiter: 'current@fixture.invalid', docstatus: 0 } }));
  await render();
  await toggle();
  expect(transferButton()).toBeDefined();
  await toggle();
  boundary.get.mockImplementation(async endpoint => ({ message: endpoint === method
    ? [] : { name: 'INV-Table 1', waiter: 'current@fixture.invalid', docstatus: 0 } }));
  await toggle();
  expect(transferButton()).toBeUndefined();
});

it('ignores a late candidate response from a different table menu', async () => {
  let resolveOld!: (value: unknown) => void;
  const oldResponse = new Promise(resolve => { resolveOld = resolve; });
  boundary.get.mockImplementation(async (endpoint, args) => {
    if (endpoint === method) return args.invoice === 'INV-Table 1'
      ? oldResponse : { message: [] };
    return { message: { name: `INV-${args.table}`, waiter: 'current@fixture.invalid', docstatus: 0 } };
  });
  await render();
  await toggle();
  await toggle(1);
  await act(async () => resolveOld({ message: eligible }));
  expect(transferButton()).toBeUndefined();
});
