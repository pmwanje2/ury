import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as core from '@ury/core';
import AuthGuard from '../src/components/AuthGuard';

// Theme T4 (fork #70): idle sign-out on a paired floor tablet, configured on POS Profile.

const reprint = vi.hoisted(() => vi.fn());
vi.mock('../src/offline/use-outbox', () => ({
  useOutbox: (profile: { name?: string } | null, user: { name: string }) => ({
    queue: { reprint }, offline: false, replaying: false,
    rows: profile?.name === 'Till' && user.name === 'waiter@example.com' ? [{
      sequence: 1, client_order_id: 'queued', order: { customer: 'Guest' }, rounds: [{ expected_total: 12, offline_printed: 0 }], state: 'pending',
    }] : [],
  }),
}));

const guard = vi.hoisted(() => vi.fn(async (run: () => Promise<void>) => run()));
vi.mock('../src/offline/outbox', async (original) => ({
  ...await original<typeof import('../src/offline/outbox')>(), withEmptyOutbox: guard,
}));

const boundary = vi.hoisted(() => ({
  logout: vi.fn(),
  disconnectQzPrinter: vi.fn(),
  auth: {
    checkAuth: vi.fn(),
    user: { name: 'waiter@example.com', roles: ['URY Captain'] } as { name: string; roles: string[] },
    isLoading: false,
    error: null as string | null,
    fetchPosProfile: vi.fn(),
    posProfile: {} as Record<string, unknown>,
    hasAccess: true,
    allowedRoles: ['URY Captain'],
  },
}));

vi.mock('@ury/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@ury/core')>();
  return { ...actual, logout: boundary.logout, disconnectQzPrinter: boundary.disconnectQzPrinter };
});
vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector?: (state: typeof boundary.auth) => unknown) =>
    selector ? selector(boundary.auth) : boundary.auth,
}));

type IdleHook = (
  user: { roles: string[] } | null,
  profile: { floor_idle_signout_seconds?: number; custom_floor_stay_signed_in_roles?: Array<{ role?: string }> } | null,
) => void;

async function useFloorIdleHook(): Promise<IdleHook> {
  const path = '../src/lib/floor-session';
  const mod = await import(/* @vite-ignore */ path).catch(() => null);
  expect(mod?.useFloorIdle, 'floor-session must export useFloorIdle').toBeTypeOf('function');
  return mod.useFloorIdle as IdleHook;
}

let container: HTMLDivElement;
let root: Root | undefined;
let replacement: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  Object.defineProperty(window, 'floor_pad', { configurable: true, value: { paired: true, till_open: true } });
  localStorage.clear();
  sessionStorage.clear();
  sessionStorage.setItem('userAuth', 'waiter@example.com');
  boundary.auth.user = { name: 'waiter@example.com', roles: ['URY Captain'] };
  boundary.auth.posProfile = { floor_idle_signout_seconds: 60, custom_floor_stay_signed_in_roles: [] };
  boundary.logout.mockResolvedValue(undefined);
  replacement = vi.fn();
  const browserWindow = window;
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) {
      if (key === 'location') return { replace: replacement };
      return Reflect.get(target, key, target);
    },
  }));
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  root = undefined;
  container?.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete (window as Window & { floor_pad?: unknown }).floor_pad;
});

function render(node: React.ReactNode) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root!.render(node));
}

