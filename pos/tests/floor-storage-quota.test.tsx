/** @vitest-environment-options {"url":"https://register.test/pos/order/table/T1"} */
import { BroadcastChannel as NodeChannel } from 'node:worker_threads';
import { IDBDatabase, IDBFactory } from 'fake-indexeddb';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import * as core from '@ury/core';
import type { AxiosInstance } from 'axios';
import { getTableOrder } from '../src/lib/order-api';
import { getRealtimeSocket } from '../src/lib/realtime';
import AuthGuard from '../src/components/AuthGuard';
import OutboxPanel from '../src/components/OutboxPanel';
import { cashierOutbox, clearCashierStorage } from '../src/offline/cashier';
import { onOutboxMembershipChange, withOutboxLogin } from '../src/offline/outbox';
import { useOutbox } from '../src/offline/use-outbox';
import { beginFloorLogin, clearFloorLogoutFailureMarker, confirmVerifiedLogin, currentFloorGeneration, floorSignOut, hasFloorSessionMarker, setFloorSessionMarker } from '../src/lib/floor-session';
import * as logoutStore from '../src/lib/logout-store';
import { readLogoutMarker, writeLogoutMarker } from '../src/lib/logout-store';
import { isFloorSessionConfirmedForTab } from '../src/lib/floor-tab';
import TillLogout from '../src/components/TillLogout';
import FloorPinPad from '../src/components/FloorPinPad';
import { testLocks } from './offline-locks';

const state = vi.hoisted(() => ({
  user: { name: 'owner@example.test', roles: ['Cashier'] },
  posProfile: { name: 'Till', custom_floor_stay_signed_in_roles: [{ role: 'Cashier' }], ury_offline_capture: 1 },
  checkAuth: vi.fn(), fetchPosProfile: vi.fn(), hasAccess: true,
}));
vi.mock('../src/store/root-store', () => ({ useRootStore: () => state }));

const capture = () => cashierOutbox(state.posProfile, state.user).capture({
  pos_profile: 'Till', no_of_pax: 1, order_type: 'Take Away', invoice: null, last_invoice: null,
  customer: 'Private order', items: [{ item: 'Tea', item_name: 'Tea', qty: 1, rate: 10 }],
}, 10);

beforeEach(() => {
  document.cookie = 'user_id=' + encodeURIComponent(state.user.name) + '; Path=/';
  sessionStorage.clear();
  localStorage.clear();
  vi.stubGlobal('indexedDB', new IDBFactory());
  vi.stubGlobal('navigator', { onLine: true, locks: testLocks() });
  vi.stubGlobal('BroadcastChannel', undefined);
  const original = Storage.prototype.setItem;
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
    if (this === localStorage) throw new DOMException('Full', 'QuotaExceededError');
    original.call(this, key, value);
  });
  vi.spyOn(window, 'open').mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  document.cookie = 'user_id=; Max-Age=0; Path=/';
  document.cookie = 'ury_floor_generation=; Max-Age=0; Path=/pos; SameSite=Strict; Secure';
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.cookie = 'ury_floor_logout_failed=; Max-Age=0; Path=/pos; SameSite=Strict; Secure';
});

it('locks an admitted tab on its next poll without exposing or replaying the newly committed row', async () => {
  const queue = cashierOutbox(state.posProfile, state.user);
  const replay = vi.spyOn(queue, 'replay').mockResolvedValue();
  const timer = vi.spyOn(window, 'setInterval');
  const observed: string[] = [];
  function Workspace() {
    const { rows } = useOutbox(state.posProfile, state.user, true);
    observed.push(...rows.map(row => row.client_order_id));
    return <main>Workspace<OutboxPanel posProfile={state.posProfile} user={state.user} /></main>;
  }
  render(<AuthGuard><Workspace /></AuthGuard>);
  await screen.findByText('Workspace');
  await act(async () => { await queue.list(); });
  // Commit as another window: its DOM events are not delivered to our window.
  const dispatch = vi.spyOn(window, 'dispatchEvent').mockReturnValue(true);
  try { await capture(); } finally { dispatch.mockRestore(); }
  replay.mockClear();
  const polls = timer.mock.calls.filter(([, delay]) => delay === 5000);
  expect(polls.length).toBeGreaterThan(0);
  await act(async () => { for (const [wake] of polls) await (wake as () => Promise<void>)(); });
  await screen.findByText(/account that owns the queued orders/);
  expect(observed).toEqual([]);
  expect(replay).not.toHaveBeenCalled();
  expect(document.querySelector('[data-client-order]')).toBeNull();
});

it('sends and receives cross-tab membership notifications despite localStorage quota', async () => {
  vi.stubGlobal('BroadcastChannel', NodeChannel);
  const remote = new NodeChannel('ury-outbox-membership');
  const received = vi.fn();
  remote.onmessage = received;
  const recheck = vi.fn();
  const unsubscribe = onOutboxMembershipChange(recheck);
  try {
    await capture();
    await waitFor(() => expect(received).toHaveBeenCalled());
    recheck.mockClear();
    remote.postMessage(null);
    await waitFor(() => expect(recheck).toHaveBeenCalled());
  } finally { unsubscribe(); remote.close(); }
});

