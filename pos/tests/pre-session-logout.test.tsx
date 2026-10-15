import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { showToast } from '@ury/ui';
import { initI18n } from '../src/i18n';
import { checkPOSOpening, validatePOSClose } from '../src/lib/pos-opening-api';
import { getChecklist } from '../src/lib/checklist-api';
import POSOpeningProvider from '../src/components/POSOpeningProvider';

const state = vi.hoisted(() => ({
  user: { name: 'cashier@example.com', roles: ['URY Cashier'] },
  profile: { name: 'POS-1', company: 'Test Co', custom_daily_pos_close: 1 },
  paymentModes: [],
}));
vi.mock('../src/store/root-store', () => ({ useRootStore: () => ({ user: state.user }) }));
vi.mock('../src/store/pos-store', () => ({ usePOSStore: () => ({
  posProfile: state.profile, showVoluntaryClosing: false, paymentModes: state.paymentModes,
}) }));
vi.mock('../src/lib/pos-opening-api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../src/lib/pos-opening-api')>(),
  checkPOSOpening: vi.fn(), validatePOSClose: vi.fn(),
}));
vi.mock('../src/lib/checklist-api', () => ({ getChecklist: vi.fn() }));
vi.mock('../src/lib/pos-closing-api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../src/lib/pos-closing-api')>(),
  getOpenPosOpeningEntries: async () => [],
}));

const opening = {
  name: 'OPEN-1', company: 'Test Co', pos_profile: 'POS-1',
  period_start_date: '2026-10-11 08:00:00',
};
const checklist = {
  items: [{ item_label: 'Count float', is_mandatory: true }],
  logName: 'LOG-1', logStatus: 'Pending',
};
let container: HTMLDivElement;
let root: Root;
let location: { href: string };
let request: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.clearAllMocks();
  await initI18n('en');
  location = { href: '/pos' };
  const browserWindow = window;
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) {
      if (key === 'location') return location;
      if (key === 'csrf_token') return 'fixture-csrf';
      return Reflect.get(target, key, target);
    },
  }));
  request = vi.fn().mockResolvedValue({ ok: true, status: 200 });
  vi.stubGlobal('fetch', request);
  localStorage.clear();
  sessionStorage.clear();
  localStorage.setItem('posOrderTabsData', 'cart');
  localStorage.setItem('posOrderTabsData:cashier@example.com', 'cashier cart');
  localStorage.setItem('ury_active_branch_id', 'Branch A');
  localStorage.setItem('deskPreference', 'keep');
  localStorage.setItem('ury_language', 'en');
  localStorage.setItem('ury_device_credential', 'keep');
  sessionStorage.setItem('posProfile', 'POS-1');
  sessionStorage.setItem('ury.setup.configureState', 'setup');
  sessionStorage.setItem('deskSession', 'keep');
  vi.mocked(checkPOSOpening).mockResolvedValue({ message: [opening] });
  vi.mocked(validatePOSClose).mockResolvedValue({ message: 'Success' });
  vi.mocked(getChecklist).mockResolvedValue(checklist);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  localStorage.clear();
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function render() {
  await act(async () => root.render(<POSOpeningProvider><main>POS workspace</main></POSOpeningProvider>));
}

function button(label: string): HTMLButtonElement {
  const found = Array.from(container.querySelectorAll('button')).find((node) => node.textContent === label);
  expect(found, `${label} must be available on this pre-session gate`).toBeDefined();
  return found!;
}

async function logout() {
  await act(async () => button('Logout').click());
  expect(request).toHaveBeenCalledExactlyOnceWith('/api/method/logout', {
    method: 'POST', headers: { 'X-Frappe-CSRF-Token': 'fixture-csrf' },
  });
  expect(location.href).toBe('/login?redirect-to=%2Fpos');
}