describe('paired floor idle sign-out', () => {
  it('AuthGuard signs out a non-stay user after the configured idle period', async () => {
    render(<AuthGuard><div>POS</div></AuthGuard>);
    expect(container.textContent).toContain('POS');

    await act(async () => vi.advanceTimersByTimeAsync(59999));
    expect(boundary.logout).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(1));

    expect(boundary.logout).toHaveBeenCalledOnce();
    expect(replacement).toHaveBeenCalledExactlyOnceWith('/pos/order');
  });

  it.each([
    ['zero seconds', { floor_idle_signout_seconds: 0 }, { paired: true, till_open: true }, []],
    ['stay-signed-in role', { floor_idle_signout_seconds: 60 }, { paired: true, till_open: true }, [{ role: 'URY Captain' }]],
    ['unpaired tablet', { floor_idle_signout_seconds: 60 }, { paired: false, till_open: true }, []],
    ['closed till', { floor_idle_signout_seconds: 60 }, { paired: true, till_open: false }, []],
  ])('does not arm for %s', async (_name, profile, floorPad, stayRoles) => {
    const useFloorIdle = await useFloorIdleHook();
    Object.defineProperty(window, 'floor_pad', { configurable: true, value: floorPad });
    function IdleProbe() {
      useFloorIdle({ roles: ['URY Captain'] }, { ...profile, custom_floor_stay_signed_in_roles: stayRoles });
      return null;
    }
    render(<IdleProbe />);

    vi.advanceTimersByTime(120000);
    expect(boundary.logout).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('stops the watcher when the waiter screen unmounts', async () => {
    const useFloorIdle = await useFloorIdleHook();
    function IdleProbe() {
      useFloorIdle({ roles: ['URY Captain'] }, { floor_idle_signout_seconds: 60, custom_floor_stay_signed_in_roles: [] });
      return null;
    }
    render(<IdleProbe />);
    expect(vi.getTimerCount()).toBe(1);

    act(() => root!.unmount());
    root = undefined;
    vi.advanceTimersByTime(120000);

    expect(boundary.logout).not.toHaveBeenCalled();
    expect(replacement).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('POS Profile floor-session settings', () => {
  it('carries idle and after-send flags and the stay-signed-in roles into the combined profile', async () => {
    vi.spyOn(core.call, 'get').mockImplementation((async (method: string) => {
      if (method === 'ury.ury_pos.api.getPosProfile') {
        return { message: { pos_profile: 'POS-1', floor_idle_signout_seconds: 60, floor_signout_after_send: 1 } };
      }
      return { message: { name: 'POS-1', custom_floor_stay_signed_in_roles: [{ role: 'URY Manager' }] } };
    }) as never);
    vi.spyOn(console, 'log').mockImplementation(() => {});

    const { getCombinedPosProfile } = await import('../src/lib/pos-profile-api');
    const profile = await getCombinedPosProfile() as unknown as Record<string, unknown>;

    expect(profile.floor_idle_signout_seconds).toBe(60);
    expect(profile.floor_signout_after_send).toBe(1);
    expect(profile.custom_floor_stay_signed_in_roles).toEqual([{ role: 'URY Manager' }]);
  });

  it('defaults both floor flags to off when the server omits them', async () => {
    vi.spyOn(core.call, 'get').mockImplementation((async (method: string) => ({
      message: method === 'ury.ury_pos.api.getPosProfile' ? { pos_profile: 'POS-1' } : { name: 'POS-1' },
    })) as never);
    vi.spyOn(console, 'log').mockImplementation(() => {});

    const { getCombinedPosProfile } = await import('../src/lib/pos-profile-api');
    const profile = await getCombinedPosProfile() as unknown as Record<string, unknown>;

    expect(profile.floor_idle_signout_seconds).toBe(0);
    expect(profile.floor_signout_after_send).toBe(0);
  });
});

it('preserves the session while offline orders await sync', async () => {
  guard.mockRejectedValueOnce(new Error('Offline outbox has pending items'));
  render(<AuthGuard><div>POS</div></AuthGuard>);
  await act(async () => vi.advanceTimersByTimeAsync(60000));
  expect(boundary.logout).not.toHaveBeenCalled();
  expect(sessionStorage.getItem('userAuth')).toBe('waiter@example.com');
  expect(replacement).not.toHaveBeenCalled();
});

it.each([false, true])('retries deferred idle sign-out unless activity resumes (activity=%s)', async (active) => {
  guard.mockRejectedValueOnce(new Error('Offline outbox has pending items'));
  render(<AuthGuard><div>POS</div></AuthGuard>);
  await act(async () => vi.advanceTimersByTimeAsync(60000));
  expect(boundary.logout).not.toHaveBeenCalled();
  if (active) window.dispatchEvent(new Event('pointerdown'));
  // Replay has drained the queue. The next guarded attempt can proceed.
  await act(async () => vi.advanceTimersByTimeAsync(10000));
  expect(boundary.logout).toHaveBeenCalledTimes(active ? 0 : 1);
  if (active) {
    await act(async () => vi.advanceTimersByTimeAsync(50000));
    expect(boundary.logout).toHaveBeenCalledOnce();
  }
});

it('locks identity switching on cold boot until the existing outbox drains', async () => {
  sessionStorage.removeItem('userAuth');
  boundary.auth.posProfile = { name: 'Till', ury_offline_capture: 1 };
  guard.mockRejectedValueOnce(new Error('Offline outbox has pending items'));
  render(<AuthGuard><div>Protected POS</div></AuthGuard>);
  await act(async () => {});
  expect(container.querySelector('form, [aria-label="4-digit PIN"]')).toBeNull();
  expect(container.textContent).not.toContain('Protected POS');
  expect(container.querySelector('[role=status]')).not.toBeNull();
  const print = container.querySelector<HTMLButtonElement>('[data-client-order="queued"] button');
  expect(print).not.toBeNull();
  await act(async () => print!.click());
  expect(reprint).toHaveBeenCalledWith('queued', 'kot');
  expect(boundary.logout).not.toHaveBeenCalled();
  await act(async () => vi.advanceTimersByTimeAsync(10000));
  expect(boundary.logout).toHaveBeenCalledOnce();
});
