import { testLocks } from './offline-locks';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initI18n } from '../src/i18n';
import { hasFloorSessionMarker, setFloorSessionMarker } from '../src/lib/floor-session';
import AuthGuard from '../src/components/AuthGuard';

// Theme T4 (fork #62): paired floor tablets show an in-app PIN pad while the till is open.

let pendingOwner: string | null = null;
vi.mock('../src/offline/outbox', async (original) => ({
  ...await original<typeof import('../src/offline/outbox')>(),
  withOutboxLogin: async (user: string | null, run: (pending: boolean) => Promise<void>) => {
    if (pendingOwner && user !== pendingOwner) throw new Error('Queued orders belong to another waiter');
    await run(!!pendingOwner);
  },
}));

const boundary = vi.hoisted(() => ({
  callPost: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
  disconnectQzPrinter: vi.fn(),
  state: {
    user: null as null | { name: string; roles: string[]; provisional?: boolean },
    isLoading: false,
    error: null as string | null,
    posProfile: null as Record<string, unknown> | null,
    hasAccess: true,
    allowedRoles: ['URY Captain'],
    fetchPosProfile: vi.fn(),
    checkAuth: vi.fn(),
  },
}));

vi.mock('@ury/core', () => ({
  call: { post: boundary.callPost },
  auth: { loginWithUsernamePassword: boundary.login, getLoggedInUser: async () => 'waiter@example.test' },
  getSessionUser: () => 'waiter@example.test',
  logout: boundary.logout,
  disconnectQzPrinter: boundary.disconnectQzPrinter,
  watchIdle: () => () => {},
  parseFrappeError: (error: unknown, fallback = '') =>
    String((error as { message?: unknown })?.message || fallback),
}));

vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector?: (state: typeof boundary.state) => unknown) =>
    selector ? selector(boundary.state) : boundary.state,
}));

let container: HTMLDivElement;
let root: Root;
let replace: ReturnType<typeof vi.fn>;
let reload: ReturnType<typeof vi.fn>;

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

async function enterPin(pin: string) {
  await act(async () => {
    for (const value of pin) digit(value).click();
  });
}

function setInput(input: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

async function openUsernameForm() {
  const fallback = [...container.querySelectorAll('button')]
    .find((button) => button.textContent?.includes('username'));
  expect(fallback, 'username fallback should be offered').toBeDefined();
  await act(async () => fallback!.click());
}

beforeEach(async () => {
  vi.stubGlobal('navigator', { onLine: true, locks: testLocks() });
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  pendingOwner = null;
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  boundary.state.user = null;
  boundary.state.posProfile = null;
  boundary.state.hasAccess = true;
  boundary.logout.mockResolvedValue(undefined);
  replace = vi.fn();
  reload = vi.fn();
  const browserWindow = window;
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) {
      if (key === 'location') return { replace, reload, href: '/pos/order', pathname: '/pos/order' };
      return Reflect.get(target, key, target);
    },
  }));
  await initI18n('en');
  setFloorPad({ paired: true, till_open: true });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  delete (window as Window & { floor_pad?: unknown }).floor_pad;
});

