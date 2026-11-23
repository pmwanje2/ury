import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { call, db, formatCurrency } from '@ury/core';
import { initI18n } from '../src/i18n';
import Orders from '../src/pages/Orders';
import { useRootStore } from '../src/store/root-store';
import { installTailwindUtilities, WRAPS, wrapStyle } from './helpers/tailwind-css';

// U-1, U-2, U-3 and U-5: the cashier's order queue must stay scannable at
// desktop and tablet widths, on touch screens with no hover. jsdom has no
// layout engine, so these tests read the real compiled Tailwind styles each
// element gets (resolved for a viewport width where it matters), not class
// names. The pixel results are measured in a real browser for the PR.

type Order = Record<string, unknown> & { name: string; waiter: string; customer: string };

const state = vi.hoisted(() => ({
  order: {} as Order,
  orders: null as Order[] | null,
  profile: { name: 'POS-1', print_format: 'Bill', view_all_status: 1, paid_limit: 10 },
  selectedStatus: 'Outstanding',
  /** User.full_name as the server holds it */
  users: {} as Record<string, string>,
  usersRefused: false,
  counts: null as { outstanding: number; unbilled: number } | null,
  /** The session whose queue the store holds */
  ordersOwner: 'cashier@example.com',
}));

function freshOrder(): Order {
  return {
    name: 'POS-INV-0001', status: 'Draft', invoice_printed: 0,
    grand_total: 125000, rounded_total: 125000, net_total: 125000,
    total_taxes_and_charges: 0, restaurant_table: 'T-14',
    cashier: 'cashier@example.com', waiter: 'samuel.otieno@domain.example',
    customer: 'Dr. Elena Rostova', customer_name: 'Dr. Elena Rostova',
    custom_guest_label: 'Dr. Elena Rostova (Rm 408)',
    mobile_number: '', posting_date: '2026-10-02', posting_time: '10:00:00', age_minutes: 45,
    order_type: 'Dine In', custom_split_from: '', custom_merged_pos_invoice: '', custom_merged_total: 0,
  };
}

vi.mock('../src/store/root-store', () => ({
  useRootStore: () => ({
    ordersOwner: state.ordersOwner, ordersBranch: 'Branch A',
    orders: state.orders ?? [state.order], selectedOrder: state.order, selectedOrderItems: [],
    user: { name: 'cashier@example.com', roles: ['URY Cashier'] },
    selectedOrderTaxes: [], selectedStatus: state.selectedStatus,
    orderLoading: false, selectedOrderLoading: false, error: null, selectedOrderError: null,
    pagination: { currentPage: 1, hasNextPage: false, hasPreviousPage: false },
    fetchOrders: vi.fn(), selectOrder: vi.fn().mockResolvedValue(true),
    setSelectedStatus: vi.fn(), goToNextPage: vi.fn(),
    goToPreviousPage: vi.fn(), clearSelectedOrder: vi.fn(), orderSearchQuery: '',
  }),
}));
Object.assign(useRootStore, { getState: () => ({ orders: state.orders ?? [state.order] }), setState: vi.fn() });
vi.mock('../src/store/pos-store', () => ({ usePOSStore: () => ({
  posProfile: state.profile, paymentModes: ['Cash'], fetchPaymentModes: vi.fn(),
}) }));
vi.mock('../src/lib/print', () => ({ printOrder: vi.fn().mockResolvedValue(undefined) }));
vi.mock('../src/lib/realtime', () => ({ getRealtimeSocket: async () => { throw new Error('Offline'); } }));

const SOURCES = [
  'src/pages/Orders.tsx',
  'src/components/OrderStatusSidebar.tsx',
  'src/components/Notifications.tsx',
  '../packages/ui/src/components/button.tsx',
  '../packages/ui/src/components/badge.tsx',
  '../packages/ui/src/components/card.tsx',
];

let container: HTMLDivElement;
let root: Root;

