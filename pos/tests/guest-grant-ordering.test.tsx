import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import MobileQRLayout from '../../self-order/src/layouts/MobileQRLayout';

const context = {
  session: 'grant-session', restaurant: 'Salama', table: 'T-07', source: 'QR Table' as const, layout: 'Mobile' as const,
  capabilities: { product_detail_enabled: true, show_item_images: false, show_item_descriptions: false,
    item_notes_enabled: true, request_bill_enabled: true, customer_payment_enabled: false,
    payment_link_enabled: false, pay_at_counter_enabled: true, add_to_running_table_enabled: true },
};
const menu = { items: [{ item: 'Soup', item_name: 'Soup', rate: 12000, disabled: 0, special_dish: 0,
  item_image: null, course: null, course_label: null }], modified_time: null, name: 'Menu A' };
const order = { invoice: 'POS-1', table: 'T-07', items: [{ item_code: 'Soup', item_name: 'Soup', qty: 1, rate: 12000, amount: 12000, comment: null }],
  grand_total: 12000, billed: false };

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  window.history.replaceState({}, '', '/order/#g=raw-grant-secret');
  vi.spyOn(call, 'post').mockImplementation(async (path: string) => {
    if (path.endsWith('get_granted_ordering_context')) return { message: context } as never;
    if (path.endsWith('get_customer_menu')) return { message: menu } as never;
    if (path.endsWith('get_customer_order')) return { message: order } as never;
    return { message: { status: 'Requested', request: 'REQ-1' } } as never;
  });
  vi.spyOn(call, 'get').mockRejectedValue(new Error('Static QR GET must not be used for grant boot'));
  sessionStorage.clear();
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

describe('guest phone grant bootstrap', () => {
  it('consumes and removes #g without putting the grant in a request URL, then shows the existing bill/order actions', async () => {
    await act(async () => root.render(<MobileQRLayout />));
    const post = vi.mocked(call.post);
    expect(window.location.hash).toBe('');
    expect(window.location.href).not.toContain('raw-grant-secret');
    expect(post).toHaveBeenCalledWith('ury.ury.api.self_ordering.get_granted_ordering_context', { session: 'raw-grant-secret' });
    expect(post).not.toHaveBeenCalledWith(expect.stringContaining('raw-grant-secret'), expect.anything());
    expect(container.textContent).toContain('Soup');
    expect(container.textContent).toContain('Call waiter');
    expect(container.textContent).toContain('Request Bill');
  });

  it('shows the generic refusal when a grant is expired or revoked', async () => {
    vi.mocked(call.post).mockRejectedValueOnce({
      _server_messages: JSON.stringify([JSON.stringify({ message: 'URY_GUEST_GRANT_REQUIRED: Please ask staff for a new ordering link.' })]),
    });
    await act(async () => root.render(<MobileQRLayout />));
    expect(container.textContent).toContain('URY_GUEST_GRANT_REQUIRED: Please ask staff for a new ordering link.');
    expect(container.textContent).not.toContain('Soup');
  });
});