describe('paired floor tablet sign-in', () => {
  it('shows the PIN pad without redirecting a Guest to native /login', async () => {
    await render();
    expect(container.textContent).toContain('Staff sign-in');
    expect(container.textContent).not.toContain('Captain shell');
  });

  it('submits exactly once at the fourth digit and never persists the PIN', async () => {
    boundary.callPost.mockResolvedValue({ message: { user: 'waiter@example.test', csrf_token: 'fresh-csrf' } });
    await render();
    await enterPin('0527');
    expect(boundary.callPost).toHaveBeenCalledExactlyOnceWith(
      'ury.ury_pos.floor_pin.sign_in', { pin: '0527' },
    );
    expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain('0527');
    expect(hasFloorSessionMarker('waiter@example.test')).toBe(true);
    expect(reload).toHaveBeenCalledExactlyOnceWith();
  });

  it.each(['PIN', 'unidentified PIN', 'password'])(
    'refuses to confirm a sibling cookie on an unlocked %s sign-in', async method => {
      vi.stubGlobal('navigator', { onLine: true });
      await render();
      // This request authenticated the first cashier. The session probe now sees the sibling's cookie.
      const first = 'first@example.test';
      if (method === 'password') {
        boundary.login.mockResolvedValueOnce({ message: 'Logged In', full_name: 'Cashier' });
        await openUsernameForm();
        await act(async () => {
          setInput(container.querySelector('input[name=user_id]')!, first);
          setInput(container.querySelector('input[type=password]')!, 'secret');
          container.querySelector('form')!.requestSubmit();
        });
        expect(boundary.login).toHaveBeenCalledOnce();
      } else {
        boundary.callPost.mockResolvedValueOnce({ message: method === 'PIN' ? { user: first } : { full_name: 'Cashier' } });
        await enterPin('0527');
        expect(boundary.callPost).toHaveBeenCalledOnce();
      }
      expect(hasFloorSessionMarker('waiter@example.test')).toBe(false);
      expect(hasFloorSessionMarker(first)).toBe(false);
      expect(reload).not.toHaveBeenCalled();
      expect(container.querySelector('[role=alert]')).not.toBeNull();
    },
  );

  it('keeps a failed-logout guest on password sign-in until authenticated', async () => {
    boundary.callPost.mockResolvedValue({ message: { user: 'waiter@example.test', csrf_token: 'fresh-csrf' } });
    localStorage.setItem('ury_floor_logout_failed', 'waiter@example.test');
    await render();
    expect(container.querySelector('input[type=password]')).not.toBeNull();
    expect(container.querySelector('[aria-label="4-digit PIN"]')).toBeNull();
    expect(localStorage.getItem('ury_floor_logout_failed')).not.toBeNull();
  });

  it('clears the visible PIN after the attempt and ignores a fifth press while busy', async () => {
    let finish!: (value: never) => void;
    boundary.callPost.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    await render();
    await enterPin('0527');
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
    await enterPin('0527');
    expect(container.textContent).toContain("That PIN didn't work");
    expect(container.querySelector('[aria-label="4-digit PIN"]')?.textContent).toBe('');
    expect(reload).not.toHaveBeenCalled();
  });

  it.each([
    ['a rate limit', { message: 'Too Many Requests', httpStatus: 429 }],
    ['a server error', { message: 'Internal Server Error', httpStatus: 500 }],
    ['a network failure', new Error('')],
  ])('shows a try-again message, not a wrong-PIN message, after %s', async (_label, reason) => {
    boundary.callPost.mockRejectedValue(reason);
    await render();
    await enterPin('0527');
    expect(container.textContent).toContain('Could not check the PIN. Try again.');
    expect(container.textContent).not.toContain("That PIN didn't work");
    expect(container.querySelector('[aria-label="4-digit PIN"]')?.textContent).toBe('');
    expect(reload).not.toHaveBeenCalled();
  });

  it.each([
    ['URY_FLOOR_PIN_LOCKED', 'PIN sign-in is paused on this tablet. Ask a manager.'],
    ['URY_FLOOR_TILL_CLOSED', 'Sign in with username while the till is closed.'],
    ['URY_FLOOR_DEVICE_NOT_PAIRED', 'Sign in with username to pair this tablet.'],
  ])('maps %s to its distinct refusal copy', async (code, copy) => {
    boundary.callPost.mockRejectedValue({ message: `${code}: refusal` });
    await render();
    await enterPin('0527');
    expect(container.textContent).toContain(copy);
    expect(container.textContent).not.toContain(code);
  });

  it.each(['URY_FLOOR_TILL_CLOSED', 'URY_FLOOR_DEVICE_NOT_PAIRED'])(
    'changes the live keypad to password-only after %s', async (code) => {
      boundary.callPost.mockRejectedValueOnce({ message: code });
      await render();
      await enterPin('0527');
      expect(container.querySelector('input[type=password]')).not.toBeNull();
      expect(container.querySelector('[aria-label="4-digit PIN"]')).toBeNull();
      expect(container.textContent).not.toContain('Back to PIN');
      expect(container.querySelector('[role=alert]')?.textContent).toContain('username');
    },
  );

  it.each([
    [{ method: 'Email', setup: true, token_delivery: true, prompt: 'Check your registered email for setup instructions.' }, 'Check your registered email', true],
    [{ method: 'SMS', setup: false, token_delivery: false }, 'could not be delivered', false],
    [{ method: 'OTP App', setup: false }, 'contact your administrator', false],
  ] as const)('displays challenge instructions for %j', async (verification, copy, acceptsOtp) => {
    boundary.login.mockResolvedValueOnce({ tmp_id: 'challenge', verification });
    await render();
    await openUsernameForm();
    await act(async () => {
      setInput(container.querySelector('input[name=user_id]')!, 'waiter@example.test');
      setInput(container.querySelector('input[type=password]')!, 'secret');
      container.querySelector('form')!.requestSubmit();
    });
    expect(container.textContent).toContain(copy);
    expect(!!container.querySelector('input[autocomplete=one-time-code]')).toBe(acceptsOtp);
    expect(reload).not.toHaveBeenCalled();
  });

  it('finishes password sign-in only after Frappe accepts the OTP', async () => {
    setFloorPad({ paired: false, till_open: false });
    boundary.login.mockResolvedValueOnce({ verification: { method: 'Email' }, tmp_id: 'challenge' })
      .mockRejectedValueOnce(new Error('Invalid verification code'))
      .mockResolvedValueOnce({ message: 'Logged In' });
    await render();
    const submit = () => container.querySelector('form')!.requestSubmit();
    await act(async () => {
      setInput(container.querySelector('input[name=user_id]')!, 'waiter@example.test');
      setInput(container.querySelector('input[type=password]')!, 'secret');
      submit();
    });
    expect(reload).not.toHaveBeenCalled();
    expect(sessionStorage.getItem('userAuth')).toBeNull();
    expect(container.querySelector('input[type=password]')).toBeNull();
    const otp = container.querySelector<HTMLInputElement>('input[autocomplete=one-time-code]')!;
    await act(async () => { setInput(otp, '000001'); submit(); });
    expect(container.querySelector('[role=alert]')).not.toBeNull();
    expect(sessionStorage.getItem('userAuth')).toBeNull();
    await act(async () => { setInput(otp, '000002'); submit(); });
    expect(boundary.login).toHaveBeenLastCalledWith({ tmp_id: 'challenge', otp: '000002' });
    expect(hasFloorSessionMarker('waiter@example.test')).toBe(true);
    expect(reload).toHaveBeenCalledOnce();
  });

  it('replaces a failed logout with confirmed password authentication', async () => {
    boundary.login.mockResolvedValue({ message: 'Logged In' });
    localStorage.setItem('ury_floor_logout_failed', 'waiter@example.test');
    await render();
    const username = container.querySelector<HTMLInputElement>('input[name="user_id"]');
    const password = container.querySelector<HTMLInputElement>('input[name="currentPassword"]');
    expect(username).not.toBeNull();
    expect(password).not.toBeNull();
    await act(async () => {
      setInput(username!, 'waiter@example.test');
      setInput(password!, 'secret');
      container.querySelector<HTMLFormElement>('form')!.requestSubmit();
    });
    expect(boundary.login).toHaveBeenCalledExactlyOnceWith({
      username: 'waiter@example.test', password: 'secret',
    });
    expect(hasFloorSessionMarker('waiter@example.test')).toBe(true);
    expect(localStorage.getItem('ury_floor_logout_failed')).toBeNull();
  });

  it('clears the password after a failed username sign-in', async () => {
    boundary.login.mockRejectedValue(new Error('bad credentials'));
    await render();
    await openUsernameForm();
    const username = container.querySelector<HTMLInputElement>('input[name="user_id"]')!;
    const password = container.querySelector<HTMLInputElement>('input[name="currentPassword"]')!;
    await act(async () => {
      setInput(username, 'waiter@example.test');
      setInput(password, 'secret');
      container.querySelector<HTMLFormElement>('form')!.requestSubmit();
      await Promise.resolve();
    });
    expect(password.value).toBe('');
    expect(sessionStorage.getItem('userAuth')).toBeNull();
  });

  it.each([
    ['unpaired tablet', { paired: false, till_open: true }],
    ['closed till', { paired: true, till_open: false }],
  ])('renders only username sign-in for a Guest on a %s', async (_name, floorPad) => {
    setFloorPad(floorPad);
    await render();
    expect(container.querySelector('input[type=password]')).not.toBeNull();
    expect(container.querySelector('[aria-label="4-digit PIN"]')).toBeNull();
    expect(container.textContent).not.toContain('Captain shell');
  });
});

