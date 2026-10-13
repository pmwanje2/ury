import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import MobileQRLayout from '../../self-order/src/layouts/MobileQRLayout';
import type { OrderingContext } from '../../self-order/src/lib/api';

const context: OrderingContext = {
  session: 'synthetic-session', restaurant: 'Restaurant A', table: 'T-07', source: 'QR Table', layout: 'Mobile',
  capabilities: { product_detail_enabled: true, show_item_images: false, show_item_descriptions: false,
    item_notes_enabled: false, request_bill_enabled: true, customer_payment_enabled: false,
    payment_link_enabled: false, pay_at_counter_enabled: true, add_to_running_table_enabled: true },
};
const order = { invoice: 'POS-1', items: [{ item_code: 'Soup', item_name: 'Soup', qty: 1, rate: 12000, amount: 12000 }],
  grand_total: 12000, billed: false };
let root: Root, container: HTMLDivElement;
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  sessionStorage.clear();
  vi.spyOn(call, 'get').mockImplementation(async (path: string) => ({ message: path.endsWith('get_customer_menu') ? {
    items: [{ item: 'Soup', item_name: 'Soup', rate: 12000, disabled: 0, special_dish: 0, item_image: null, course: null, course_label: null }],
    modified_time: null, name: 'Menu A',
  } : order }));
  vi.spyOn(call, 'post').mockResolvedValue({ message: { status: 'Requested', request: 'REQ-1' } });
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount()); container.remove();
  vi.restoreAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals();
});
async function render(ctx = context) { await act(async () => root.render(<MobileQRLayout initialContext={ctx} />)); }
function button(label: string) { return [...container.querySelectorAll('button')].find(b => b.textContent?.trim() === label); }
async function click(label: string) {
  expect(button(label), `Missing guest action: ${label}`).toBeDefined();
  await act(async () => button(label)!.click());
}
async function addSoup() {
  const item = [...container.querySelectorAll('button')].find(b => b.textContent?.startsWith('Soup'));
  expect(item).toBeDefined();
  await act(async () => item!.click());
}

describe('guest service requests', () => {
  it('sends only the verified session and confirms staff notification with a cooldown', async () => {
    await render(); vi.useFakeTimers(); await click('Call waiter');
    expect(call.post).toHaveBeenCalledWith('ury.ury.api.self_ordering.request_assistance', { session: 'synthetic-session' });
    expect(container.textContent).toContain('Waiter requested — staff notified');
    expect(button('Call waiter')?.disabled).toBe(true);
    await act(async () => vi.advanceTimersByTime(60000));
    expect(button('Call waiter')?.disabled).toBe(false);
  });
  it('blocks repeat taps while the request is in flight', async () => {
    vi.mocked(call.post).mockImplementation(() => new Promise(() => {}));
    await render(); await click('Call waiter'); await click('Calling waiter…');
    expect(call.post).toHaveBeenCalledTimes(1);
  });
  it('keeps the cooldown when Start Over starts a new ordering session', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await render(); await click('Call waiter'); await click('Start Over');
    expect(button('Call waiter')?.disabled).toBe(true);
  });
  it('does not confirm failed requests and allows a retry', async () => {
    vi.mocked(call.post).mockRejectedValue({ _server_messages: JSON.stringify([JSON.stringify({ message: 'Session expired. Rescan.' })]) });
    await render(); await click('Call waiter');
    expect(container.textContent).toContain('Session expired. Rescan.');
    expect(container.textContent).not.toContain('staff notified');
    expect(button('Call waiter')?.disabled).toBe(false);
  });
  it.each([ { ...context, source: 'QR Pickup', table: null },
    { ...context, capabilities: { ...context.capabilities, request_bill_enabled: false } } ])('hides assistance when unavailable', async ctx => {
    await render(ctx); expect(button('Call waiter')).toBeUndefined();
  });
});

describe('ERP refusal text', () => {
  it.each([
    { _server_messages: JSON.stringify([JSON.stringify({ message: '[SAL-QR-WAITER-OPENS-TABLE] Please ask a waiter to open your table first.' }),
      JSON.stringify({ message: 'Error while placing order: [SAL-QR-WAITER-OPENS-TABLE] Please ask a waiter to open your table first.' })]) },
    { exception: 'frappe.exceptions.ValidationError: Error while placing order: [SAL-QR-WAITER-OPENS-TABLE] Please ask a waiter to open your table first.' },
  ])('shows the ERP reason without its code, preserving the cart', async refusal => {
    vi.mocked(call.post).mockRejectedValue(refusal);
    await render(); await addSoup();
    await click('Place Order');
    expect(container.textContent).toContain('Please ask a waiter to open your table first.');
    expect(container.textContent).not.toContain('[SAL-QR-WAITER-OPENS-TABLE]');
    expect(container.textContent).not.toContain('Could not place the order');
    expect(button('Place Order')).toBeDefined();
  });
  it('renders server HTML as harmless text only', async () => {
    vi.mocked(call.post).mockRejectedValue({ _server_messages: JSON.stringify([JSON.stringify({ message: '[DENIED] <b>Please ask staff.</b><img src=x onerror=evil()>' })]) });
    await render(); await addSoup(); await click('Place Order');
    expect(container.textContent).toContain('Please ask staff.');
    expect(container.querySelector('b, [onerror], script')).toBeNull();
  });
  it('keeps the friendly fallback for transport failures without ERP text', async () => {
    vi.mocked(call.post).mockRejectedValue({ httpStatus: 503 });
    await render(); await addSoup(); await click('Place Order');
    expect(container.textContent).toContain('Could not place the order. Please try again.');
  });
});

it('renders the guest boot brand name and logo without interpreting HTML', async () => {
  const browser = window as Window & { frappe?: { boot: object } };
  browser.frappe = { boot: { ury_brand: { name: 'Salama <script>evil()</script>', logo: '/files/salama.png' } } };
  try {
    await render();
    expect(container.querySelector('header')?.textContent).toContain('Salama <script>evil()</script>');
    expect(container.querySelector('header img')?.getAttribute('src')).toBe('/files/salama.png');
    expect(container.querySelector('script')).toBeNull();
  } finally { delete browser.frappe; }
});

it('renders an optional logo even when no display name is configured', async () => {
  const browser = window as Window & { frappe?: { boot: object } };
  browser.frappe = { boot: { ury_brand: { name: null, logo: '/files/logo-only.png' } } };
  try {
    await render();
    expect(container.querySelector('header img')?.getAttribute('src')).toBe('/files/logo-only.png');
  } finally { delete browser.frappe; }
});
