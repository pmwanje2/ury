import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import Header from '../src/components/Header';
import { initI18n, t } from '../src/i18n';

// Theme T4 (fork #69 691ce2c5): managers pair a floor tablet and issue Staff PINs
// from the /pos header, gated by native POS Profile write permission.

const fixtures = vi.hoisted(() => ({
  callGet: vi.fn(),
  callPost: vi.fn(),
  root: {
    user: { name: 'manager@example.test', full_name: 'Manager', roles: ['URY Manager'] },
    orderSearchQuery: '',
    setOrderSearchQuery: vi.fn(),
  },
  pos: {
    searchQuery: '',
    setSearchQuery: vi.fn(),
    setShowVoluntaryClosing: vi.fn(),
    posProfile: { name: 'POS-1' } as Record<string, unknown> | null,
  },
}));

vi.mock('@ury/core', () => ({
  call: { get: fixtures.callGet, post: fixtures.callPost },
  logout: vi.fn(),
  withReturnContext: (path: string) => path,
  isPrintStatusDisabled: () => true,
  DESK_RETURN_STORAGE_KEY: 'ury:desk_return_to',
  parseFrappeError: (error: unknown, fallback: string) => {
    if (error && typeof error === 'object' && 'message' in error && typeof error.message === 'string') {
      return error.message;
    }
    return fallback;
  },
}));
vi.mock('../src/components/PrinterWatch', () => ({ PrinterStatusButton: () => null }));
vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector?: (state: typeof fixtures.root) => unknown) =>
    selector ? selector(fixtures.root) : fixtures.root,
}));
vi.mock('../src/store/pos-store', () => ({
  usePOSStore: (selector?: (state: typeof fixtures.pos) => unknown) =>
    selector ? selector(fixtures.pos) : fixtures.pos,
}));

type DialogModule = { default: (props: Record<string, unknown>) => React.ReactElement | null };