describe('floor session isolation', () => {
  it('protects both provisional sign-in paths and admits only owner password recovery', async () => {
    pendingOwner = 'waiter@example.test';
    boundary.state.user = { name: pendingOwner, roles: ['URY Captain'], provisional: true };
    boundary.state.posProfile = { name: 'Till' };
    await render();
    await enterPin('0527');
    expect(boundary.callPost).not.toHaveBeenCalled();
    await openUsernameForm();
    const submit = async (username: string) => act(async () => {
      setInput(container.querySelector('input[name=user_id]')!, username);
      setInput(container.querySelector('input[type=password]')!, 'secret');
      container.querySelector('form')!.requestSubmit();
    });
    await submit('another@example.test');
    expect(boundary.login).not.toHaveBeenCalled();
    boundary.login.mockResolvedValueOnce({ message: 'Logged In' });
    await submit(pendingOwner);
    expect(boundary.login).toHaveBeenCalledOnce();
    expect(reload).toHaveBeenCalledOnce();
    expect(sessionStorage.getItem('userAuth')).toBeNull();
  });

  it('loads the provisional cashier profile on an unpaired till', async () => {
    setFloorPad({ paired: false, till_open: true });
    boundary.state.user = { name: 'cashier@example.test', roles: ['URY Cashier'], provisional: true };
    boundary.state.posProfile = { name: 'Till' };
    await render();
    expect(boundary.state.fetchPosProfile).toHaveBeenCalledExactlyOnceWith();
    expect(container.textContent).toContain('Captain shell');
  });

  it.each([false, true])('keeps a provisional cookie on the paired sign-in screen (tab marker: %s)', async (marker) => {
    if (marker) setFloorSessionMarker('waiter@example.test', false);
    boundary.state.user = { name: 'waiter@example.test', roles: ['URY Captain'], provisional: true };
    boundary.state.posProfile = { custom_floor_stay_signed_in_roles: [] };
    await render();
    expect(container.textContent).not.toContain('Captain shell');
    expect(container.querySelector('[role="status"]')?.textContent).toContain('Offline');
    expect(boundary.logout).not.toHaveBeenCalled();
  });

  it('cold-boots a non-stay user without the tab marker back to the PIN pad', async () => {
    boundary.state.user = { name: 'waiter@example.test', roles: ['URY Captain'] };
    boundary.state.posProfile = { custom_floor_stay_signed_in_roles: [{ role: 'URY Manager' }] };
    await render();
    expect(container.textContent).toContain('Staff sign-in');
    expect(container.textContent).not.toContain('Captain shell');
    expect(boundary.logout).toHaveBeenCalledOnce();
  });

  it('keeps a stay-role user signed in without the tab marker', async () => {
    boundary.state.user = { name: 'captain@example.test', roles: ['URY Captain'] };
    boundary.state.posProfile = { custom_floor_stay_signed_in_roles: [{ role: 'URY Captain' }] };
    await render();
    expect(container.textContent).toContain('Captain shell');
    expect(boundary.logout).not.toHaveBeenCalled();
  });

  it.each([null, 'another@example.test'])('rejects legacy local or mismatched tab identity %s', async (marker) => {
    localStorage.setItem('userAuth', 'true');
    if (marker) sessionStorage.setItem('userAuth', marker);
    boundary.state.user = { name: 'waiter@example.test', roles: ['URY Captain'] };
    boundary.state.posProfile = { name: 'Till' };
    await render();
    expect(container.textContent).not.toContain('Captain shell');
    expect(boundary.logout).toHaveBeenCalledOnce();
  });

  it('keeps a signed-in waiter with the tab marker in the app', async () => {
    setFloorSessionMarker('waiter@example.test', false);
    boundary.state.user = { name: 'waiter@example.test', roles: ['URY Captain'] };
    boundary.state.posProfile = { custom_floor_stay_signed_in_roles: [] };
    await render();
    expect(container.textContent).toContain('Captain shell');
    expect(boundary.logout).not.toHaveBeenCalled();
  });

  it.each([{ paired: false, till_open: false }, { paired: true, till_open: false }, { paired: true, till_open: true }])(
    'retries failed logout and locks the surviving session to password sign-in on %j', async (pad) => {
    setFloorPad(pad);
    boundary.logout.mockResolvedValue(false);
    boundary.state.user = { name: 'captain@example.test', roles: ['URY Captain'] };
    boundary.state.posProfile = { custom_floor_stay_signed_in_roles: [] };
    localStorage.setItem('ury_floor_logout_failed', 'waiter@example.test');
    await render();
    expect(container.querySelector('input[type=password]')).not.toBeNull();
    expect(container.textContent).not.toContain('Captain shell');
    expect(boundary.logout).toHaveBeenCalledOnce();
    expect(replace).not.toHaveBeenCalled();
    expect(localStorage.getItem('ury_floor_logout_failed')).not.toBeNull();
  });

  it('leaves an unpaired device on the normal signed-in flow', async () => {
    setFloorPad({ paired: false, till_open: false });
    boundary.state.user = { name: 'cashier@example.test', roles: ['URY Cashier'] };
    boundary.state.posProfile = { name: 'POS-1', custom_floor_stay_signed_in_roles: [] };
    await render();
    expect(boundary.state.fetchPosProfile).toHaveBeenCalledExactlyOnceWith();
    expect(container.textContent).toContain('Captain shell');
    expect(boundary.logout).not.toHaveBeenCalled();
  });
});

vi.mock('../src/lib/logout-store', () => ({ readLogoutMarker: async () => false, writeLogoutMarker: async () => {}, readGeneration: async () => 0, writeGeneration: async () => {} }));
