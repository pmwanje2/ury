import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initI18n } from '../src/i18n';
import AuthGuard from '../src/components/AuthGuard';

const boundary = vi.hoisted(() => ({
  callPost: vi.fn(),
  login: vi.fn(),
  logoutSession: vi.fn(),
  clearAppStorage: vi.fn(),
  disconnectQzPrinter: vi.fn(),
  state: {
    user: null as null | { name: string; roles: string[]; provisional?: boolean },
    isLoading: false,
    error: null as string | null,
    posProfile: null as Record<string, unknown> | null,
    hasAccess: true,
    fetchPosProfile: vi.fn(),
    checkAuth: vi.fn(),
  },
}));

vi.mock('@ury/core', () => ({
  call: { post: boundary.callPost },
  auth: { loginWithUsernamePassword: boundary.login },
  logoutSession: boundary.logoutSession,
  clearAppStorage: boundary.clearAppStorage,
  disconnectQzPrinter: boundary.disconnectQzPrinter,
  resolveBrand: () => ({ name: 'URY POS', logo: '/assets/ury/pos/ury_pos.png' }),
  parseFrappeError: (error: unknown) => String((error as { message?: unknown }).message || error),
}));

vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector?: (state: typeof boundary.state) => unknown) =>
    selector ? selector(boundary.state) : boundary.state,
}));

let container: HTMLDivElement;
let root: Root;

function setFloorPad(value: { paired: boolean; till_open: boolean }) {
  Object.defineProperty(window, 'floor_pad', { configurable: true, value });
}

async function render() {
  await act(async () => root.render(
    <AuthGuard><main data-testid="captain-shell">Captain shell</main></AuthGuard>,
  ));
}

function digit(value: string): HTMLButtonElement {
  const button = [...container.querySelectorAll<HTMLButtonElement>('button')]
    .find((candidate) => candidate.textContent?.trim() === value);
  expect(button, `PIN digit ${value} should be rendered`).toBeDefined();
  return button!;
}

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  boundary.state.user = null;
  boundary.state.posProfile = null;
  boundary.state.hasAccess = true;
  boundary.logoutSession.mockResolvedValue(undefined);
  boundary.clearAppStorage.mockResolvedValue(undefined);
  await initI18n('en');
  setFloorPad({ paired: true, till_open: true });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  delete (window as Window & { floor_pad?: unknown }).floor_pad;
  vi.unstubAllGlobals();
});

