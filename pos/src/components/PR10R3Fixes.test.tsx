import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { call, db } from '@ury/core';
import { initI18n } from '../i18n';
import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';
import OfflineBanner from './OfflineBanner';
import { testLocks } from '../../tests/offline-locks';
import type { AvailableOutlet } from '../lib/outlet-api';

const rootInit = useRootStore.getState();
const posInit = usePOSStore.getState();

const TWO_OUTLETS: AvailableOutlet[] = [
  {
    pos_profile: 'Main Grill POS',
    name: 'Main Grill POS',
    branch: 'Main Grill',
    company: 'Maximus Hospitality',
    restaurant: 'Main Grill',
    currency: 'UGX',
    has_open_shift: false,
    open_entry: null,
    is_active: false,
  },
  {
    pos_profile: 'Pool Bar POS',
    name: 'Pool Bar POS',
    branch: 'Pool Bar',
    company: 'Maximus Hospitality',
    restaurant: 'Pool Bar',
    currency: 'USD',
    has_open_shift: false,
    open_entry: null,
    is_active: false,
  },
];

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(async () => {
  sessionStorage.clear();
  localStorage.clear();
  document.cookie = 'user_id=' + encodeURIComponent('cashier@example.test') + '; Path=/';
  await initI18n('en');
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.cookie = 'user_id=; Max-Age=0; Path=/';
  sessionStorage.clear();
  localStorage.clear();
  useRootStore.setState(rootInit, true);
  usePOSStore.setState(posInit, true);
});