it.each(['server logout', 'owner login'])('keeps a failed till logout across tab closure until successful %s', async (resolution) => {
  const logout = vi.spyOn(core, 'logout').mockResolvedValue(false);
  await expect(clearCashierStorage(true, state.user.name)).rejects.toThrow();
  expect(localStorage.getItem('ury_floor_logout_failed')).toBeNull();
  sessionStorage.clear();
  window.name = '';
  const Workspace = vi.fn(() => <main>Workspace</main>);
  render(<AuthGuard><Workspace /></AuthGuard>);
  await screen.findByLabelText('Password');
  expect(Workspace).not.toHaveBeenCalled();
  expect(logout).toHaveBeenCalledTimes(2);
  if (resolution === 'server logout') {
    logout.mockResolvedValue(true);
    await clearCashierStorage(true, state.user.name);
  } else {
    vi.spyOn(core.auth, 'loginWithUsernamePassword').mockResolvedValue({ message: 'Logged In' } as never);
    vi.spyOn(core.auth, 'getLoggedInUser').mockResolvedValue(state.user.name);
    const { fireEvent } = await import('@testing-library/react');
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: state.user.name } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'test password' } });
    fireEvent.submit(screen.getByLabelText('Password').closest('form')!);
    await waitFor(() => expect(core.auth.getLoggedInUser).toHaveBeenCalled());
    // A second transaction under the shared login lock waits for sign-in cleanup to commit.
    const { withEmptyOutbox } = await import('../src/offline/outbox');
    await withEmptyOutbox(async () => {});
  }
  cleanup();
  sessionStorage.clear();
  render(<AuthGuard><Workspace /></AuthGuard>);
  await screen.findByText('Workspace');
});

it('fails closed on a durable-store read error even with a confirmed tab', async () => {
  setFloorSessionMarker(state.user.name, false);
  vi.spyOn(indexedDB, 'open').mockImplementation(() => { throw new Error('Storage unavailable'); });
  const Workspace = vi.fn(() => <main>Workspace</main>);
  render(<AuthGuard><Workspace /></AuthGuard>);
  await screen.findByLabelText('Password');
  expect(Workspace).not.toHaveBeenCalled();
});

it('locks a table route without an outbox hook after a missed membership notification', async () => {
  const timers = vi.spyOn(window, 'setInterval');
  const stop = vi.spyOn(window, 'clearInterval');
  const view = render(<AuthGuard><main>Table T1</main></AuthGuard>);
  await screen.findByText('Table T1');
  const dispatch = vi.spyOn(window, 'dispatchEvent').mockReturnValue(true);
  try { await capture(); } finally { dispatch.mockRestore(); }
  await act(async () => {
    for (const [wake, ms] of timers.mock.calls) if (ms === 5000) await (wake as () => void)();
  });
  await screen.findByText(/account that owns the queued orders/);
  expect(screen.queryByText('Table T1')).toBeNull();
  view.unmount();
  timers.mock.calls.forEach(([, ms], i) => {
    if (ms === 5000) expect(stop).toHaveBeenCalledWith(timers.mock.results[i].value);
  });
});

it('broadcasts a logout across tabs when the storage signal is unavailable', async () => {
  vi.stubGlobal('BroadcastChannel', NodeChannel);
  vi.spyOn(core, 'logout').mockResolvedValue(false);
  const sibling = new NodeChannel('ury-floor-logout');
  const received = vi.fn();
  sibling.onmessage = received;
  try {
    await expect(clearCashierStorage(true, state.user.name)).rejects.toThrow();
    await waitFor(() => expect(received).toHaveBeenCalled());
  } finally { sibling.close(); }
});

it.each(['broadcast', 'poll', 'focus', 'visibility'])('revokes a mounted sibling confirmation on %s', async (signal) => {
  if (signal === 'broadcast') vi.stubGlobal('BroadcastChannel', NodeChannel);
  const timers = vi.spyOn(window, 'setInterval');
  vi.spyOn(core, 'logout').mockResolvedValue(false);
  setFloorSessionMarker(state.user.name, false);
  render(<AuthGuard><main>Sibling workspace</main></AuthGuard>);
  await screen.findByText('Sibling workspace');
  // Only the origin-wide record is shared with the tab that attempted logout.
  await writeLogoutMarker('remote-owner');
  expect(sessionStorage.getItem('ury_floor_logout_failed')).toBeNull();
  await act(async () => {
    if (signal === 'broadcast') {
      const remote = new NodeChannel('ury-floor-logout');
      remote.postMessage(null);
      remote.close();
    } else if (signal === 'poll') {
      for (const [wake, ms] of timers.mock.calls) if (ms === 5000) await (wake as () => void)();
    } else if (signal === 'focus') window.dispatchEvent(new Event('focus'));
    else document.dispatchEvent(new Event('visibilitychange'));
  });
  await screen.findByLabelText('Password');
  expect(screen.queryByText('Sibling workspace')).toBeNull();
  expect(hasFloorSessionMarker(state.user.name)).toBe(false);
});

