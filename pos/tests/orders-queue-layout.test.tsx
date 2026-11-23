import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { call, formatCurrency } from '@ury/core';
import { initI18n } from '../src/i18n';
import Orders from '../src/pages/Orders';
import { useRootStore } from '../src/store/root-store';
import { installTailwindUtilities, TRUNCATES, truncationStyle } from './helpers/tailwind-css';

// U-1, U-2, U-3 and U-5: the cashier's order queue must stay scannable at
// desktop and tablet widths. jsdom has no layout engine, so these tests read
// the real compiled Tailwind styles each element gets, not class names.

const state = vi.hoisted(() => ({
  order: {
    name: 'POS-INV-0001', status: 'Draft', invoice_printed: 0,
    grand_total: 125000, rounded_total: 125000, net_total: 125000,
    total_taxes_and_charges: 0, restaurant_table: 'T-14',
    cashier: 'cashier@example.com', waiter: 'samuel.otieno@domain.example' as string,
    customer: 'Dr. Elena Rostova', custom_guest_label: 'Dr. Elena Rostova (Rm 408)' as string | null,
    mobile_number: '', posting_date: '2026-10-02', posting_time: '10:00:00',
    order_type: 'Dine In', custom_split_from: '', custom_merged_pos_invoice: '', custom_merged_total: 0,
  },
  profile: { name: 'POS-1', print_format: 'Bill', view_all_status: 1, paid_limit: 10 },
  selectedStatus: 'Outstanding',
}));

vi.mock('../src/store/root-store', () => ({
  useRootStore: () => ({
    ordersOwner: 'cashier@example.com', ordersBranch: 'Branch A',
    orders: [state.order], selectedOrder: state.order, selectedOrderItems: [],
    user: { name: 'cashier@example.com', roles: ['URY Cashier'] },
    selectedOrderTaxes: [], selectedStatus: state.selectedStatus,
    orderLoading: false, selectedOrderLoading: false, error: null, selectedOrderError: null,
    pagination: { currentPage: 1, hasNextPage: false, hasPreviousPage: false },
    fetchOrders: vi.fn(), selectOrder: vi.fn().mockResolvedValue(true),
    setSelectedStatus: vi.fn(), goToNextPage: vi.fn(),
    goToPreviousPage: vi.fn(), clearSelectedOrder: vi.fn(), orderSearchQuery: '',
  }),
}));
Object.assign(useRootStore, { getState: () => ({ orders: [state.order] }), setState: vi.fn() });
vi.mock('../src/store/pos-store', () => ({ usePOSStore: () => ({
  posProfile: state.profile, paymentModes: ['Cash'], fetchPaymentModes: vi.fn(),
}) }));
vi.mock('../src/lib/print', () => ({ printOrder: vi.fn().mockResolvedValue(undefined) }));
vi.mock('../src/lib/realtime', () => ({ getRealtimeSocket: async () => { throw new Error('Offline'); } }));

let container: HTMLDivElement;
let root: Root;

beforeAll(async () => {
  await installTailwindUtilities([
    'src/pages/Orders.tsx',
    'src/components/OrderStatusSidebar.tsx',
    '../packages/ui/src/components/button.tsx',
    '../packages/ui/src/components/badge.tsx',
    '../packages/ui/src/components/card.tsx',
  ]);
});

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  await initI18n('en');
  state.order.waiter = 'samuel.otieno@domain.example';
  state.order.customer = 'Dr. Elena Rostova';
  state.order.custom_guest_label = 'Dr. Elena Rostova (Rm 408)';
  state.selectedStatus = 'Outstanding';
  document.cookie = 'user_id=cashier%40example.com';
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ message: [] }) }));
  vi.spyOn(call, 'get').mockImplementation(async (method) => {
    if (method === 'ury.ury_pos.api.getBranch') return { message: 'Branch A' };
    if (method === 'frappe.client.get_list') return { message: [] };
    if (method === 'frappe.client.has_permission') return { message: { has_permission: false } };
    if (method === 'ury.ury_pos.api.get_split_group') return { message: { invoices: [], current: 'POS-INV-0001', group: null } };
    throw new Error(`Unexpected Frappe call: ${method}`);
  });
  vi.spyOn(call, 'post').mockResolvedValue({ message: null });
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

function orderCard() {
  const card = container.querySelector('h3[title="POS-INV-0001"]')?.closest('[class*="cursor-pointer"]');
  expect(card).toBeTruthy();
  return card!;
}

/** The right-hand Order Details pane: the parent of the sticky header naming the order. */
function detailPane() {
  const heading = Array.from(container.querySelectorAll('h2')).find((h2) => h2.textContent?.trim() === 'POS-INV-0001');
  const pane = heading?.closest('.sticky')?.parentElement;
  expect(pane).toBeTruthy();
  expect(pane!.contains(orderCard())).toBe(false);
  return pane!;
}

/** Innermost elements under `scope` whose own text matches. */
function elementsWithText(scope: Element, text: string | RegExp) {
  return Array.from(scope.querySelectorAll('*')).filter((el) => {
    const own = el.textContent?.trim() ?? '';
    const matches = typeof text === 'string' ? own === text : text.test(own);
    return matches && !Array.from(el.children).some((child) => {
      const inner = child.textContent?.trim() ?? '';
      return typeof text === 'string' ? inner === text : text.test(inner);
    });
  });
}

function onlyElementWithText(scope: Element, text: string | RegExp) {
  const found = elementsWithText(scope, text);
  expect(found, `exactly one element reading ${String(text)}`).toHaveLength(1);
  return found[0];
}

function commonAncestor(a: Element, b: Element) {
  let node: Element | null = a;
  while (node && !node.contains(b)) node = node.parentElement;
  return node!;
}