describe('paired floor tablet sign-in', () => {
  it('shows the PIN pad without redirecting a Guest to native /login', async () => {
    await render();
    expect(container.textContent).toContain('Staff sign-in');
    expect(container.textContent).not.toContain('Captain shell');
  });

  it('submits exactly once at the fourth digit and never persists the PIN', async () => {
    boundary.callPost.mockResolvedValue({ message: { csrf_token: 'fresh-csrf' } });
    await render();
    await act(async () => {
      for (const value of ['0', '5', '2', '7']) digit(value).click();
    });
    expect(boundary.callPost).toHaveBeenCalledExactlyOnceWith(
      'ury.ury_pos.floor_pin.sign_in', { pin: '0527' },
    );
    expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain('0527');
    expect(sessionStorage.getItem('ury_floor_pin_session')).toBeNull();
  });

  it('clears a stale failed-logout marker after a successful PIN sign-in', async () => {
    boundary.callPost.mockResolvedValue({ message: { csrf_token: 'fresh-csrf' } });
    sessionStorage.setItem('ury_floor_logout_failed', 'true');
    await render();
    await act(async () => {
      for (const value of ['0', '5', '2', '7']) digit(value).click();
    });
    expect(boundary.callPost).toHaveBeenCalledExactlyOnceWith(
      'ury.ury_pos.floor_pin.sign_in', { pin: '0527' },
    );
    expect(sessionStorage.getItem('ury_floor_logout_failed')).toBeNull();
  });

  it('clears the visible PIN after refusal and ignores a fifth press while busy', async () => {
    let finish!: (value: never) => void;
    boundary.callPost.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    await render();
    await act(async () => {
      for (const value of ['0', '5', '2', '7']) digit(value).click();
    });
    expect(container.querySelector('[aria-label="4-digit PIN"]')?.textContent).toBe('●●●●');
    await act(async () => digit('1').click());
    expect(boundary.callPost).toHaveBeenCalledExactlyOnceWith(
      'ury.ury_pos.floor_pin.sign_in', { pin: '0527' },
    );
    await act(async () => {
      finish(undefined as never);
      await Promise.resolve();
    });
    expect(container.querySelector('[aria-label="4-digit PIN"]')?.textContent).toBe('');
  });

  it('shows a stable refusal message and clears the entry after a wrong PIN', async () => {
    boundary.callPost.mockRejectedValue({ message: 'URY_FLOOR_PIN_REFUSED: That PIN did not work.' });
    await render();
    await act(async () => {
      for (const value of ['0', '5', '2', '7']) digit(value).click();
    });
    expect(container.textContent).toContain("That PIN didn't work");
    expect(container.querySelector('[aria-label="4-digit PIN"]')?.textContent).toBe('');
  });

  it.each([
    ['URY_FLOOR_PIN_LOCKED', 'PIN sign-in is paused on this tablet. Ask a manager.'],
    ['URY_FLOOR_TILL_CLOSED', 'Sign in with username while the till is closed.'],
    ['URY_FLOOR_DEVICE_NOT_PAIRED', 'Sign in with username to pair this tablet.'],
  ])('maps %s to its distinct refusal copy', async (code, copy) => {
    boundary.callPost.mockRejectedValue({ message: `${code}: refusal` });
    await render();
    await act(async () => {
      for (const value of ['0', '5', '2', '7']) digit(value).click();
    });
    expect(container.textContent).toContain(copy);
  });

  it('supports in-app username fallback and sets the session marker', async () => {
    boundary.login.mockResolvedValue(undefined);
    sessionStorage.setItem('ury_floor_logout_failed', 'true');
    await render();
    const fallback = [...container.querySelectorAll('button')]
      .find((button) => button.textContent?.includes('username'));
    expect(fallback).toBeDefined();
    await act(async () => fallback!.click());
    const username = container.querySelector<HTMLInputElement>('input[name="user_id"]');
    const password = container.querySelector<HTMLInputElement>('input[name="currentPassword"]');
    expect(username).toBeDefined();
    expect(password).toBeDefined();
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(username, 'waiter@example.test');
      username!.dispatchEvent(new Event('input', { bubbles: true }));
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(password, 'secret');
      password!.dispatchEvent(new Event('input', { bubbles: true }));
      container.querySelector<HTMLFormElement>('form')!.requestSubmit();
    });
    expect(boundary.login).toHaveBeenCalledExactlyOnceWith({
      username: 'waiter@example.test', password: 'secret',
    });
    expect(localStorage.getItem('userAuth')).toBe('true');
    expect(sessionStorage.getItem('ury_floor_logout_failed')).toBeNull();
  });

  it('clears the password after a failed username sign-in', async () => {
    boundary.login.mockRejectedValue(new Error('bad credentials'));
    await render();
    const fallback = [...container.querySelectorAll('button')]
      .find((button) => button.textContent?.includes('username'));
    await act(async () => fallback!.click());
    const username = container.querySelector<HTMLInputElement>('input[name="user_id"]')!;
    const password = container.querySelector<HTMLInputElement>('input[name="currentPassword"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(username, 'waiter@example.test');
      username.dispatchEvent(new Event('input', { bubbles: true }));
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(password, 'secret');
      password.dispatchEvent(new Event('input', { bubbles: true }));
      container.querySelector<HTMLFormElement>('form')!.requestSubmit();
      await Promise.resolve();
    });
    expect(password.value).toBe('');
  });

  it.each([
    ['unpaired tablet', { paired: false, till_open: true }],
    ['closed till', { paired: true, till_open: false }],
  ])('does not render the floor pad for a Guest on a %s', async (_name, floorPad) => {
    setFloorPad(floorPad);
    await render();
    expect(container.textContent).not.toContain('Staff sign-in');
    expect(container.textContent).not.toContain('Captain shell');
  });
});

describe('floor session isolation', () => {
  it('still loads a provisional cashier profile outside the paired floor flow', async () => {
    setFloorPad({ paired: false, till_open: false });
    boundary.state.user = { name: 'cashier@example.test', roles: ['URY Cashier'], provisional: true };
    boundary.state.posProfile = { name: 'POS-1' };
    await render();
    expect(boundary.state.fetchPosProfile).toHaveBeenCalledExactlyOnceWith();
    expect(container.textContent).toContain('Captain shell');
  });

  it('never renders Captain screens for an offline provisional cookie user', async () => {
    boundary.state.user = { name: 'waiter@example.test', roles: ['URY Captain'], provisional: true };
    boundary.state.posProfile = { custom_floor_stay_signed_in_roles: [] };
    await render();
    expect(container.textContent?.toLowerCase()).toContain('offline');
    expect(container.textContent).not.toContain('Captain shell');
  });

  it('cold-boots a non-stay user without the tab marker back to the PIN pad', async () => {
    boundary.state.user = { name: 'waiter@example.test', roles: ['URY Captain'] };
    boundary.state.posProfile = { custom_floor_stay_signed_in_roles: [{ role: 'URY Manager' }] };
    await render();
    expect(container.textContent).toContain('Staff sign-in');
    expect(container.textContent).not.toContain('Captain shell');
    expect(boundary.logoutSession).toHaveBeenCalled();
  });

  it('keeps a stay-role user signed in without the tab marker', async () => {
    boundary.state.user = { name: 'captain@example.test', roles: ['URY Captain'] };
    boundary.state.posProfile = { custom_floor_stay_signed_in_roles: [{ role: 'URY Captain' }] };
    await render();
    expect(container.textContent).toContain('Captain shell');
    expect(boundary.logoutSession).not.toHaveBeenCalled();
  });

  it('shows the pad after a failed logout without starting the logout again', async () => {
    boundary.state.user = { name: 'captain@example.test', roles: ['URY Captain'] };
    boundary.state.posProfile = { custom_floor_stay_signed_in_roles: [] };
    sessionStorage.setItem('ury_floor_logout_failed', 'true');
    await render();
    expect(container.textContent).toContain('Staff sign-in');
    expect(boundary.logoutSession).not.toHaveBeenCalled();
  });
});