function refuseMarkerTransactions(mode: IDBTransactionMode) {
  const original = IDBDatabase.prototype.transaction;
  return vi.spyOn(IDBDatabase.prototype, 'transaction').mockImplementation(function (this: IDBDatabase, ...args) {
    if (this.name === 'ury-pos-session' && args[1] === mode) throw new DOMException('Storage unavailable', 'QuotaExceededError');
    return original.apply(this, args);
  });
}

it('confirms a verified password login with a cookie-only marker and a read-only empty durable store', async () => {
  expect(await readLogoutMarker()).toBe(false);
  refuseMarkerTransactions('readwrite');
  document.cookie = 'ury_floor_logout_failed=1; Path=/pos; SameSite=Strict; Secure';
  vi.spyOn(core.auth, 'loginWithUsernamePassword').mockResolvedValue({ message: 'Logged In' } as never);
  vi.spyOn(core.auth, 'getLoggedInUser').mockResolvedValue(state.user.name);
  render(<FloorPinPad usernameOnly />);
  fireEvent.change(screen.getByLabelText('Username'), { target: { value: state.user.name } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'test password' } });
  fireEvent.submit(screen.getByLabelText('Password').closest('form')!);
  await waitFor(() => expect(hasFloorSessionMarker(state.user.name)).toBe(true));
  expect(document.cookie).not.toContain('ury_floor_logout_failed=');
});

it.each(['readonly', 'readwrite'] as const)('keeps the login gate closed when a present marker cannot be checked or cleared (%s)', async mode => {
  await writeLogoutMarker(state.user.name);
  refuseMarkerTransactions(mode);
  document.cookie = 'ury_floor_logout_failed=1; Path=/pos; SameSite=Strict; Secure';
  await expect(clearFloorLogoutFailureMarker()).rejects.toThrow();
  expect(document.cookie).toContain('ury_floor_logout_failed=1');
  expect(hasFloorSessionMarker(state.user.name)).toBe(false);
});

it('sets and expires the fallback cookie with Secure on HTTPS', async () => {
  expect(location.protocol).toBe('https:');
  expect(await readLogoutMarker()).toBe(false);
  const blocked = refuseMarkerTransactions('readwrite');
  const cookies = vi.spyOn(document, 'cookie', 'set');
  vi.spyOn(core, 'logout').mockResolvedValue(false);
  await expect(clearCashierStorage(true, state.user.name)).rejects.toThrow();
  blocked.mockRestore();
  await clearFloorLogoutFailureMarker();
  for (const prefix of ['ury_floor_logout_failed=1;', 'ury_floor_logout_failed=;']) {
    const value = cookies.mock.calls.find(([cookie]) => cookie.startsWith(prefix))?.[0];
    expect(value).toContain('Path=/pos');
    expect(value).toContain('SameSite=Strict');
    expect(value?.toLowerCase().split(';').map(attribute => attribute.trim())).toContain('secure');
  }
});

async function passwordLogin(user: string) {
  vi.spyOn(core.auth, 'loginWithUsernamePassword').mockResolvedValue({ message: 'Logged In' } as never);
  vi.spyOn(core.auth, 'getLoggedInUser').mockResolvedValue(user);
  const form = render(<FloorPinPad usernameOnly />);
  fireEvent.change(form.getByLabelText('Username'), { target: { value: user } });
  fireEvent.change(form.getByLabelText('Password'), { target: { value: 'test password' } });
  fireEvent.submit(form.getByLabelText('Password').closest('form')!);
  await waitFor(() => expect(hasFloorSessionMarker(user)).toBe(true));
  form.unmount();
}

it.each(['logout broadcast', 'login broadcast', 'poll', 'focus', 'visibility', 'cookie-only focus'])(
  'locks a suspended cashier on %s after another tab has cleared the logout failure by signing in',
  async signal => {
    if (signal === 'cookie-only focus') {
      await readLogoutMarker(); // Create the database before making it read-only.
      refuseMarkerTransactions('readwrite');
    }
    const messages: MessageEvent[] = [];
    let receive: ((event: MessageEvent) => void) | undefined;
    class SuspendedChannel {
      onmessage?: (event: MessageEvent) => void;
      constructor(readonly name: string) {
        if (name === 'ury-floor-logout') receive ??= event => this.onmessage?.(event);
      }
      postMessage(data: unknown) {
        if (this.name === 'ury-floor-logout') messages.push(new MessageEvent('message', { data }));
      }
      close() {}
    }
    await passwordLogin(state.user.name);
    const cashierTab = window.name;
    const confirmation = sessionStorage.getItem('userAuth')!;
    vi.stubGlobal('BroadcastChannel', SuspendedChannel);
    const timers = vi.spyOn(window, 'setInterval');
    const view = render(<AuthGuard><main>Suspended cashier workspace</main></AuthGuard>);
    await screen.findByText('Suspended cashier workspace');

    // The suspended window retains its private storage while A and B share only origin storage.
    window.name = '';
    sessionStorage.clear();
    const otherTab = render(<AuthGuard><main>Other cashier workspace</main></AuthGuard>);
    await screen.findByText('Other cashier workspace');
    otherTab.unmount();
    const logout = vi.spyOn(core, 'logout').mockResolvedValue(false);
    await expect(clearCashierStorage(true, state.user.name)).rejects.toThrow();
    const logoutEvent = messages.at(-1);
    messages.length = 0;
    window.name = '';
    sessionStorage.clear();
    await passwordLogin('replacement@example.test');
    const loginEvent = messages.at(-1);
    expect(await readLogoutMarker()).toBe(false);
    expect(document.cookie).not.toContain('ury_floor_logout_failed=');
    window.name = cashierTab;
    sessionStorage.clear();
    sessionStorage.setItem('userAuth', confirmation);

    await act(async () => {
      if (signal.endsWith('broadcast')) {
        const event = signal === 'login broadcast' ? loginEvent : logoutEvent;
        expect(event).toBeDefined();
        receive!(event!);
      } else if (signal === 'poll') {
        for (const [wake, ms] of timers.mock.calls) if (ms === 5000) await (wake as () => void)();
      } else if (signal === 'visibility') document.dispatchEvent(new Event('visibilitychange'));
      else window.dispatchEvent(new Event('focus'));
    });
    await screen.findByLabelText('Password');
    expect(screen.queryByText('Suspended cashier workspace')).toBeNull();
    expect(hasFloorSessionMarker(state.user.name)).toBe(false);
    expect(logout).toHaveBeenCalledTimes(1);
    view.unmount();
    render(<AuthGuard><main>Suspended cashier workspace</main></AuthGuard>);
    await screen.findByLabelText('Password');
    expect(screen.queryByText('Suspended cashier workspace')).toBeNull();
  },
);

