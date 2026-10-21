import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as core from '@ury/core';
import AuthGuard from '../src/components/AuthGuard';
import { useFloorIdle } from '../src/lib/floor-session';

const boundary = vi.hoisted(() => ({
  clearCashierStorage: vi.fn(),
  clearAppStorage: vi.fn(),
  disconnectQzPrinter: vi.fn(),
  auth: {
    checkAuth: vi.fn(),
    user: { name: 'waiter@example.com', roles: ['URY Captain'] },
    isLoading: false,
    error: null as string | null,
    fetchPosProfile: vi.fn(),
    posProfile: {
      floor_idle_signout_seconds: 60,
      custom_floor_stay_signed_in_roles: [],
    },
    hasAccess: true,
  },
}));

vi.mock('@ury/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@ury/core')>();
  return {
    ...actual,
    clearAppStorage: boundary.clearAppStorage,
    disconnectQzPrinter: boundary.disconnectQzPrinter,
  };
});
vi.mock('../src/offline/cashier', () => ({
  clearCashierStorage: boundary.clearCashierStorage,
}));
vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector?: (state: typeof boundary.auth) => unknown) =>
    selector ? selector(boundary.auth) : boundary.auth,
}));
vi.mock('../src/components/FloorPinPad', () => ({ default: () => null }));

let container: HTMLDivElement;
let root: Root;

function IdleProbe({
  user,
  profile,
}: {
  user: { roles: string[] };
  profile: { floor_idle_signout_seconds?: number; custom_floor_stay_signed_in_roles?: Array<{ role?: string }> };
}) {
  useFloorIdle(user, profile);
  return null;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  window.floor_pad = { paired: true, till_open: true };
  localStorage.clear();
  sessionStorage.clear();
  localStorage.setItem('userAuth', 'true');
  boundary.auth.user = { name: 'waiter@example.com', roles: ['URY Captain'] };
  boundary.auth.posProfile = {
    floor_idle_signout_seconds: 60,
    custom_floor_stay_signed_in_roles: [],
  };
  boundary.clearCashierStorage.mockResolvedValue(undefined);
});

afterEach(() => {
  if (root) act(() => root.unmount());
  container?.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function render(node: React.ReactNode) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(node));
}

function stubReplace() {
  const browserWindow = window;
  const replacement = vi.fn();
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) {
      if (key === 'location') return { replace: replacement };
      return Reflect.get(target, key, target);
    },
  }));
  return replacement;
}

describe('paired floor idle sign-out', () => {
  it('AuthGuard signs out a non-stay user after the configured idle period', async () => {
    const replacement = stubReplace();
    render(<AuthGuard><div>POS</div></AuthGuard>);

    await act(async () => vi.advanceTimersByTimeAsync(59999));
    expect(boundary.clearCashierStorage).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(1));

    expect(boundary.clearCashierStorage).toHaveBeenCalledExactlyOnceWith(true, undefined);
    expect(replacement).toHaveBeenCalledExactlyOnceWith('/pos/order');
  });

  it.each([
    ['zero seconds', { floor_idle_signout_seconds: 0 }, { paired: true, till_open: true }, []],
    ['stay-signed-in role', { floor_idle_signout_seconds: 60 }, { paired: true, till_open: true }, [{ role: 'URY Captain' }]],
    ['unpaired tablet', { floor_idle_signout_seconds: 60 }, { paired: false, till_open: true }, []],
    ['closed till', { floor_idle_signout_seconds: 60 }, { paired: true, till_open: false }, []],
  ])('does not arm for %s', (_name, profile, floorPad, stayRoles) => {
    window.floor_pad = floorPad;
    render(<IdleProbe
      user={{ roles: ['URY Captain'] }}
      profile={{ ...profile, custom_floor_stay_signed_in_roles: stayRoles }}
    />);

    vi.advanceTimersByTime(120000);
    expect(boundary.clearCashierStorage).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('stops the watcher when the waiter screen unmounts', () => {
    const replacement = stubReplace();
    render(<IdleProbe
      user={{ roles: ['URY Captain'] }}
      profile={{ floor_idle_signout_seconds: 60, custom_floor_stay_signed_in_roles: [] }}
    />);

    act(() => root.unmount());
    vi.advanceTimersByTime(120000);

    expect(boundary.clearCashierStorage).not.toHaveBeenCalled();
    expect(replacement).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('POS Profile floor-session settings', () => {
  it('carries idle and after-send flags from the limited profile response', async () => {
    const response = {
      pos_profile: 'POS-1',
      floor_idle_signout_seconds: 60,
      floor_signout_after_send: 1,
    };
    vi.spyOn(core.call, 'get').mockResolvedValue({ message: response } as never);
    vi.spyOn(core.db, 'getDoc').mockResolvedValue({ name: 'POS-1' } as never);

    const { getCombinedPosProfile } = await import('../src/lib/pos-profile-api');
    const profile = await getCombinedPosProfile();

    expect(profile.floor_idle_signout_seconds).toBe(60);
    expect(profile.floor_signout_after_send).toBe(1);
  });
});