describe('Hydrogen R2 / R3 P0 regressions (initializeApp parallel fan-out & provisional offline banner)', () => {
  it('[R3 P0] initializeApp starts profile, outlets, categories, and payment modes in parallel and preserves P0-1 cold-boot outlet picker', async () => {
    const profileGate = deferred<{ message: unknown }>();
    const called: string[] = [];

    vi.spyOn(call, 'get').mockImplementation((method: string) => {
      called.push(method);
      if (method === 'ury.ury_pos.api.getPosProfile') {
        return profileGate.promise;
      }
      if (method === 'ury.ury_pos.api.getPosProfileFull') {
        return Promise.reject(new Error('getPosProfileFull must not be called when outlet selection is required'));
      }
      if (method === 'ury.ury_pos.outlet_context.get_available_outlets') {
        return Promise.resolve({ message: TWO_OUTLETS });
      }
      if (method === 'ury.ury_pos.api.getMenuCourses') {
        return Promise.resolve({ message: [] });
      }
      if (method === 'ury.ury_pos.api.getModeOfPayment') {
        return Promise.resolve({ message: [{ mode_of_payment: 'Cash', opening_amount: 0 }] });
      }
      return Promise.resolve({ message: {} });
    });

    const initPromise = usePOSStore.getState().initializeApp();
    // Flush microtasks/macrotask so bindOfflineUser settles while getPosProfile stays pending.
    await new Promise((resolve) => setTimeout(resolve, 0));

    // All startup fetches must fan out in parallel without blocking behind await fetchPosProfile().
    expect(called).toContain('ury.ury_pos.api.getPosProfile');
    expect(called).toContain('ury.ury_pos.outlet_context.get_available_outlets');
    expect(called).toContain('ury.ury_pos.api.getMenuCourses');
    expect(called).toContain('ury.ury_pos.api.getModeOfPayment');

    profileGate.resolve({
      message: {
        requires_outlet_selection: true,
        available_outlets: TWO_OUTLETS,
      },
    });
    await initPromise;

    const state = usePOSStore.getState();
    expect(state.requiresOutletSelection).toBe(true);
    expect(state.availableOutlets).toHaveLength(2);
    expect(state.posProfile).toBeNull();
    expect(state.isInitializing).toBe(false);
    expect(state.error).toBeNull();
  });

  it('[R3 P0] initializeApp hydrates posProfile and availableOutlets cleanly in parallel for resolved outlets', async () => {
    const profileGate = deferred<{ message: unknown }>();
    const called: string[] = [];

    vi.spyOn(call, 'get').mockImplementation((method: string) => {
      called.push(method);
      if (method === 'ury.ury_pos.api.getPosProfile') {
        return profileGate.promise;
      }
      if (method === 'ury.ury_pos.api.getPosProfileFull') {
        return Promise.resolve({
          message: {
            name: 'Main Grill POS',
            company: 'Maximus Hospitality',
            country: 'Uganda',
            currency: 'UGX',
            branch: 'Main Grill',
            restaurant: 'Main Grill',
            warehouse: 'Stores',
            disabled: 0,
            role_allowed_for_billing: [{ role: 'URY Cashier' }],
            role_restricted_for_table_order: [],
            transfer_role_permissions: [],
            enable_payment_without_printing: 0,
          },
        });
      }
      if (method === 'ury.ury_pos.outlet_context.get_available_outlets') {
        return Promise.resolve({ message: TWO_OUTLETS });
      }
      if (method === 'ury.ury_pos.api.getMenuCourses') {
        return Promise.resolve({ message: [] });
      }
      if (method === 'ury.ury_pos.api.getModeOfPayment') {
        return Promise.resolve({ message: [{ mode_of_payment: 'Cash', opening_amount: 0 }] });
      }
      if (method === 'ury.ury_pos.api.getRestaurantMenu') {
        return Promise.resolve({ message: { items: [] } });
      }
      return Promise.resolve({ message: {} });
    });
    vi.spyOn(db, 'getDoc').mockResolvedValue({ name: 'UGX', symbol: 'UGX' } as never);

    const initPromise = usePOSStore.getState().initializeApp();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(called).toContain('ury.ury_pos.outlet_context.get_available_outlets');
    expect(called).toContain('ury.ury_pos.api.getMenuCourses');
    expect(called).toContain('ury.ury_pos.api.getModeOfPayment');

    profileGate.resolve({
      message: {
        pos_profile: 'Main Grill POS',
        branch: 'Main Grill',
        company: 'Maximus Hospitality',
        currency: 'UGX',
        country: 'Uganda',
        waiter: 'cashier@example.test',
        cashier: 'cashier@example.test',
        owner: 'cashier@example.test',
        warehouse: 'Stores',
        print_format: 'Receipt',
        qz_print: 0,
        qz_host: null,
        printer: null,
        print_type: 'Network',
        tableAttention: 10,
        paid_limit: 0,
        disable_rounded_total: 0,
        enable_discount: 1,
        multiple_cashier: 0,
        enable_payment_without_printing: 0,
        requires_outlet_selection: false,
        available_outlets_count: 2,
      },
    });
    await initPromise;

    const state = usePOSStore.getState();
    expect(state.requiresOutletSelection).toBe(false);
    expect(state.posProfile?.name).toBe('Main Grill POS');
    expect(state.availableOutlets).toHaveLength(2);
    expect(state.isInitializing).toBe(false);
    expect(state.error).toBeNull();
  });

  it('[R3 P0] OfflineBanner renders synchronously for provisional offline sessions before IndexedDB settles and stays fail-closed when sessionBlocked', () => {
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
    Object.defineProperty(navigator, 'locks', { configurable: true, value: testLocks() });
    // Keep IndexedDB open pending so any reliance on async queue.list() causes OfflineBanner to return null on mount.
    vi.stubGlobal('indexedDB', {
      open: () => ({}) as unknown as IDBOpenDBRequest,
    });
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))));

    const offlineProfile = {
      name: 'Till A',
      company: 'Company A',
      currency: 'USD',
      country: 'US',
      ury_offline_capture: 1,
      role_allowed_for_billing: [{ role: 'URY Cashier' }],
      offline_payment_modes: [
        { mode_of_payment: 'Cash', type: 'Cash', allow_offline: true, requires_reference: false },
      ],
    };

    useRootStore.setState({
      user: { name: 'alice@example.test', roles: ['URY Cashier'], provisional: true },
    });
    usePOSStore.setState({
      posProfile: offlineProfile as unknown as ReturnType<typeof usePOSStore.getState>['posProfile'],
      currency: 'USD',
    });

    render(<OfflineBanner />);
    expect(screen.getByTestId('offline-banner')).toBeInTheDocument();

    // Fail-closed: if the offline session was revoked/blocked online, banner must NOT advertise offline capture.
    cleanup();
    sessionStorage.setItem('ury_offline_session_blocked:alice@example.test', '1');
    usePOSStore.setState({
      posProfile: { ...offlineProfile } as unknown as ReturnType<typeof usePOSStore.getState>['posProfile'],
    });
    render(<OfflineBanner />);
    expect(screen.queryByTestId('offline-banner')).not.toBeInTheDocument();
  });
});