it('rotates the persistent generation on logout and verified login and binds confirmation to it', async () => {
  vi.restoreAllMocks(); // All three origin stores accept writes in this case.
  const cookie = vi.spyOn(document, 'cookie', 'set');
  const generation = () => localStorage.getItem('ury_floor_generation');
  await passwordLogin(state.user.name);
  const first = generation();
  expect(first).toBeTruthy();
  expect(String(JSON.parse(sessionStorage.getItem('userAuth')!).generation)).toBe(first);
  vi.spyOn(core, 'logout').mockResolvedValue(false);
  await expect(clearCashierStorage(true, state.user.name)).rejects.toThrow();
  const revoked = generation();
  expect(revoked).toBeTruthy();
  expect(revoked).not.toBe(first);
  await passwordLogin('replacement@example.test');
  const next = generation();
  expect(next).toBeTruthy();
  expect(next).not.toBe(revoked);
  expect(String(JSON.parse(sessionStorage.getItem('userAuth')!).generation)).toBe(next);
  expect(document.cookie).toContain('ury_floor_generation=' + next);
  for (const value of [first, revoked, next]) {
    const attributes = cookie.mock.calls.find(([line]) => line.startsWith('ury_floor_generation=' + value + ';'))?.[0];
    expect(attributes).toContain('Path=/pos');
    expect(attributes).toContain('SameSite=Strict');
    expect(attributes).toMatch(/; Secure/i);
  }
  // Removing just the two mirrors still leaves the latest generation in IndexedDB.
  localStorage.removeItem('ury_floor_generation');
  document.cookie = 'ury_floor_generation=; Path=/pos; Max-Age=0; SameSite=Strict; Secure';
  const record = JSON.parse(sessionStorage.getItem('userAuth')!);
  sessionStorage.setItem('userAuth', JSON.stringify({ ...record, user: state.user.name, generation: first }));
  render(<AuthGuard><main>Old workspace</main></AuthGuard>);
  await screen.findByLabelText('Password');
  expect(screen.queryByText('Old workspace')).toBeNull();
});

function saveTab() {
  const name = window.name;
  const entries = Object.entries(sessionStorage);
  return () => {
    window.name = name;
    sessionStorage.clear();
    for (const [key, value] of entries) sessionStorage.setItem(key, value);
  };
}

it.each(['quota still full', 'quota recovered', 'retry already scheduled'])(
  'keeps a replacement cashier signed in when the failed tab returns (%s)', async scenario => {
    await passwordLogin(state.user.name);
    const logout = vi.spyOn(core, 'logout').mockResolvedValueOnce(false).mockResolvedValue(true);
    await expect(clearCashierStorage(true, state.user.name)).rejects.toThrow();
    expect(sessionStorage.getItem('ury_floor_logout_failed')).toBeTruthy();
    const restoreFailedTab = saveTab();

    window.name = '';
    sessionStorage.clear();
    const replacement = 'replacement@example.test';
    await passwordLogin(replacement);
    const restoreReplacementTab = saveTab();
    const replacementGeneration = await currentFloorGeneration();
    expect(await readLogoutMarker()).toBe(false);

    restoreFailedTab();
    if (scenario === 'quota recovered') vi.mocked(Storage.prototype.setItem).mockRestore();
    if (scenario === 'retry already scheduled') await floorSignOut(() => true, true);
    const previous = state.user;
    state.user = { ...previous, name: replacement }; // Both tabs now see B's cookie.
    try {
      const showGuard = () => render(<AuthGuard><main>Cashier workspace</main></AuthGuard>);
      const gate = showGuard();
      await screen.findByLabelText('Password');
      expect(logout).toHaveBeenCalledTimes(1);
      expect(screen.queryByText('Cashier workspace')).toBeNull();
      expect(sessionStorage.getItem('ury_floor_logout_failed')).toBeNull();
      expect(localStorage.getItem('ury_floor_logout_failed')).toBeNull();
      expect(await currentFloorGeneration()).toBe(replacementGeneration);
      gate.unmount();

      const reopened = showGuard();
      await screen.findByLabelText('Password');
      reopened.unmount();
      restoreReplacementTab();
      showGuard();
      await screen.findByText('Cashier workspace');
      expect(hasFloorSessionMarker(replacement)).toBe(true);
      expect(logout).toHaveBeenCalledTimes(1);
    } finally { state.user = previous; }
  },
);

