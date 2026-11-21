// Ported from fork pos/tests/cancel-order-reason.test.tsx (#2) and the
// "Orders void reason dialog" block of pos/tests/Orders.test.tsx (#67).
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { showToast } from '@ury/ui';
import { initI18n, t } from '../i18n';
import Orders from './Orders';

const state = vi.hoisted(() => ({
  canCancel: true,
  profile: { name: 'POS-1', view_all_status: 1, paid_limit: 10 } as {
    name: string; view_all_status: number; paid_limit: number; custom_void_reasons?: string | null;
  },
  order: {
    name: 'POS-INV-1', status: 'Draft', invoice_printed: 1,
    grand_total: 40, rounded_total: 40, net_total: 40,
    total_taxes_and_charges: 0, restaurant_table: 'Table 1',
    cashier: 'cashier@example.com', waiter: 'Waiter', customer: 'Customer',
    mobile_number: '', posting_date: '2026-10-04', posting_time: '10:00:00',
    order_type: 'Dine In', age_minutes: 10,
  },
}));

vi.mock('../store/root-store', () => ({ useRootStore: () => ({
  ordersOwner: "cashier@example.com",
  orders: [state.order], selectedOrder: state.order,
  selectedOrderItems: [{ name: 'ROW-1', item_name: 'Soup', qty: 2, rate: 20, amount: 40 }],
  selectedOrderTaxes: [], selectedStatus: 'Draft',
  user: { name: 'cashier@example.com', roles: ['URY Cashier'] },
  orderLoading: false, selectedOrderLoading: false, error: null, selectedOrderError: null,
  pagination: { currentPage: 1, hasNextPage: false, hasPreviousPage: false },
  fetchOrders: vi.fn(), selectOrder: vi.fn(), setSelectedStatus: vi.fn(),
  goToNextPage: vi.fn(), goToPreviousPage: vi.fn(), clearSelectedOrder: vi.fn(), orderSearchQuery: '',
}) }));
vi.mock('../store/pos-store', () => ({ usePOSStore: () => ({
  posProfile: state.profile, paymentModes: ['Cash'], fetchPaymentModes: vi.fn(),
}) }));
vi.mock('../components/OrderStatusSidebar', () => ({ default: () => null }));
vi.mock('../components/OrderActionsMenu', () => ({ default: () => null }));
vi.mock('../components/SplitGroupPanel', () => ({ default: () => null }));
vi.mock('../components/MergedBillPanel', () => ({ default: () => null }));

let container: HTMLDivElement;
let root: Root;
const cancelMethod = 'ury.ury.doctype.ury_order.ury_order.cancel_order';
const cancelOrderButton = () =>
  container.querySelector<HTMLButtonElement>(`button[aria-label="${t('orders.cancel_order')}"]`);

beforeEach(async () => {
  document.cookie = 'user_id=cashier%40example.com; Path=/';
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  state.canCancel = true;
  delete state.profile.custom_void_reasons;
  await initI18n('en');
  vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
    if (method === 'frappe.client.has_permission') return { message: { has_permission: state.canCancel } };
    return { message: [] };
  });
  vi.spyOn(call, 'post').mockResolvedValue({ message: null });
  vi.spyOn(showToast, 'error').mockImplementation(() => '' as never);
  vi.spyOn(showToast, 'success').mockImplementation(() => '' as never);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function renderOrders() {
  await act(async () => root.render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><Orders /></MemoryRouter>,
  ));
}

async function openCancelDialog() {
  await renderOrders();
  await vi.waitFor(() => {
    expect(cancelOrderButton()).not.toBeNull();
  });
  await act(async () => {
    cancelOrderButton()!.click();
  });
  const dialog = container.querySelector('[role="dialog"]');
  expect(dialog).not.toBeNull();
  return dialog!;
}

