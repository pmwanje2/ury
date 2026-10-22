import { createElement, act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initI18n } from '../src/i18n';
import App from '../src/App';
import { checkPOSOpening, getPOSOpeningContext, validatePOSClose } from '../src/lib/pos-opening-api';
import { getCaptainContext } from '../src/lib/captain-context-api';
import { getChecklist } from '../src/lib/checklist-api';

const state = vi.hoisted(() => ({
  user: { name: 'Salama Waiter', roles: ['URY Captain'] },
  profile: {
    name: 'Salama POS',
    company: 'Salama',
    role_allowed_for_billing: [{ role: 'URY Cashier' }],
  },
}));

vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector?: (store: { user: typeof state.user }) => unknown) => {
    const store = { user: state.user };
    return selector ? selector(store) : store;
  },
}));
vi.mock('../src/store/pos-store', () => ({
  usePOSStore: () => ({
    posProfile: state.profile,
    showVoluntaryClosing: false,
    setShowVoluntaryClosing: vi.fn(),
    initializeApp: vi.fn(),
  }),
}));
vi.mock('../src/components/ScreenSizeProvider', () => ({ default: ({ children }: { children: ReactNode }) => children }));
vi.mock('../src/components/AuthGuard', () => ({ default: ({ children }: { children: ReactNode }) => children }));
vi.mock('../src/components/KotAlertListener', () => ({ default: () => null }));
vi.mock('../src/captain/pages/CaptainTables', () => ({ default: () => createElement('div', {}, 'Captain page') }));
vi.mock('../src/captain/pages/CaptainOrder', () => ({ default: () => createElement('div', {}, 'Captain order') }));
vi.mock('../src/pages/Orders', () => ({ default: () => createElement('div', {}, 'Settlement page') }));
vi.mock('../src/pages/Dashboard', () => ({ default: () => createElement('div', {}, 'Analytics page') }));
vi.mock('../src/pages/POS', () => ({ default: () => null }));
vi.mock('../src/pages/Table', () => ({ default: () => null }));
vi.mock('../src/pages/Settings', () => ({ default: () => null }));
vi.mock('../src/lib/pos-opening-api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../src/lib/pos-opening-api')>(),
  checkPOSOpening: vi.fn(),
  getPOSOpeningContext: vi.fn(),
  validatePOSClose: vi.fn(),
  createPOSOpening: vi.fn(),
}));
vi.mock('../src/lib/checklist-api', () => ({ getChecklist: vi.fn(), submitChecklist: vi.fn() }));
vi.mock('../src/lib/captain-context-api', () => ({ getCaptainContext: vi.fn() }));

const occupied = {
  company: 'Salama',
  allowed_profiles: [{ name: 'Salama POS' }],
  selected_profile: 'Salama POS',
  payment_modes: [{ mode_of_payment: 'Cash', opening_amount: 0 }],
  user: 'Salama Waiter',
  user_full_name: 'Salama Waiter',
  branch: 'Main',
  restaurant: 'Salama',
  daily_close_pending: false,
  multi_cashier: { enabled: false, main_cashier_open: false, main_cashier_configured: false },
  permissions: { create: false, submit: false },
  occupied_entry: {
    name: 'POS-OPE-2026-00036',
    user_full_name: 'Salama Cashier',
    period_start_date: '2026-10-22 08:00:00',
    joinable: false,
  },
};

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.clearAllMocks();
  await initI18n('en');
  vi.mocked(checkPOSOpening).mockResolvedValue({ message: 1 });
  vi.mocked(getPOSOpeningContext).mockResolvedValue(occupied);
  vi.mocked(validatePOSClose).mockResolvedValue({ message: 'Success' });
  vi.mocked(getChecklist).mockResolvedValue({ items: [], logName: null, logStatus: 'Complete' });
  vi.mocked(getCaptainContext).mockResolvedValue({
    user: 'Salama Waiter',
    roles: ['URY Captain'],
    branch: 'Main',
    rooms: [],
    pos_profile: {
      name: 'Salama POS',
      transfer_role_permissions: false,
      role_allowed_for_billing: false,
      role_restricted_for_table_order: false,
      remove_items: false,
      show_image: false,
      custom_enable_kot_reprint: false,
      custom_enable_multiple_cashier: false,
    },
    role_restricted_for_table_order: false,
    opening_state: { pos_open: true },
  });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  window.history.replaceState({}, '', '/');
});

async function renderAt(pathname: string) {
  window.history.replaceState({}, '', pathname);
  await act(async () => {
    root.render(createElement(App));
  });
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const text = container.textContent || '';
    if (text.includes('Till already open') || text.includes('Captain page') || text.includes('Captain order')) return;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

describe('waiter opening gate', () => {
  it('admits a waiter at /pos/order when the cashier till is already open', async () => {
    await renderAt('/pos/order');
    expect(container.textContent).toContain('Captain page');
    expect(container.textContent).not.toContain('Till already open');
    expect(checkPOSOpening).not.toHaveBeenCalled();
  });

  it('admits a waiter on a table order route', async () => {
    await renderAt('/pos/order/table/T-05');
    expect(container.textContent).toContain('Captain order');
    expect(container.textContent).not.toContain('Till already open');
    expect(checkPOSOpening).not.toHaveBeenCalled();
  });

  it('redirects a captain-only /pos/ home to the order pad', async () => {
    await renderAt('/pos/');
    expect(window.location.pathname).toBe('/pos/order');
    expect(container.textContent).toContain('Captain page');
    expect(container.textContent).not.toContain('Till already open');
  });
});