it('binds a private logout retry to its generation and retries while it matches', async () => {
  const logout = vi.spyOn(core, 'logout').mockResolvedValue(false);
  await expect(clearCashierStorage(true, state.user.name)).rejects.toThrow();
  expect(sessionStorage.getItem('ury_floor_logout_failed')).toBe(await currentFloorGeneration());
  render(<AuthGuard><main>Cashier workspace</main></AuthGuard>);
  await screen.findByLabelText('Password');
  expect(logout).toHaveBeenCalledTimes(2);
  expect(sessionStorage.getItem('ury_floor_logout_failed')).toBe(await currentFloorGeneration());
});

it('withholds a scheduled private retry when the durable generation cannot be read', async () => {
  const logout = vi.spyOn(core, 'logout').mockResolvedValue(false);
  await expect(clearCashierStorage(true, state.user.name)).rejects.toThrow();
  refuseMarkerTransactions('readonly');
  await floorSignOut(() => true, true);
  expect(logout).toHaveBeenCalledTimes(1);
  render(<AuthGuard><main>Cashier workspace</main></AuthGuard>);
  await screen.findByLabelText('Password');
  expect(screen.queryByText('Cashier workspace')).toBeNull();
});

it.each([
  ['confirmed', 'Switch Waiter'], ['confirmed', 'till logout'],
  ['initial stay-role', 'till logout'], ['existing stay-role', 'Switch Waiter'],
  ['lost stay-role snapshot', 'till logout'], ['malformed stay-role snapshot', 'till logout'],
])('refuses %s workspace %s queued behind a replacement login', async (admission, action) => {
  if (admission === 'confirmed') {
    await passwordLogin(state.user.name);
  } else {
    vi.stubGlobal('floor_pad', { paired: true, till_open: true });
    if (admission === 'existing stay-role') await logoutStore.writeGeneration('0000000000000001:previous');
    const workspace = render(<AuthGuard><main>Stay-role workspace</main></AuthGuard>);
    await screen.findByText('Stay-role workspace');
    expect(isFloorSessionConfirmedForTab(state.user.name)).toBe(false);
    if (admission === 'lost stay-role snapshot') sessionStorage.clear();
    if (admission === 'malformed stay-role snapshot') sessionStorage.setItem('ury_floor_observed_generation', '{');
    workspace.unmount();
  }
  const restoreA = saveTab();
  const generationA = await currentFloorGeneration();
  expect(sessionStorage.getItem('ury_floor_logout_failed')).toBeNull();

  const logout = vi.spyOn(core, 'logout').mockResolvedValue(true);
  const markerWrites = vi.spyOn(logoutStore, 'writeLogoutMarker');
  const generationWrites = vi.spyOn(logoutStore, 'writeGeneration');
  const lockRequests = vi.spyOn(navigator.locks, 'request');
  let resumeLogin!: () => void;
  const paused = new Promise<void>(resolve => { resumeLogin = resolve; });
  let loginHasLock!: () => void;
  const acquired = new Promise<void>(resolve => { loginHasLock = resolve; });
  const cashierB = 'replacement@example.test';
  let generationB!: string;
  let restoreB!: () => void;
  const login = withOutboxLogin(cashierB, async () => {
    loginHasLock();
    await paused;
    window.name = '';
    sessionStorage.clear();
    await confirmVerifiedLogin(cashierB, false, await beginFloorLogin());
    generationB = await currentFloorGeneration();
    restoreB = saveTab();
    restoreA(); // The queued operation runs with A's private storage.
  });
  await acquired;
  const attempt = action === 'Switch Waiter' ? floorSignOut() : clearCashierStorage(true, state.user.name);
  const completed = Promise.allSettled([attempt]);
  expect(lockRequests).toHaveBeenCalledTimes(2);
  expect(lockRequests.mock.calls[0][0]).toBe(lockRequests.mock.calls[1][0]);
  resumeLogin();
  await login;
  await completed;
  expect(generationB).not.toBe(generationA);
  expect(logout).not.toHaveBeenCalled();
  expect(markerWrites).not.toHaveBeenCalled();
  expect(generationWrites.mock.calls).toEqual([[generationB]]); // Only the verified login rotated it.
  expect(await currentFloorGeneration()).toBe(generationB);
  expect(hasFloorSessionMarker(state.user.name)).toBe(false);
  const gate = render(<AuthGuard><main>Stale workspace</main></AuthGuard>);
  await screen.findByLabelText('Password');
  gate.unmount();
  restoreB();
  const previous = state.user;
  state.user = { ...previous, name: cashierB };
  try {
    render(<AuthGuard><main>Replacement workspace</main></AuthGuard>);
    await screen.findByText('Replacement workspace');
  } finally { state.user = previous; }
  expect(hasFloorSessionMarker(cashierB)).toBe(true);
  expect(logout).not.toHaveBeenCalled();
});