async function dialog(name: 'PairTabletDialog' | 'StaffPinsDialog'): Promise<DialogModule['default']> {
  const path = `../src/components/${name}`;
  const mod = await import(/* @vite-ignore */ path).catch(() => null) as DialogModule | null;
  expect(mod?.default, `${name} must exist`).toBeTypeOf('function');
  return mod!.default;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  await initI18n('en');
  fixtures.pos.posProfile = { name: 'POS-1' };
  fixtures.callGet.mockImplementation(async (method: string) => {
    if (method === 'frappe.client.has_permission') return { message: { has_permission: true } };
    return { message: [] };
  });
  fixtures.callPost.mockResolvedValue({ message: {} });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function render(element: React.ReactElement) {
  await act(async () => root.render(element));
}

async function settle() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function button(label: string, scope: ParentNode = container): HTMLButtonElement {
  const match = [...scope.querySelectorAll<HTMLButtonElement>('button')]
    .find((candidate) => candidate.textContent?.trim() === label);
  expect(match, `button ${label} should exist`).toBeDefined();
  return match!;
}

function setInput(input: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

async function openManagerMenu() {
  await render(<MemoryRouter initialEntries={['/dashboard']}><Header /></MemoryRouter>);
  await settle();
  await act(async () => button('Manager').click());
  await settle();
}

describe('POS Header manager tablet setup access', () => {
  it('keeps Staff PINs and Pair this tablet hidden without POS Profile write permission', async () => {
    fixtures.callGet.mockImplementation(async (method: string) => {
      if (method === 'frappe.client.has_permission') return { message: { has_permission: false } };
      return { message: [] };
    });

    await openManagerMenu();

    expect(container.textContent).toContain('Logout');
    expect(container.textContent).not.toContain('Staff PINs');
    expect(container.textContent).not.toContain('Pair this tablet');
  });

  it('fails closed when the POS Profile permission lookup fails', async () => {
    fixtures.callGet.mockRejectedValue(new Error('permission lookup failed'));

    await openManagerMenu();

    expect(container.textContent).not.toContain('Staff PINs');
    expect(container.textContent).not.toContain('Pair this tablet');
  });

  it('shows both manager actions after the native permission lookup succeeds', async () => {
    await openManagerMenu();

    expect(button('Staff PINs')).toBeDefined();
    expect(button('Pair this tablet')).toBeDefined();
    expect(fixtures.callGet).toHaveBeenCalledWith('frappe.client.has_permission', {
      doctype: 'POS Profile', docname: 'POS-1', perm_type: 'write',
    });
  });
});

describe('Staff PINs dialog', () => {
  it('loads staff with pin_staff and shows an issued PIN once without storing it', async () => {
    const StaffPinsDialog = await dialog('StaffPinsDialog');
    fixtures.callGet.mockResolvedValue({
      message: [{ user: 'waiter@example.test', full_name: 'Waiter One', has_pin: false }],
    });
    fixtures.callPost.mockResolvedValue({ message: { pin: '4821' } });

    await render(<StaffPinsDialog open={true} onOpenChange={vi.fn()} />);
    await settle();

    expect(fixtures.callGet).toHaveBeenCalledWith('ury.ury_pos.floor_pin.pin_staff');
    expect(container.textContent).toContain('Waiter One');
    expect(container.textContent).toContain('Needs PIN');
    await act(async () => button('Give PIN').click());
    await settle();

    expect(fixtures.callPost).toHaveBeenCalledExactlyOnceWith(
      'ury.ury_pos.floor_pin.issue_pin', { user: 'waiter@example.test' },
    );
    expect(container.textContent).toContain('4821');
    expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain('4821');

    await act(async () => button('Done').click());
    expect(container.textContent).not.toContain('4821');
  });

  it('maps URY_FLOOR_PIN_NOT_PERMITTED to stable localized copy', async () => {
    const StaffPinsDialog = await dialog('StaffPinsDialog');
    fixtures.callGet.mockRejectedValue({ message: 'URY_FLOOR_PIN_NOT_PERMITTED: refused' });

    await render(<StaffPinsDialog open={true} onOpenChange={vi.fn()} />);
    await settle();

    expect(container.textContent).toContain(t('errors.floor_pin_not_permitted'));
    expect(container.textContent).not.toContain('URY_FLOOR_PIN_NOT_PERMITTED');
  });
});

describe('Pair this tablet dialog', () => {
  it('requires a non-blank label and refuses labels over 80 characters before posting', async () => {
    const PairTabletDialog = await dialog('PairTabletDialog');
    await render(<PairTabletDialog open={true} onOpenChange={vi.fn()} posProfile="POS-1" />);
    const input = container.querySelector<HTMLInputElement>('input[name="tablet_label"]')!;
    const form = container.querySelector('form')!;

    setInput(input, '   ');
    await act(async () => form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
    expect(fixtures.callPost).not.toHaveBeenCalled();
    expect(container.textContent).toContain(t('pair_tablet.label_required'));

    setInput(input, 'x'.repeat(81));
    await act(async () => form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
    expect(fixtures.callPost).not.toHaveBeenCalled();
    expect(container.textContent).toContain(t('pair_tablet.label_too_long'));
    expect(input.maxLength).toBe(80);
  });

  it('posts the selected profile and label, then reloads after pairing succeeds', async () => {
    const PairTabletDialog = await dialog('PairTabletDialog');
    const reload = vi.fn();
    const browserWindow = window;
    vi.stubGlobal('window', new Proxy(browserWindow, {
      get(target, key) {
        if (key === 'location') return { reload };
        return Reflect.get(target, key, target);
      },
    }));
    await render(<PairTabletDialog open={true} onOpenChange={vi.fn()} posProfile="POS-1" />);
    setInput(container.querySelector<HTMLInputElement>('input[name="tablet_label"]')!, 'Floor tablet 1');

    await act(async () => button('Pair tablet').click());
    await settle();

    expect(fixtures.callPost).toHaveBeenCalledExactlyOnceWith(
      'ury.ury_pos.floor_pin.pair_device', { pos_profile: 'POS-1', label: 'Floor tablet 1' },
    );
    expect(reload).toHaveBeenCalledExactlyOnceWith();
  });

  it.each([
    ['URY_FLOOR_PIN_NOT_PERMITTED', 'errors.floor_pin_not_permitted'],
    ['URY_FLOOR_TABLET_LABEL_INVALID', 'pair_tablet.label_invalid'],
  ])('maps %s to stable localized copy', async (code, key) => {
    const PairTabletDialog = await dialog('PairTabletDialog');
    fixtures.callPost.mockRejectedValue({ message: `${code}: refused` });
    await render(<PairTabletDialog open={true} onOpenChange={vi.fn()} posProfile="POS-1" />);
    setInput(container.querySelector<HTMLInputElement>('input[name="tablet_label"]')!, 'Floor tablet 1');

    await act(async () => button('Pair tablet').click());
    await settle();

    expect(t(key)).not.toBe(key);
    expect(container.textContent).toContain(t(key));
    expect(container.textContent).not.toContain(code);
  });
});

describe('manager tablet setup translations', () => {
  it.each([
    ['en', 'Staff PINs'],
    ['ar', 'أرقام PIN للموظفين'],
    ['fr', 'PIN du personnel'],
    ['es', 'PIN del personal'],
  ])('includes the manager setup strings in %s', async (language, expected) => {
    await initI18n(language);
    expect(t('header.staff_pins')).toContain(expected);
    for (const key of ['header.pair_tablet', 'pair_tablet.label', 'staff_pins.give_pin', 'floor_pin.title', 'floor_pin.refused']) {
      expect(t(key), `${language} ${key}`).not.toBe(key);
    }
  });
});