beforeAll(async () => {
  await installTailwindUtilities(SOURCES);
});

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  await initI18n('en');
  state.order = freshOrder();
  state.orders = null;
  state.selectedStatus = 'Outstanding';
  state.users = {};
  state.usersRefused = false;
  state.counts = null;
  state.ordersOwner = 'cashier@example.com';
  document.cookie = 'user_id=cashier%40example.com';
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ message: [] }) }));
  vi.spyOn(call, 'get').mockImplementation(async (method, args?: any) => {
    if (method === 'ury.ury_pos.api.getBranch') return { message: 'Branch A' };
    if (method === 'frappe.client.get_list') return { message: [] };
    if (method === 'frappe.client.has_permission') return { message: { has_permission: false } };
    if (method === 'ury.ury_pos.api.get_split_group') return { message: { invoices: [], current: 'POS-INV-0001', group: null } };
    if (method === 'frappe.client.get_count') {
      if (!state.counts) throw new Error('Count unavailable');
      const filters = typeof args?.filters === 'string' ? JSON.parse(args.filters) : args?.filters;
      expect(filters).toMatchObject({ branch: 'Branch A', docstatus: 0, status: 'Draft' });
      return { message: filters.invoice_printed === 0 ? state.counts.unbilled : state.counts.outstanding };
    }
    throw new Error(`Unexpected Frappe call: ${method}`);
  });
  vi.spyOn(call, 'post').mockResolvedValue({ message: null });
  vi.spyOn(db, 'getDocList').mockImplementation(async (doctype: string, args?: any) => {
    if (doctype !== 'User') throw new Error(`Unexpected list: ${doctype}`);
    if (state.usersRefused) throw new Error('Not permitted');
    const [[field, operator, ids]] = args.filters;
    expect([field, operator]).toEqual(['name', 'in']);
    return (ids as string[]).filter((id) => id in state.users).map((name) => ({ name, full_name: state.users[name] }));
  });
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
  // Let lookups started on mount (staff names, status counts) settle.
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}

async function atViewport(width: number, run: () => Promise<void>) {
  const style = await installTailwindUtilities(SOURCES, { viewport: width });
  try {
    await run();
  } finally {
    style.remove();
  }
}

function referenceOf(name: string) {
  const reference = Array.from(container.querySelectorAll('h3')).find((h3) => h3.textContent?.trim() === name);
  expect(reference, `card heading ${name}`).toBeTruthy();
  return reference!;
}