it('locks a confirmed tab on logout if its non-empty generation cannot be verified', async () => {
  await passwordLogin(state.user.name);
  const generation = await currentFloorGeneration();
  const logout = vi.spyOn(core, 'logout').mockResolvedValue(true);
  const markerWrites = vi.spyOn(logoutStore, 'writeLogoutMarker');
  const blocked = refuseMarkerTransactions('readonly');
  await floorSignOut();
  blocked.mockRestore();
  expect(logout).not.toHaveBeenCalled();
  expect(markerWrites).not.toHaveBeenCalled();
  expect(await currentFloorGeneration()).toBe(generation);
  render(<AuthGuard><main>Stale workspace</main></AuthGuard>);
  await screen.findByLabelText('Password');
  expect(hasFloorSessionMarker(state.user.name)).toBe(false);
});

it.each(['till logout', 'Switch Waiter'])('locks locally on %s during replacement login without Web Locks', async action => {
  vi.stubGlobal('navigator', { onLine: true, locks: undefined });
  await passwordLogin(state.user.name);
  const logout = vi.spyOn(core, 'logout').mockResolvedValue(true);
  const generationWrites = vi.spyOn(logoutStore, 'writeGeneration');

  // If A starts a durable write, suspend it there until B has replaced the shared session.
  let reachedWrite!: () => void;
  const writing = new Promise<void>(resolve => { reachedWrite = resolve; });
  let resumeLogout!: () => void;
  const paused = new Promise<void>(resolve => { resumeLogout = resolve; });
  const writeMarker = logoutStore.writeLogoutMarker;
  const markerWrites = vi.spyOn(logoutStore, 'writeLogoutMarker').mockImplementationOnce(async owner => {
    reachedWrite();
    await paused;
    await writeMarker(owner);
  });

  const attempt = action === 'till logout' ? clearCashierStorage(true, state.user.name) : floorSignOut();
  const finished = Promise.allSettled([attempt]);
  await Promise.race([writing, finished]);
  const restoreA = saveTab();
  const cashierB = 'replacement@example.test';
  window.name = '';
  sessionStorage.clear();
  let restoreB!: () => void;
  let generationB!: string;
  try {
    await passwordLogin(cashierB);
    generationB = await currentFloorGeneration();
    restoreB = saveTab();
  } finally {
    restoreA();
    resumeLogout();
    await finished;
  }
  expect(logout).not.toHaveBeenCalled();
  expect(markerWrites).not.toHaveBeenCalled();
  expect(generationWrites.mock.calls).toEqual([[generationB]]); // B's verified login was the only rotation.
  expect(await currentFloorGeneration()).toBe(generationB);
  expect(isFloorSessionConfirmedForTab(state.user.name)).toBe(false);
  const gate = render(<AuthGuard><main>Old workspace</main></AuthGuard>);
  await screen.findByLabelText('Password');
  expect(screen.queryByText('Old workspace')).toBeNull();
  gate.unmount();

  restoreB();
  const previous = state.user;
  state.user = { ...previous, name: cashierB };
  try {
    render(<AuthGuard><main>Replacement workspace</main></AuthGuard>);
    await screen.findByText('Replacement workspace');
    expect(hasFloorSessionMarker(cashierB)).toBe(true);
    expect(logout).not.toHaveBeenCalled();
  } finally { state.user = previous; }
});

it('locks a private retry without Web Locks until verified sign-in', async () => {
  await passwordLogin(state.user.name);
  const logout = vi.spyOn(core, 'logout').mockResolvedValueOnce(false).mockResolvedValue(true);
  await expect(clearCashierStorage(true, state.user.name)).rejects.toThrow();
  expect(logout).toHaveBeenCalledTimes(1);
  const generation = await currentFloorGeneration();
  expect(sessionStorage.getItem('ury_floor_logout_failed')).toBe(generation);

  // The retry reopens in an environment without the lock used for the original attempt.
  vi.stubGlobal('navigator', { onLine: true, locks: undefined });
  const markerWrites = vi.spyOn(logoutStore, 'writeLogoutMarker');
  const showGuard = () => render(<AuthGuard><main>Cashier workspace</main></AuthGuard>);
  for (let boot = 0; boot < 2; boot++) {
    const gate = showGuard();
    await screen.findByLabelText('Password');
    expect(screen.queryByText('Cashier workspace')).toBeNull();
    expect(logout).toHaveBeenCalledTimes(1);
    expect(markerWrites).not.toHaveBeenCalled();
    expect(await currentFloorGeneration()).toBe(generation);
    gate.unmount();
  }
  await passwordLogin(state.user.name);
  showGuard();
  await screen.findByText('Cashier workspace');
  expect(logout).toHaveBeenCalledTimes(1);
});