describe('pre-session logout', () => {
  it('renders Logout on the opening checklist and uses shared CSRF logout and named storage cleanup', async () => {
    location.href = '/pos/orders?invoice=PRIVATE-BILL';
    await render();
    expect(container.textContent).toContain('Opening Checklist');
    expect(container.textContent).toContain('Submit Checklist');
    expect(container.textContent).not.toContain('POS workspace');
    await logout();
    for (const key of ['posOrderTabsData', 'posOrderTabsData:cashier@example.com', 'ury_active_branch_id']) {
      expect(localStorage.getItem(key)).toBeNull();
    }
    for (const key of ['posProfile', 'ury.setup.configureState']) {
      expect(sessionStorage.getItem(key)).toBeNull();
    }
    expect(localStorage.getItem('deskPreference')).toBe('keep');
    expect(localStorage.getItem('ury_language')).toBe('en');
    expect(localStorage.getItem('ury_device_credential')).toBe('keep');
    expect(sessionStorage.getItem('deskSession')).toBe('keep');
    expect(button('Logout').disabled).toBe(true);
    await act(async () => button('Logout').click());
    expect(request).toHaveBeenCalledTimes(1);
  });

  it.each(['HTTP', 'network'])('keeps storage and allows retry after %s logout failure', async (failure) => {
    const toast = vi.spyOn(showToast, 'error');
    await render();
    if (failure === 'HTTP') request.mockResolvedValueOnce({ ok: false, status: 403 });
    else request.mockRejectedValueOnce(new Error('Offline'));
    const localBefore = { ...localStorage };
    const sessionBefore = { ...sessionStorage };
    await act(async () => button('Logout').click());
    expect(location.href).toBe('/pos');
    expect({ ...localStorage }).toEqual(localBefore);
    expect({ ...sessionStorage }).toEqual(sessionBefore);
    expect(toast).toHaveBeenCalledWith('Failed to logout. Please try again.');
    expect(button('Logout').disabled).toBe(false);
    await act(async () => button('Logout').click());
    expect(request).toHaveBeenCalledTimes(2);
    expect(location.href).toBe('/login?redirect-to=%2Fpos');
  });

  it('navigates to login only after the URY worker has ended the session', async () => {
    let release!: () => void;
    const released = new Promise<void>((resolve) => { release = resolve; });
    const active = {
      scriptURL: 'http://localhost:3000/sw.js',
      postMessage: (_message: unknown, ports: MessagePort[]) => {
        void released.then(() => ports[0]?.postMessage({ ok: true }));
      },
    };
    vi.stubGlobal('navigator', Object.assign(Object.create(navigator), {
      serviceWorker: {
        controller: active,
        getRegistrations: async () => [{ scope: 'http://localhost:3000/pos', active, unregister: async () => true }],
      },
    }));
    vi.stubGlobal('caches', { keys: async () => ['ury-pos-reads'], delete: async () => true });
    await render();
    await act(async () => button('Logout').click());
    expect(request).toHaveBeenCalledTimes(1);
    expect(location.href).toBe('/pos');
    await act(async () => {
      release();
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(location.href).toBe('/login?redirect-to=%2Fpos');
  });

  it('ignores double clicks and keeps storage until logout is confirmed', async () => {
    let complete!: (value: { ok: boolean }) => void;
    request.mockReturnValue(new Promise((resolve) => { complete = resolve; }));
    await render();
    await act(async () => {
      button('Logout').click();
      button('Logout').click();
    });
    expect(request).toHaveBeenCalledTimes(1);
    expect(button('Logout').disabled).toBe(true);
    expect(localStorage.getItem('posOrderTabsData')).toBe('cart');
    expect(location.href).toBe('/pos');
    await act(async () => complete({ ok: true }));
    expect(location.href).toBe('/login?redirect-to=%2Fpos');
  });

  it('preserves checked items and remarks when logout fails', async () => {
    await render();
    await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
    const remarks = container.querySelector<HTMLInputElement>('input[type="text"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(remarks, 'Float counted');
      remarks.dispatchEvent(new Event('input', { bubbles: true }));
    });
    request.mockRejectedValueOnce(new Error('Offline'));
    await act(async () => button('Logout').click());
    expect(container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(true);
    expect(container.querySelector<HTMLInputElement>('input[type="text"]')!.value).toBe('Float counted');
  });

  it('allows logout while the opening checklist cannot load', async () => {
    vi.mocked(getChecklist).mockRejectedValue(new Error('Checklist unavailable'));
    await render();
    expect(container.textContent).toContain('Opening Checklist');
    expect(container.textContent).toContain('Checklist unavailable');
    await logout();
  });

  it('allows logout while opening status is still loading', async () => {
    vi.mocked(checkPOSOpening).mockReturnValue(new Promise(() => {}));
    await render();
    expect(container.textContent).toContain('Checking POS status');
    await logout();
  });

  it.each([
    ['permission denied', "You don't have permission to open a session. Contact your manager."],
    ['main cashier not open', 'Main cashier POS must be open'],
    ['generic error', 'Service unavailable'],
    ['cross-company open', 'Open Session Elsewhere'],
    ['daily close pending', "Yesterday's session was never closed"],
  ])('allows logout from the %s gate', async (gate, title) => {
    if (gate === 'cross-company open') {
      vi.mocked(checkPOSOpening).mockResolvedValue({ message: [{ ...opening, company: 'Other Co' }] });
    } else if (gate === 'daily close pending') {
      vi.mocked(checkPOSOpening).mockResolvedValue({ message: 1 });
      vi.mocked(validatePOSClose).mockResolvedValue({ message: 'Failed' });
    } else {
      vi.mocked(checkPOSOpening).mockRejectedValue(new Error(title));
    }
    await render();
    expect(container.textContent).toContain(title);
    expect(container.textContent).not.toContain('POS workspace');
    await logout();
  });

  it('keeps Logout reachable inside the forced daily-close dialog', async () => {
    vi.mocked(checkPOSOpening).mockResolvedValue({ message: 1 });
    vi.mocked(validatePOSClose).mockResolvedValue({ message: 'Failed' });
    await render();
    await act(async () => button('Close Previous Session').click());
    expect(container.querySelector('[role="dialog"]')).not.toBeNull();
    expect(container.querySelector('[role="dialog"]')!.contains(button('Logout'))).toBe(true);
    await logout();
  });
});