function orderCard(name = 'POS-INV-0001') {
  const card = referenceOf(name).closest('[class*="cursor-pointer"]');
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

/** The smallest card width an auto-filling grid allows, in rem; 0 for a fixed column count. */
function minimumTrackRem(gridTemplateColumns: string) {
  const match = /^repeat\(auto-(?:fill|fit),\s*minmax\(min\(100%,\s*([\d.]+)rem\),\s*1fr\)\)$/.exec(gridTemplateColumns);
  return match ? Number(match[1]) : 0;
}

describe('U-1 order card header', () => {
  it('leads with the order reference and its total on one row', async () => {
    await renderOrders();
    const card = orderCard();
    const reference = referenceOf('POS-INV-0001');
    const total = onlyElementWithText(card, formatCurrency(125000));
    expect(reference.parentElement).toBe(total.parentElement);
    expect(isFlexRow(reference.parentElement!)).toBe(true);
    expect(getComputedStyle(reference).minWidth).toBe('0px');
    expect(getComputedStyle(total).flexShrink).toBe('0');
    expect(getComputedStyle(total).whiteSpace).toBe('nowrap');
  });

  it('keeps the whole check number readable on a touch screen: it wraps, never an ellipsis', async () => {
    // Measured at 1280px: four cards read "POS-INV-2026-…" alike and only a
    // mouse tooltip told them apart.
    await renderOrders();
    expect(wrapStyle(referenceOf('POS-INV-0001'))).toEqual(WRAPS);
  });

  it('lets the total drop under the reference on a narrow card rather than cut the reference short', async () => {
    await renderOrders();
    const total = onlyElementWithText(orderCard(), formatCurrency(125000));
    expect(getComputedStyle(total.parentElement!).flexWrap).toBe('wrap');
    expect(getComputedStyle(total).marginInlineStart).toBe('auto');
  });

  it('gives table and order type their own line, which wraps instead of clipping', async () => {
    await renderOrders();
    const card = orderCard();
    const line = onlyElementWithText(card, /^Table T-14 [·•] Dine In$/);
    const badge = onlyElementWithText(card, 'Not printed');
    expect(isFlexRow(commonAncestor(line, badge))).toBe(false);
    expect(wrapStyle(line)).toEqual(WRAPS);
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

  it('keeps the date, the time and the age each whole, wrapping between them', async () => {
    // Measured at 1280px: "Nov 23, / 1:45 PM" beside "45 / min".
    await renderOrders();
    const card = orderCard();
    const age = onlyElementWithText(card, '45 min');
    const row = age.parentElement!;
    const date = Array.from(row.children).find((child) => child !== age && child.tagName === 'SPAN')!;
    expect(date.textContent).toMatch(/10:00/);
    expect(getComputedStyle(row).flexWrap).toBe('wrap');
    for (const part of [date, age]) expect(getComputedStyle(part).whiteSpace).toBe('nowrap');
    expect(getComputedStyle(row.querySelector('svg')!).flexShrink).toBe('0');
  });
});

describe('U-1 order queue at the widths a till runs at', () => {
  it.each([768, 1024, 1280, 1440, 1536, 1920])('never squeezes an order card below 12rem at %ipx', async (width) => {
    await atViewport(width, async () => {
      await renderOrders();
      const grid = orderCard().parentElement!;
      expect(getComputedStyle(grid).display).toBe('grid');
      expect(minimumTrackRem(getComputedStyle(grid).gridTemplateColumns)).toBeGreaterThanOrEqual(12);
    });
  });

  it.each([1280, 1440])('gives the cards the full width at %ipx and puts Notifications under them', async (width) => {
    await atViewport(width, async () => {
      await renderOrders();
      const rail = container.querySelector('section[aria-label="Notifications"]')!;
      expect(getComputedStyle(rail.parentElement!).gridTemplateColumns).not.toMatch(/14rem/);
    });
  });

  it('puts Notifications beside the cards once the screen is 2xl wide', async () => {
    await atViewport(1536, async () => {
      await renderOrders();
      const rail = container.querySelector('section[aria-label="Notifications"]')!;
      expect(getComputedStyle(rail.parentElement!).gridTemplateColumns).toMatch(/14rem/);
    });
  });
});

describe('U-2 waiter display name', () => {
  it('names the waiter from their User record, not from the email', async () => {
    state.order.waiter = 'jsmith@x.example';
    state.users = { 'jsmith@x.example': 'John Smith' };
    await renderOrders();
    for (const scope of [orderCard(), detailPane()]) {
      onlyElementWithText(scope, 'John Smith');
      expect(scope.textContent).not.toContain('Jsmith');
    }
  });

  it('tells apart two waiters whose emails start the same', async () => {
    const first = { ...freshOrder(), name: 'POS-INV-0001', waiter: 'samuel@a.example' };
    const second = { ...freshOrder(), name: 'POS-INV-0002', waiter: 'samuel@b.example' };
    state.order = first;
    state.orders = [first, second];
    state.users = { 'samuel@a.example': 'Samuel Okello', 'samuel@b.example': 'Samuel Otieno' };
    await renderOrders();
    onlyElementWithText(orderCard('POS-INV-0001'), 'Samuel Okello');
    onlyElementWithText(orderCard('POS-INV-0002'), 'Samuel Otieno');
  });

  it('reads a name from the email only when the User lookup is refused', async () => {
    state.order.waiter = 'jsmith@x.example';
    state.usersRefused = true;
    await renderOrders();
    onlyElementWithText(orderCard(), 'Jsmith');
  });

  it('keeps the waiter name whole on the card', async () => {
    state.users = { 'samuel.otieno@domain.example': 'Samuel Otieno' };
    await renderOrders();
    const waiter = onlyElementWithText(orderCard(), 'Samuel Otieno');
    expect(wrapStyle(waiter)).toEqual(WRAPS);
    expect(orderCard().textContent).not.toContain('samuel.otieno@domain.example');
  });

  it('shows the waiter user id as text in Order Details, so a touch screen can read it', async () => {
    state.users = { 'samuel.otieno@domain.example': 'Samuel Otieno' };
    await renderOrders();
    const pane = detailPane();
    onlyElementWithText(pane, 'Samuel Otieno');
    const id = onlyElementWithText(pane, 'samuel.otieno@domain.example');
    expect(wrapStyle(id).whiteSpace).toBe('normal');
  });

  it('shows a waiter name the payload carries even without a user id', async () => {
    state.order.waiter = '';
    state.order.waiter_name = 'Grace Nakato';
    await renderOrders();
    onlyElementWithText(orderCard(), 'Grace Nakato');
    onlyElementWithText(detailPane(), 'Grace Nakato');
  });

  it('leaves out the waiter row entirely when the check has no waiter', async () => {
    state.order.waiter = '';
    await renderOrders();
    expect(detailPane().querySelector('.lucide-user-check')).toBeNull();
  });

  it('shows a non-email waiter id as it is', async () => {
    state.order.waiter = 'Waiter';
    await renderOrders();
    onlyElementWithText(orderCard(), 'Waiter');
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

  it('names a customer kept under a naming-series id by its customer name', async () => {
    state.order.customer = 'CUST-0001';
    state.order.customer_name = 'Dr. Elena Rostova';
    await renderOrders();
    for (const scope of [orderCard(), detailPane()]) {
      onlyElementWithText(scope, 'Dr. Elena Rostova (Rm 408)');
      expect(scope.textContent).not.toContain('CUST-0001');
    }
  });

  it('names the customer when the check has no guest label', async () => {
    state.order.customer = 'CUST-0001';
    state.order.customer_name = 'Dr. Elena Rostova';
    state.order.custom_guest_label = null;
    await renderOrders();
    onlyElementWithText(orderCard(), 'Dr. Elena Rostova');
  });

  it('still shows both when the label and the customer differ', async () => {
    state.order.custom_guest_label = 'Table Guest';
    state.order.customer = 'CUST-0042';
    state.order.customer_name = 'Corporate Account';
    await renderOrders();
    for (const scope of [orderCard(), detailPane()]) {
      onlyElementWithText(scope, 'Table Guest · Corporate Account');
    }
  });

  it('lets the check name wrap on the card and in Order Details', async () => {
    await renderOrders();
    for (const scope of [orderCard(), detailPane()]) {
      expect(wrapStyle(onlyElementWithText(scope, 'Dr. Elena Rostova (Rm 408)'))).toEqual(WRAPS);
    }
  });
});

describe('Open check details (Simphony check detail area)', () => {
  it('shows the covers on the card and in Order Details', async () => {
    state.order.no_of_pax = 3;
    await renderOrders();
    expect(orderCard().textContent).toContain('Covers: 3');
    onlyElementWithText(detailPane(), 'Covers: 3');
  });

  it('shows no covers when the check has none recorded', async () => {
    state.order.no_of_pax = 0;
    await renderOrders();
    expect(container.textContent).not.toContain('Covers');
  });

  it('shows the order type and whether the bill is printed in Order Details', async () => {
    await renderOrders();
    const pane = detailPane();
    onlyElementWithText(pane, 'Dine In');
    onlyElementWithText(pane, 'Not printed');
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

  it('keeps every status pill, the active one included, inside the filter card on a desktop', async () => {
    await atViewport(1280, async () => {
      await renderOrders();
      const nav = container.querySelector('nav')!;
      const card = nav.firstElementChild!;
      expect(getComputedStyle(card).minWidth).toBe('0px');
      expect(getComputedStyle(card).boxSizing).toBe('border-box');
      const pills = Array.from(nav.querySelectorAll('button'));
      expect(pills.map((pill) => pill.textContent?.trim())).toContain('Outstanding');
      expect(getComputedStyle(pills[0].parentElement!).flexDirection).toBe('column');
      for (const pill of pills) {
        const style = getComputedStyle(pill);
        expect({ width: style.width, minWidth: style.minWidth, boxSizing: style.boxSizing })
          .toEqual({ width: '100%', minWidth: '0px', boxSizing: 'border-box' });
        const label = onlyElementWithText(pill, pill.textContent!.trim());
        expect(wrapStyle(label)).toEqual(WRAPS);
        expect(getComputedStyle(pill.querySelector('svg')!).flexShrink).toBe('0');
      }
    });
  });

  it('lays the status pills out as a wrapping row under 1024px instead of a full-width column', async () => {
    await atViewport(768, async () => {
      await renderOrders();
      const pills = Array.from(container.querySelector('nav')!.querySelectorAll('button'));
      const list = pills[0].parentElement!;
      expect(getComputedStyle(list).flexDirection).toBe('row');
      expect(getComputedStyle(list).flexWrap).toBe('wrap');
      for (const pill of pills) expect(getComputedStyle(pill).width).not.toBe('100%');
    });
  });

  it('counts the open checks in each open status', async () => {
    state.counts = { outstanding: 7, unbilled: 3 };
    await renderOrders();
    const nav = container.querySelector('nav')!;
    const pill = (label: string) => Array.from(nav.querySelectorAll('button')).find((b) => b.textContent?.includes(label))!;
    onlyElementWithText(pill('Outstanding'), '7');
    onlyElementWithText(pill('Unbilled'), '3');
    onlyElementWithText(pill('Draft'), '4');
    for (const closed of ['Recently Paid', 'Paid', 'Consolidated', 'Return']) {
      const found = pill(closed);
      if (found) expect(found.textContent).not.toMatch(/\d/);
    }
  });

  it('reads no counts while the queue still belongs to another session', async () => {
    state.counts = { outstanding: 7, unbilled: 3 };
    state.ordersOwner = 'previous.cashier@example.com';
    await renderOrders();
    const methods = vi.mocked(call.get).mock.calls.map(([method]) => method);
    expect(methods).not.toContain('frappe.client.get_count');
    expect(methods).not.toContain('ury.ury_pos.api.getBranch');
    for (const pill of Array.from(container.querySelector('nav')!.querySelectorAll('button'))) {
      expect(pill.textContent).not.toMatch(/\d/);
    }
  });

  it('shows no counts when they cannot be read', async () => {
    state.counts = null;
    await renderOrders();
    for (const pill of Array.from(container.querySelector('nav')!.querySelectorAll('button'))) {
      expect(pill.textContent).not.toMatch(/\d/);
    }
  });
});