it.each(['', '0000000000000001:previous'])('permits stay-role logout at its unchanged admission generation (%s)', async generation => {
  vi.stubGlobal('floor_pad', { paired: true, till_open: true });
  if (generation) await logoutStore.writeGeneration(generation);
  render(<AuthGuard><main>Stay-role workspace</main></AuthGuard>);
  await screen.findByText('Stay-role workspace');
  expect(isFloorSessionConfirmedForTab(state.user.name)).toBe(false);
  const logout = vi.spyOn(core, 'logout').mockResolvedValue(true);
  await clearCashierStorage(true, state.user.name);
  expect(logout).toHaveBeenCalledOnce();
});

it('invalidates the prior generation before replacing the cookie even if login verification disconnects', async () => {
  await passwordLogin(state.user.name);
  const restoreA = saveTab();
  const previous = await currentFloorGeneration();
  window.name = '';
  sessionStorage.clear();
  const cashierB = 'replacement@example.test';
  let cookieUser = state.user.name;
  let generationAtRequest: string | undefined;
  vi.spyOn(core.auth, 'loginWithUsernamePassword').mockImplementationOnce(async () => {
    generationAtRequest = await currentFloorGeneration();
    cookieUser = cashierB;
    return { message: 'Logged In' } as never;
  });
  let disconnect!: (reason: Error) => void;
  const verify = vi.spyOn(core.auth, 'getLoggedInUser').mockImplementationOnce(() =>
    new Promise<string>((_, reject) => { disconnect = reject; }));
  verify.mockClear();
  const logout = vi.spyOn(core, 'logout').mockResolvedValue(true);
  render(<FloorPinPad usernameOnly />);
  fireEvent.change(screen.getByLabelText('Username'), { target: { value: cashierB } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'test password' } });
  fireEvent.submit(screen.getByLabelText('Password').closest('form')!);
  await waitFor(() => expect(verify).toHaveBeenCalledOnce());
  expect(cookieUser).toBe(cashierB);
  restoreA();
  const queuedLogout = floorSignOut();
  await act(async () => {
    disconnect(new Error('Connection lost after Set-Cookie'));
    await queuedLogout;
  });
  expect(logout).not.toHaveBeenCalled();
  expect(generationAtRequest).not.toBe(previous);
  expect(await currentFloorGeneration()).toBe(generationAtRequest);
  expect(hasFloorSessionMarker(state.user.name)).toBe(false);
});

it('unmounts the stay-role workspace and keeps the tab locked when both storage quotas are full', async () => {
  vi.stubGlobal('navigator', { onLine: true });
  const mount = () => render(<AuthGuard><main>Stay-role workspace<TillLogout /></main></AuthGuard>);
  const view = mount();
  await screen.findByText('Stay-role workspace');
  expect(sessionStorage.getItem('userAuth')).toBeNull();
  const logout = vi.spyOn(core, 'logout').mockResolvedValue(true);
  vi.mocked(Storage.prototype.setItem).mockImplementation(() => {
    throw new DOMException('Full', 'QuotaExceededError');
  });
  fireEvent.click(screen.getByRole('button'));
  await screen.findByLabelText('Password');
  expect(screen.queryByText('Stay-role workspace')).toBeNull();
  view.unmount();
  // The storage quota can recover before navigation finishes and the guard boots again.
  vi.mocked(Storage.prototype.setItem).mockRestore();
  mount();
  await screen.findByLabelText('Password');
  expect(screen.queryByText('Stay-role workspace')).toBeNull();
  expect(logout).not.toHaveBeenCalled();
});

it('locks the mounted replacement workspace when an earlier unlocked login response displaces its cookie', async () => {
  vi.stubGlobal('navigator', { onLine: true });
  let cookieUser = 'Guest';
  const first = 'first@example.test';
  const second = state.user.name;
  let finishFirst!: () => void;
  vi.spyOn(core.auth, 'getLoggedInUser').mockImplementation(async () => cookieUser);
  const login = vi.spyOn(core.auth, 'loginWithUsernamePassword')
    .mockImplementationOnce(() => new Promise(resolve => {
      finishFirst = () => {
        cookieUser = first;
        resolve({ message: 'Logged In', full_name: 'First' });
      };
    })).mockImplementationOnce(async () => {
      cookieUser = second;
      return { message: 'Logged In', full_name: 'Second' };
    });
  function start(user: string) {
    const view = render(<FloorPinPad usernameOnly />);
    const form = view.container.querySelector('form')!;
    fireEvent.change(form.querySelector('input[name=user_id]')!, { target: { value: user } });
    fireEvent.change(form.querySelector('input[type=password]')!, { target: { value: 'test password' } });
    fireEvent.submit(form);
    return view;
  }
  const a = start(first);
  await waitFor(() => expect(login).toHaveBeenCalledTimes(1));
  const restoreA = saveTab();
  window.name = '';
  sessionStorage.clear();
  const b = start(second);
  await waitFor(() => expect(hasFloorSessionMarker(second)).toBe(true));
  b.unmount();
  const restoreB = saveTab();
  const secondGeneration = await currentFloorGeneration();

  // Queue messages until the test switches the jsdom browsing context back to B.
  const messages: unknown[] = [];
  let listener: ((event: MessageEvent) => void) | undefined;
  class Channel {
    onmessage?: (event: MessageEvent) => void;
    constructor(readonly name: string) {
      if (name === 'ury-floor-logout') listener ??= event => this.onmessage?.(event);
    }
    postMessage(data: unknown) { if (this.name === 'ury-floor-logout') messages.push(data); }
    close() {}
  }
  vi.stubGlobal('BroadcastChannel', Channel);
  const workspace = render(<AuthGuard><button>Submit cashier order</button></AuthGuard>);
  await within(workspace.container).findByRole('button', { name: 'Submit cashier order' });
  restoreA();
  await act(async () => finishFirst());
  await within(a.container).findByRole('alert');
  expect(cookieUser).toBe(first);
  expect(hasFloorSessionMarker(first)).toBe(false);
  a.unmount();
  restoreB();
  act(() => { for (const data of messages) listener!(new MessageEvent('message', { data })); });
  expect(await currentFloorGeneration()).not.toBe(secondGeneration);
  await within(workspace.container).findByLabelText('Password');
  expect(screen.queryByRole('button', { name: 'Submit cashier order' })).toBeNull();
  expect(hasFloorSessionMarker(second)).toBe(false);
});