/** A horizontal flex row squeezes its children against each other. */
function isFlexRow(element: Element) {
  const style = getComputedStyle(element);
  return style.display.endsWith('flex') && (style.flexDirection === '' || style.flexDirection.startsWith('row'));
}

describe('U-1 order card header', () => {
  it('leads with the order reference and its total on one row', async () => {
    await renderOrders();
    const card = orderCard();
    const reference = card.querySelector('h3[title="POS-INV-0001"]')!;
    const total = onlyElementWithText(card, formatCurrency(125000));
    expect(reference.parentElement).toBe(total.parentElement);
    expect(isFlexRow(reference.parentElement!)).toBe(true);
    expect(truncationStyle(reference)).toEqual(TRUNCATES);
    expect(getComputedStyle(total).flexShrink).toBe('0');
    expect(getComputedStyle(total).whiteSpace).toBe('nowrap');
  });

  it('lets the total drop under the reference on a narrow card rather than cut the reference short', async () => {
    // Measured at 1440px: two cards per row are ~186px wide, so an unwrapping
    // row 1 showed "POS-IN…" on every card. Wrapping keeps the reference whole
    // and the total right-aligned on its own line.
    await renderOrders();
    const card = orderCard();
    const total = onlyElementWithText(card, formatCurrency(125000));
    expect(getComputedStyle(total.parentElement!).flexWrap).toBe('wrap');
    expect(getComputedStyle(total).marginInlineStart).toBe('auto');
  });

  it('gives table and order type their own line that never wraps word by word', async () => {
    await renderOrders();
    const card = orderCard();
    const line = onlyElementWithText(card, /^Table T-14 [·•] Dine In$/);
    const badge = onlyElementWithText(card, 'Not printed');
    expect(isFlexRow(commonAncestor(line, badge))).toBe(false);
    expect(truncationStyle(line)).toEqual(TRUNCATES);
    expect(line.getAttribute('title')).toBe('Table T-14 · Dine In');
    expect(line.textContent).toBe('Table T-14 · Dine In');
  });

  it('puts the print and status badges on a wrapping row of their own', async () => {
    await renderOrders();
    const card = orderCard();
    const printed = onlyElementWithText(card, 'Not printed');
    const status = onlyElementWithText(card, 'Draft');
    const row = printed.parentElement!;
    expect(status.parentElement).toBe(row);
    expect(getComputedStyle(row).flexWrap).toBe('wrap');
    expect(row.textContent).not.toContain('T-14');
    expect(row.textContent).not.toContain('POS-INV-0001');
    for (const badge of [printed, status]) {
      expect(getComputedStyle(badge).whiteSpace).toBe('nowrap');
      expect(getComputedStyle(badge).flexShrink).toBe('0');
    }
  });
});

describe('U-2 waiter display name', () => {
  it('names the waiter from the email on the card and the detail pane, keeping the email as a tooltip', async () => {
    await renderOrders();
    for (const scope of [orderCard(), detailPane()]) {
      const waiter = onlyElementWithText(scope, 'Samuel Otieno');
      expect(waiter.getAttribute('title')).toBe('samuel.otieno@domain.example');
      expect(truncationStyle(waiter)).toEqual(TRUNCATES);
      expect(scope.textContent).not.toContain('samuel.otieno@domain.example');
    }
  });

  it('shows a non-email waiter id as it is', async () => {
    state.order.waiter = 'Waiter';
    await renderOrders();
    expect(onlyElementWithText(orderCard(), 'Waiter').getAttribute('title')).toBe('Waiter');
  });
});

describe('U-3 guest label and customer', () => {
  it('does not repeat a customer the guest label already names', async () => {
    await renderOrders();
    for (const scope of [orderCard(), detailPane()]) {
      onlyElementWithText(scope, 'Dr. Elena Rostova (Rm 408)');
      expect(scope.textContent).not.toContain('(Rm 408) · Dr. Elena Rostova');
    }
  });

  it('still shows both when the label and the customer differ', async () => {
    state.order.custom_guest_label = 'Table Guest';
    state.order.customer = 'Corporate Account';
    await renderOrders();
    for (const scope of [orderCard(), detailPane()]) {
      onlyElementWithText(scope, 'Table Guest · Corporate Account');
    }
  });
});

describe('U-5 order status sidebar', () => {
  it('fills its column instead of holding a fixed width wider than it', async () => {
    await renderOrders();
    const sidebar = container.querySelector('nav')!.parentElement!;
    expect(getComputedStyle(sidebar).width).toBe('100%');
    expect(getComputedStyle(sidebar).minWidth).toBe('0px');
    expect(getComputedStyle(sidebar.parentElement!).minWidth).toBe('0px');
  });

  it('keeps every status pill, the active one included, inside the filter card', async () => {
    await renderOrders();
    const nav = container.querySelector('nav')!;
    const card = nav.firstElementChild!;
    expect(getComputedStyle(card).minWidth).toBe('0px');
    expect(getComputedStyle(card).boxSizing).toBe('border-box');
    const pills = Array.from(nav.querySelectorAll('button'));
    expect(pills.map((pill) => pill.textContent?.trim())).toContain('Outstanding');
    for (const pill of pills) {
      const style = getComputedStyle(pill);
      expect({ width: style.width, minWidth: style.minWidth, boxSizing: style.boxSizing })
        .toEqual({ width: '100%', minWidth: '0px', boxSizing: 'border-box' });
      const label = onlyElementWithText(pill, pill.textContent!.trim());
      expect(truncationStyle(label)).toEqual(TRUNCATES);
      expect(pill.getAttribute('title')).toBe(pill.textContent!.trim());
      expect(getComputedStyle(pill.querySelector('svg')!).flexShrink).toBe('0');
    }
  });
});
