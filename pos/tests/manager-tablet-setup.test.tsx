import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import Header from '../src/components/Header';
import PairTabletDialog from '../src/components/PairTabletDialog';
import StaffPinsDialog from '../src/components/StaffPinsDialog';
import { initI18n, t } from '../src/i18n';

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
    posProfile: { name: 'POS-1' },
  },
}));

vi.mock('@ury/core', () => ({
  call: { get: fixtures.callGet, post: fixtures.callPost },
  parseFrappeError: (error: unknown, fallback: string) => {
    if (error && typeof error === 'object' && 'message' in error && typeof error.message === 'string') {
      return error.message;
    }
    return fallback;
  },
  resolveBrand: () => ({ name: 'URY POS', logo: '/assets/ury/pos/ury_pos.png' }),
}));

vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector?: (state: typeof fixtures.root) => unknown) =>
    selector ? selector(fixtures.root) : fixtures.root,
}));

vi.mock('../src/store/pos-store', () => ({
  usePOSStore: (selector?: (state: typeof fixtures.pos) => unknown) =>
    selector ? selector(fixtures.pos) : fixtures.pos,
}));

vi.mock('../src/offline/use-outbox', () => ({
  useOutbox: () => ({ rows: [], replaying: false, offline: false }),
}));

vi.mock('../src/offline/cashier', () => ({
  clearCashierStorage: vi.fn().mockResolvedValue(undefined),
}));

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  await initI18n('en');
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
  await render(<MemoryRouter initialEntries={['/pos']}><Header /></MemoryRouter>);
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
    fixtures.callGet.mockResolvedValue({
      message: [{ user: 'waiter@example.test', full_name: 'Waiter One', has_pin: false }],
    });
    fixtures.callPost.mockResolvedValue({ message: { pin: '4821' } });

    await render(<StaffPinsDialog open={true} onOpenChange={vi.fn()} />);
    await settle();

    expect(fixtures.callGet).toHaveBeenCalledWith('ury.ury_pos.floor_pin.pin_staff');
    expect(container.textContent).toContain('Waiter One');
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
    fixtures.callGet.mockRejectedValue({ message: 'URY_FLOOR_PIN_NOT_PERMITTED: refused' });

    await render(<StaffPinsDialog open={true} onOpenChange={vi.fn()} />);
    await settle();

    expect(container.textContent).toContain(t('errors.floor_pin_not_permitted'));
    expect(container.textContent).not.toContain('URY_FLOOR_PIN_NOT_PERMITTED');
  });
});

describe('Pair this tablet dialog', () => {
  it('requires a non-blank label and refuses labels over 80 characters before posting', async () => {
    await render(<PairTabletDialog open={true} onOpenChange={vi.fn()} posProfile="POS-1" />);
    const dialog = container.querySelector('[role="dialog"]')!;
    const input = dialog.querySelector<HTMLInputElement>('input[name="tablet_label"]')!;
    const form = dialog.querySelector('form')!;

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
    const reload = vi.spyOn(window.location, 'reload').mockImplementation(() => {});
    await render(<PairTabletDialog open={true} onOpenChange={vi.fn()} posProfile="POS-1" />);
    const dialog = container.querySelector('[role="dialog"]')!;
    const input = dialog.querySelector<HTMLInputElement>('input[name="tablet_label"]')!;
    setInput(input, 'Floor tablet 1');

    await act(async () => button('Pair tablet', dialog).click());
    await settle();

    expect(fixtures.callPost).toHaveBeenCalledExactlyOnceWith(
      'ury.ury_pos.floor_pin.pair_device', { pos_profile: 'POS-1', label: 'Floor tablet 1' },
    );
    expect(reload).toHaveBeenCalledExactlyOnceWith();
  });

  it('maps URY_FLOOR_PIN_NOT_PERMITTED to stable localized copy', async () => {
    fixtures.callPost.mockRejectedValue({ message: 'URY_FLOOR_PIN_NOT_PERMITTED: refused' });
    await render(<PairTabletDialog open={true} onOpenChange={vi.fn()} posProfile="POS-1" />);

    const dialog = container.querySelector('[role="dialog"]')!;
    const input = dialog.querySelector<HTMLInputElement>('input[name="tablet_label"]')!;
    setInput(input, 'Floor tablet 1');
    await act(async () => button('Pair tablet', dialog).click());
    await settle();

    expect(container.textContent).toContain(t('errors.floor_pin_not_permitted'));
    expect(container.textContent).not.toContain('URY_FLOOR_PIN_NOT_PERMITTED');
  });
});

describe('manager tablet setup translations', () => {
  it.each([
    ['en', 'Staff PINs'],
    ['ar', 'أرقام PIN للموظفين'],
    ['fr', 'PIN du personnel'],
  ])('includes the manager setup strings in %s', async (language, expected) => {
    await initI18n(language);
    expect(t('header.staff_pins')).toContain(expected);
    expect(t('header.pair_tablet')).not.toBe('header.pair_tablet');
    expect(t('pair_tablet.label')).not.toBe('pair_tablet.label');
    expect(t('staff_pins.issue_pin')).not.toBe('staff_pins.issue_pin');
  });
});