it.each(['order SDK', 'direct fetch'])('blocks the admitted %s request before the competing login body arrives', async transport => {
  vi.stubGlobal('navigator', { onLine: true });
  const owner = state.user.name;
  const first = 'earlier@example.test';
  let resolveLogin!: () => void;
  const delayed = vi.spyOn(core.auth, 'loginWithUsernamePassword').mockImplementationOnce(() =>
    new Promise(resolve => { resolveLogin = () => resolve({ message: 'Logged In' }); }));
  vi.spyOn(core.auth, 'getLoggedInUser').mockResolvedValue(first);
  const a = render(<FloorPinPad usernameOnly />);
  fireEvent.change(screen.getByLabelText('Username'), { target: { value: first } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'test password' } });
  fireEvent.submit(screen.getByLabelText('Password').closest('form')!);
  await waitFor(() => expect(delayed).toHaveBeenCalledOnce());
  a.unmount();
  sessionStorage.clear();
  window.name = '';
  document.cookie = 'user_id=' + encodeURIComponent(owner) + '; Path=/';
  await confirmVerifiedLogin(owner, false, await beginFloorLogin());
  const view = render(<AuthGuard><main>Cashier order screen</main></AuthGuard>);
  await screen.findByText('Cashier order screen');
  const axios = (core.auth as unknown as { axios: AxiosInstance }).axios;
  const previous = axios.defaults.adapter;
  const sent: Headers[] = [];
  axios.defaults.adapter = async config => {
    sent.push(new Headers(config.headers.toJSON() as Record<string, string>));
    return { config, data: { message: null }, status: 200, statusText: 'OK', headers: {} };
  };
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    sent.push(new Headers(init?.headers));
    return new Response(JSON.stringify({ message: {} }));
  }));
  const request = () => transport === 'order SDK' ? getTableOrder('T1', true) : getRealtimeSocket();
  try {
    await Promise.allSettled([request()]);
    const matched = sent.map(h => h.get('X-URY-Floor-User'));
    sent.length = 0;
    document.cookie = 'user_id=' + encodeURIComponent(first) + '; Path=/';
    await act(async () => { await Promise.allSettled([request()]); });
    expect(sent).toHaveLength(0);
    expect(matched).toEqual([owner]);
    await screen.findByLabelText('Password');
    expect(screen.queryByText('Cashier order screen')).toBeNull();
    expect(hasFloorSessionMarker(owner)).toBe(false);
  } finally {
    view.unmount();
    document.cookie = 'user_id=; Max-Age=0; Path=/';
    await act(async () => resolveLogin());
  }
});

it.each([
  ['floor-waiter', true], ['+256700001234', true], ['another-waiter', false],
] as const)('checks the canonical owner of pending orders after alias %s authenticates', async (alias, matches) => {
  const entry = await capture();
  const canonical = matches ? state.user.name : 'other@example.test';
  const login = vi.spyOn(core.auth, 'loginWithUsernamePassword').mockResolvedValue({
    message: 'Logged In', authenticated_user: canonical,
  } as never);
  vi.spyOn(core.auth, 'getLoggedInUser').mockResolvedValue(canonical);
  render(<FloorPinPad usernameOnly recoveryOwner={state.user.name} />);
  fireEvent.change(screen.getByLabelText('Username'), { target: { value: alias } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'test password' } });
  fireEvent.submit(screen.getByLabelText('Password').closest('form')!);
  await waitFor(() => expect(login).toHaveBeenCalledOnce());
  if (matches) {
    await waitFor(() => expect(isFloorSessionConfirmedForTab(state.user.name)).toBe(true));
    expect(hasFloorSessionMarker(state.user.name)).toBe(false);
  } else {
    await screen.findByRole('alert');
    expect(isFloorSessionConfirmedForTab(canonical)).toBe(false);
    expect(sessionStorage.getItem('userAuth')).toBe('revoked');
  }
  expect((await cashierOutbox(state.posProfile, state.user).list()).map(row => row.client_order_id)).toEqual([entry.client_order_id]);
});