async function typeInto(textarea: HTMLTextAreaElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(textarea, value);
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

async function choose(select: HTMLSelectElement | null, value: string) {
  expect(select, 'A configured reason list must render a select').not.toBeNull();
  await act(async () => {
    select!.value = value;
    select!.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

const confirmButton = (dialog: Element) => [...dialog.querySelectorAll('button')]
  .find((button) => button.textContent?.trim() === 'Confirm Cancel')!;

describe('Orders mandatory void reason (#2)', () => {
  it.each(['', ' \t\n ', 'a', 'ab', 'abc', '  abc  '])('refuses %j before posting and explains the minimum', async (reason) => {
    const dialog = await openCancelDialog();
    await typeInto(dialog.querySelector('textarea')!, reason);
    await act(async () => confirmButton(dialog).click());
    expect(call.post).not.toHaveBeenCalledWith(cancelMethod, expect.anything());
    expect(showToast.error).toHaveBeenCalledWith(expect.stringContaining('at least 4 characters'));
    expect(container.querySelector('[role="dialog"]')).not.toBeNull();
  });

  it.each([
    ['en', 'at least 4 characters'], ['fr', '4 caractères'], ['ar', '4 أحرف'],
  ])('explains the minimum in %s before posting', async (language, minimum) => {
    await initI18n(language);
    const dialog = await openCancelDialog();
    await typeInto(dialog.querySelector('textarea')!, 'abc');
    const confirm = dialog.querySelectorAll('button');
    await act(async () => confirm[confirm.length - 1].click());
    expect(call.post).not.toHaveBeenCalledWith(cancelMethod, expect.anything());
    expect(showToast.error).toHaveBeenCalledWith(expect.stringContaining(minimum));
  });

  it.each(['Void', ' \tVoid\n '])('posts an accepted reason %j without surrounding whitespace', async (reason) => {
    const dialog = await openCancelDialog();
    await typeInto(dialog.querySelector('textarea')!, reason);
    await act(async () => confirmButton(dialog).click());
    expect(call.post).toHaveBeenCalledWith(cancelMethod, { invoice_id: 'POS-INV-1', reason: 'Void' });
    expect(showToast.error).not.toHaveBeenCalled();
    expect(container.querySelector('[role="dialog"]')).toBeNull();
  });

  it('does not offer cancellation without native cancel permission', async () => {
    state.canCancel = false;
    await renderOrders();
    expect(container.querySelector('button[aria-label="Cancel order"]')).toBeNull();
    expect(call.post).not.toHaveBeenCalledWith(cancelMethod, expect.anything());
  });
});

describe('Orders void reason dialog (#67)', () => {
  const optionValues = (dialog: Element) =>
    [...dialog.querySelectorAll('option')].map((option) => (option as HTMLOptionElement).value);

  it('keeps free text when the profile lists no void reasons', async () => {
    state.profile.custom_void_reasons = ' \n\t';
    const dialog = await openCancelDialog();
    expect(dialog.querySelector('select')).toBeNull();
    expect(dialog.querySelector('textarea')).not.toBeNull();
  });

  it('offers each configured reason once, plus Other, instead of free text', async () => {
    state.profile.custom_void_reasons = 'Guest changed mind\n\nWrong item keyed\nGuest changed mind\n';
    const dialog = await openCancelDialog();
    expect(dialog.querySelector('textarea')).toBeNull();
    expect(dialog.querySelector('select')).not.toBeNull();
    expect(optionValues(dialog).filter(Boolean)).toEqual([
      'Guest changed mind', 'Wrong item keyed', '__other__',
    ]);
    expect([...dialog.querySelectorAll('option')].map((option) => option.textContent)).toContain('Other');
  });

  it('posts a listed reason exactly', async () => {
    state.profile.custom_void_reasons = 'Kitchen error';
    const dialog = await openCancelDialog();
    await choose(dialog.querySelector('select'), 'Kitchen error');
    await act(async () => confirmButton(dialog).click());
    expect(call.post).toHaveBeenCalledWith(cancelMethod, { invoice_id: 'POS-INV-1', reason: 'Kitchen error' });
  });

  it('refuses Other without a four-character note and posts Other with one', async () => {
    state.profile.custom_void_reasons = 'Kitchen error';
    const dialog = await openCancelDialog();
    await choose(dialog.querySelector('select'), '__other__');
    const note = dialog.querySelector('textarea');
    expect(note).not.toBeNull();
    await act(async () => confirmButton(dialog).click());
    expect(call.post).not.toHaveBeenCalled();
    for (const short of ['a', 'ab', 'abc']) {
      await typeInto(note!, short);
      await act(async () => confirmButton(dialog).click());
      expect(call.post).not.toHaveBeenCalled();
    }
    await typeInto(note!, 'late');
    await act(async () => confirmButton(dialog).click());
    expect(call.post).toHaveBeenCalledWith(cancelMethod, { invoice_id: 'POS-INV-1', reason: 'Other: late' });
  });

  it('does not ask for four characters when a reason list is configured', async () => {
    state.profile.custom_void_reasons = 'Kitchen error';
    const dialog = await openCancelDialog();
    expect(dialog.textContent).not.toMatch(/at least 4 characters/i);
    expect(dialog.textContent).toContain('Choose a configured void reason, or Other with a note.');
  });

  it('refuses a choice that is no longer on the configured list', async () => {
    state.profile.custom_void_reasons = 'Kitchen error\nGuest changed mind';
    const dialog = await openCancelDialog();
    await choose(dialog.querySelector('select'), 'Guest changed mind');
    state.profile.custom_void_reasons = 'Kitchen error';
    await renderOrders();
    const again = container.querySelector('[role="dialog"]');
    expect(again).not.toBeNull();
    await act(async () => confirmButton(again!).click());
    expect(call.post).not.toHaveBeenCalled();
  });

  it('clears the previous choice and note when the dialog opens again', async () => {
    state.profile.custom_void_reasons = 'Kitchen error';
    const dialog = await openCancelDialog();
    await choose(dialog.querySelector('select'), '__other__');
    await typeInto(dialog.querySelector('textarea')!, 'late order');
    const cancel = [...dialog.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Cancel');
    await act(async () => cancel!.click());
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    await act(async () => {
      container.querySelector<HTMLButtonElement>('button[aria-label="Cancel order"]')!.click();
    });
    const reopened = container.querySelector('[role="dialog"]');
    expect(reopened).not.toBeNull();
    expect((reopened!.querySelector('select') as HTMLSelectElement).value).toBe('');
    expect(reopened!.querySelector('textarea')).toBeNull();
  });

  it('names the void-reason select, Other and the list hint in Arabic and French', () => {
    const localesDir = join(dirname(fileURLToPath(import.meta.url)), '../i18n/locales');
    const read = (lang: string) => JSON.parse(readFileSync(join(localesDir, `${lang}.json`), 'utf8'));
    const [arabic, french, english] = [read('ar'), read('fr'), read('en')];
    expect(arabic.order.select_void_reason).toBe('اختر سبب الإلغاء');
    expect(arabic.order.void_reason_other).toBe('غير ذلك');
    expect(french.order.select_void_reason).toBe("Sélectionner un motif d'annulation");
    expect(french.order.void_reason_other).toBe('Autre');
    expect(english.order.select_void_reason).toBe('Select a void reason');
    expect(english.order.void_reason_other).toBe('Other');
    for (const locale of [arabic, french, english]) {
      expect(locale.errors.choose_void_reason).toBeTruthy();
    }
  });
});
