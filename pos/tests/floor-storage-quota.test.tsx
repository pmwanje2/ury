/** @vitest-environment-options {"url":"https://register.test/pos/order/table/T1"} */
import { BroadcastChannel as NodeChannel } from 'node:worker_threads';
import { IDBDatabase, IDBFactory } from 'fake-indexeddb';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import * as core from '@ury/core';
import AuthGuard from '../src/components/AuthGuard';
import OutboxPanel from '../src/components/OutboxPanel';
import { cashierOutbox, clearCashierStorage } from '../src/offline/cashier';
import { onOutboxMembershipChange, withOutboxLogin } from '../src/offline/outbox';
import { useOutbox } from '../src/offline/use-outbox';
import { clearFloorLogoutFailureMarker, confirmVerifiedLogin, currentFloorGeneration, floorSignOut, hasFloorSessionMarker, setFloorSessionMarker } from '../src/lib/floor-session';
import * as logoutStore from '../src/lib/logout-store';
import { readLogoutMarker, writeLogoutMarker } from '../src/lib/logout-store';
import { isFloorSessionConfirmedForTab } from '../src/lib/floor-tab';
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
  await expect(clearCashierStorage(true)).rejects.toThrow();
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
    await clearCashierStorage(true);
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
    await expect(clearCashierStorage(true)).rejects.toThrow();
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
  await expect(clearCashierStorage(true)).rejects.toThrow();
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
    await expect(clearCashierStorage(true)).rejects.toThrow();
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
  await expect(clearCashierStorage(true)).rejects.toThrow();
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
    await expect(clearCashierStorage(true)).rejects.toThrow();
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
  await expect(clearCashierStorage(true)).rejects.toThrow();
  expect(sessionStorage.getItem('ury_floor_logout_failed')).toBe(await currentFloorGeneration());
  render(<AuthGuard><main>Cashier workspace</main></AuthGuard>);
  await screen.findByLabelText('Password');
  expect(logout).toHaveBeenCalledTimes(2);
  expect(sessionStorage.getItem('ury_floor_logout_failed')).toBe(await currentFloorGeneration());
});

it('withholds a scheduled private retry when the durable generation cannot be read', async () => {
  const logout = vi.spyOn(core, 'logout').mockResolvedValue(false);
  await expect(clearCashierStorage(true)).rejects.toThrow();
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
    await confirmVerifiedLogin(cashierB, false);
    generationB = await currentFloorGeneration();
    restoreB = saveTab();
    restoreA(); // The queued operation runs with A's private storage.
  });
  await acquired;
  const attempt = action === 'Switch Waiter' ? floorSignOut() : clearCashierStorage(true);
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

it('locks a private retry without Web Locks until verified sign-in', async () => {
  vi.stubGlobal('navigator', { onLine: true, locks: undefined });
  await passwordLogin(state.user.name);
  const logout = vi.spyOn(core, 'logout').mockResolvedValueOnce(false).mockResolvedValue(true);
  // The first explicit till logout is allowed without Web Locks.
  await expect(clearCashierStorage(true)).rejects.toThrow();
  expect(logout).toHaveBeenCalledTimes(1);
  const generation = await currentFloorGeneration();
  expect(sessionStorage.getItem('ury_floor_logout_failed')).toBe(generation);
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
  await clearCashierStorage(true);
  expect(logout).toHaveBeenCalledOnce();
});
