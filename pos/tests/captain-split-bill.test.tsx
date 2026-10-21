import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { initI18n, t } from '../src/i18n';
import CaptainActionsMenu from '../src/captain/components/CaptainActionsMenu';
import CaptainOrder from '../src/captain/pages/CaptainOrder';
import { syncOrder } from '../src/lib/order-api';

type CaptainContextTestResult = {
  context: { order: Record<string, unknown> | null; permissions?: Record<string, boolean> };
  permissions: Record<string, boolean>;
  refetchContext: ReturnType<typeof vi.fn>;
  reloadOrder: ReturnType<typeof vi.fn>;
};

const boundary = vi.hoisted(() => ({
  context: null as CaptainContextTestResult | null,
  posStore: null as Record<string, unknown> | null,
  getSplitGroup: vi.fn(),
  printOrder: vi.fn(),
  floorSignOut: vi.fn(),
  isFloorPadActive: vi.fn(() => true),
  isStaySignedIn: vi.fn((user: { roles: string[] }, profile: { custom_floor_stay_signed_in_roles?: Array<{ role?: string }> }) =>
    user.roles.some((role) => profile.custom_floor_stay_signed_in_roles?.some((row) => row.role === role))),
}));

vi.mock('../src/captain/hooks/useTableOrderContext', () => ({
  useTableOrderContext: () => boundary.context,
}));

vi.mock('../src/store/pos-store', () => ({
  usePOSStore: () => boundary.posStore,
}));

vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector: (state: unknown) => unknown) => selector({
    user: { name: 'waiter@example.com', roles: ['URY Captain'] },
  }),
}));

vi.mock('../src/lib/order-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/order-api')>();
  return {
    ...actual,
    syncOrder: vi.fn(),
    reprintKot: vi.fn(),
    captainTransfer: vi.fn(),
    tableTransfer: vi.fn(),
  };
});

vi.mock('../src/lib/invoice-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/invoice-api')>();
  return { ...actual, getSplitGroup: boundary.getSplitGroup };
});

vi.mock('../src/lib/print', () => ({ printOrder: boundary.printOrder }));
vi.mock('../src/lib/floor-session', () => ({
  floorSignOut: boundary.floorSignOut,
  isFloorPadActive: boundary.isFloorPadActive,
  isStaySignedIn: boundary.isStaySignedIn,
}));
vi.mock('../src/lib/table-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/table-api')>();
  return { ...actual, getVacantTablesForBranch: vi.fn(async () => []) };
});

vi.mock('../src/captain/components/CaptainMenu', () => ({ default: () => null }));
vi.mock('../src/captain/components/CaptainOrderLine', () => ({ default: () => null }));
vi.mock('../src/components/CommentDialog', () => ({ default: () => null }));
vi.mock('../src/components/CustomerSelect', () => ({ CustomerSelect: () => null }));
vi.mock('../src/components/HoldFireControls', () => ({ HoldFireControls: () => null }));
vi.mock('../src/components/ProductDialog', () => ({ default: () => null }));
vi.mock('../src/components/TableTransferDialog', () => ({ default: () => null }));
vi.mock('../src/components/CaptainTransferDialog', () => ({ default: () => null }));

let container: HTMLDivElement;
let root: Root;

const permissions = {
  view: true,
  modify: true,
  reduce_items: true,
  remove_items: true,
  transfer_table: false,
  transfer_captain: false,
  print_bill: true,
  reprint_kot: false,
  split_bill: true,
  settle: false,
  cancel: false,
};

const baseOrder = {
  name: 'POS-INV-1',
  invoice_printed: 0,
  grand_total: 300,
  rounded_total: 300,
  restaurant_table: 'T-05',
  waiter: 'waiter@example.com',
  customer: 'Walk In',
  customer_name: 'Walk In',
  mobile_number: '',
  status: 'Draft',
  order_type: 'Dine In',
  posting_date: '2026-10-20',
  posting_time: '12:00:00',
  cashier: 'cashier@example.com',
  net_total: 300,
  total_taxes_and_charges: 0,
};

const sibling = (name: string, index: number) => ({
  ...baseOrder,
  name,
  rounded_total: 100,
  split_index: index,
  split_total: 3,
  custom_split_group: 'SPLIT-GROUP-1',
  is_original: index === 1,
  docstatus: 0,
});

function renderMenu(showSplitBill: boolean) {
  root.render(
    <CaptainActionsMenu
      isOpen
      onOpenChange={() => {}}
      showSplitBill={showSplitBill}
      onSplitBill={() => {}}
    />,
  );
}

async function renderOrder() {
  await act(async () => root.render(
    <MemoryRouter initialEntries={['/order/table/T-05']}>
      <Routes>
        <Route path="/order/table/:table" element={<CaptainOrder />} />
        <Route path="/order" element={<div>tables</div>} />
      </Routes>
    </MemoryRouter>,
  ));
}

function button(label: string) {
  return Array.from(container.querySelectorAll<HTMLButtonElement>('button'))
    .find((candidate) => candidate.textContent?.includes(label) || candidate.getAttribute('aria-label') === label);
}

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  await initI18n('en');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);

  boundary.context = {
    context: { table: { name: 'T-05', restaurant_room: 'Dining' }, order: baseOrder, assignment: { waiter: 'waiter@example.com', is_mine: true }, permissions },
    permissions,
    isContextLoading: false,
    contextError: null,
    isOrderReady: true,
    alreadyOrderedLines: [],
    newOrChangedLines: [],
    reductionPendingLines: [],
    refetchContext: vi.fn(async () => {}),
    reloadOrder: vi.fn(async () => {
      await (boundary.posStore?.loadTableOrder as (table: string) => Promise<void>)('T-05');
    }),
  };
  boundary.posStore = {
    activeOrders: [
      { id: 'ITEM-1', name: 'Soup', price: 100, quantity: 1, uniqueId: 'u1', invoiceItemName: 'ROW-1' },
      { id: 'ITEM-2', name: 'Juice', price: 200, quantity: 1, uniqueId: 'u2', invoiceItemName: 'ROW-2' },
    ],
    removeFromOrder: vi.fn(), updateQuantity: vi.fn(), updateItemComment: vi.fn(), setSelectedItem: vi.fn(),
    isUpdatingOrder: false, orderId: 'POS-INV-1', loadTableOrder: vi.fn(async () => {}), posProfile: { name: 'Salama POS', branch: 'Branch A', print_format: 'Receipt', print_type: 'qz', qz_host: 'localhost' },
    paymentModes: ['Cash'], orderComment: '', noOfPax: 2, setNoOfPax: vi.fn(), lastModifiedTime: null,
    selectedRoom: 'Dining', selectedCustomer: { id: 'Walk In', name: 'Walk In', phone: '' }, clearTableOrder: vi.fn(),
    isOrderInteractionDisabled: () => false,
  };
  boundary.getSplitGroup.mockReset().mockResolvedValue({ invoices: [], current: 'POS-INV-1', group: null });
  boundary.printOrder.mockReset().mockResolvedValue('qz');
  boundary.floorSignOut.mockReset().mockResolvedValue(undefined);
  vi.mocked(syncOrder).mockReset();
  vi.spyOn(call, 'post').mockResolvedValue({ message: { source_invoice: 'POS-INV-1', new_invoice: 'POS-INV-2' } } as never);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('captain split bill actions', () => {
  it('hides Split bill when the server permission flag is false', async () => {
    await act(async () => renderMenu(false));
    expect(button('Split bill')).toBeUndefined();
  });

  it('shows Split bill when the server permission flag is true', async () => {
    await act(async () => renderMenu(true));
    expect(button('Split bill')).toBeDefined();
  });

  it.each([
    { splitBill: false, visible: false },
    { splitBill: true, visible: true },
  ])('renders Split bill on CaptainOrder according to permissions.split_bill=$splitBill', async ({ splitBill, visible }) => {
    boundary.context!.permissions = { ...permissions, split_bill: splitBill };
    await renderOrder();
    await act(async () => button('More actions')!.click());

    if (visible) expect(button('Split bill')).toBeDefined();
    else expect(button('Split bill')).toBeUndefined();
  });

  it('submits split_bill for the context invoice and refetches the context', async () => {
    await renderOrder();
    await act(async () => button('More actions')!.click());
    await act(async () => button('Split bill')!.click());

    const item = Array.from(container.querySelectorAll<HTMLElement>('[role="button"]'))
      .find((candidate) => candidate.textContent?.includes('Soup'));
    expect(item).toBeDefined();
    await act(async () => item!.click());
    await act(async () => button('Split bill')!.click());

    expect(call.post).toHaveBeenCalledWith('ury.ury.doctype.ury_order.ury_order.split_bill', {
      source_invoice: 'POS-INV-1',
      items_to_move: [{ name: 'ROW-1', qty: 1 }],
      customer: undefined,
    });
    await vi.waitFor(() => expect(boundary.context.refetchContext).toHaveBeenCalledOnce());
    expect(boundary.posStore!.loadTableOrder).toHaveBeenCalledWith('T-05');
  });

  it('reloads the related-bills panel after a split', async () => {
    boundary.context!.context.order = { ...baseOrder, custom_split_group: 'SPLIT-GROUP-1' };
    boundary.getSplitGroup.mockResolvedValue({
      invoices: [sibling('POS-INV-1', 1), sibling('POS-INV-2', 2)],
      current: 'POS-INV-1',
      group: 'SPLIT-GROUP-1',
    });
    await renderOrder();
    await vi.waitFor(() => expect(boundary.getSplitGroup).toHaveBeenCalledOnce());

    await act(async () => button('More actions')!.click());
    await act(async () => button('Split bill')!.click());
    const item = Array.from(container.querySelectorAll<HTMLElement>('[role="button"]'))
      .find((candidate) => candidate.textContent?.includes('Soup'));
    await act(async () => item!.click());
    await act(async () => button('Split bill')!.click());

    await vi.waitFor(() => expect(boundary.getSplitGroup).toHaveBeenCalledTimes(2));
  });

  it('does not expose sibling Print when print_bill is false', async () => {
    boundary.context!.permissions = { ...permissions, print_bill: false };
    boundary.context!.context.order = { ...baseOrder, custom_split_group: 'SPLIT-GROUP-1' };
    boundary.getSplitGroup.mockResolvedValue({
      invoices: [sibling('POS-INV-1', 1), sibling('POS-INV-2', 2)],
      current: 'POS-INV-1',
      group: 'SPLIT-GROUP-1',
    });
    await renderOrder();
    await vi.waitFor(() => expect(boundary.getSplitGroup).toHaveBeenCalledOnce());

    expect(Array.from(container.querySelectorAll('button')).filter((candidate) => candidate.textContent?.includes('Print'))).toHaveLength(0);
  });

  it('has an Arabic split success translation', async () => {
    await initI18n('ar');
    expect(t('bill_split.split_success', { invoice: 'POS-INV-2' })).toBe('تم إنشاء الفاتورة الجديدة POS-INV-2');
  });

  it('renders sibling checks with one Print button per sibling', async () => {
    boundary.context!.context.order = { ...baseOrder, custom_split_group: 'SPLIT-GROUP-1' };
    boundary.getSplitGroup.mockResolvedValue({
      invoices: [sibling('POS-INV-1', 1), sibling('POS-INV-2', 2), sibling('POS-INV-3', 3)],
      current: 'POS-INV-1',
      group: 'SPLIT-GROUP-1',
    });
    await renderOrder();

    expect(await vi.waitFor(() => container.textContent?.includes('POS-INV-2'))).toBe(true);
    expect(container.textContent).toContain('POS-INV-3');
    expect(container.querySelectorAll('button').length).toBeGreaterThanOrEqual(3);
    expect(Array.from(container.querySelectorAll('button')).filter((candidate) => candidate.textContent?.includes('Print'))).toHaveLength(2);
  });

  it('prints each sibling through printOrder using that sibling invoice', async () => {
    boundary.context!.context.order = { ...baseOrder, custom_split_group: 'SPLIT-GROUP-1' };
    boundary.getSplitGroup.mockResolvedValue({
      invoices: [sibling('POS-INV-1', 1), sibling('POS-INV-2', 2), sibling('POS-INV-3', 3)],
      current: 'POS-INV-1',
      group: 'SPLIT-GROUP-1',
    });
    await renderOrder();
    await vi.waitFor(() => expect(Array.from(container.querySelectorAll('button')).filter((candidate) => candidate.textContent?.includes('Print'))).toHaveLength(2));

    for (const printButton of Array.from(container.querySelectorAll<HTMLButtonElement>('button')).filter((candidate) => candidate.textContent?.includes('Print'))) {
      await act(async () => printButton.click());
    }

    expect(boundary.printOrder).toHaveBeenNthCalledWith(1, expect.objectContaining({ orderId: 'POS-INV-2' }));
    expect(boundary.printOrder).toHaveBeenNthCalledWith(2, expect.objectContaining({ orderId: 'POS-INV-3' }));
  });
});

describe('captain floor-session parity', () => {
  it('signs out after a successful send when the profile enables it', async () => {
    boundary.posStore!.posProfile = {
      ...boundary.posStore!.posProfile,
      floor_signout_after_send: 1,
      custom_floor_stay_signed_in_roles: [],
    };
    vi.mocked(syncOrder).mockResolvedValue({ message: { name: 'POS-INV-1' } } as never);
    await renderOrder();

    await act(async () => button('Send Order')!.click());

    await vi.waitFor(() => expect(boundary.floorSignOut).toHaveBeenCalledExactlyOnceWith());
    expect(container.textContent).not.toContain('tables');
  });

  it.each([
    ['the setting is off', { floor_signout_after_send: 0, custom_floor_stay_signed_in_roles: [] }],
    ['the user is stay-signed-in', { floor_signout_after_send: 1, custom_floor_stay_signed_in_roles: [{ role: 'URY Captain' }] }],
  ])('returns to the tables when %s', async (_case, profile) => {
    boundary.posStore!.posProfile = { ...boundary.posStore!.posProfile, ...profile };
    vi.mocked(syncOrder).mockResolvedValue({ message: { name: 'POS-INV-1' } } as never);
    await renderOrder();

    await act(async () => button('Send Order')!.click());

    await vi.waitFor(() => expect(container.textContent).toContain('tables'));
    expect(boundary.floorSignOut).not.toHaveBeenCalled();
  });

  it('returns to the tables on a device that is not a paired floor tablet', async () => {
    boundary.isFloorPadActive.mockReturnValue(false);
    boundary.posStore!.posProfile = {
      ...boundary.posStore!.posProfile,
      floor_signout_after_send: 1,
      custom_floor_stay_signed_in_roles: [],
    };
    vi.mocked(syncOrder).mockResolvedValue({ message: { name: 'POS-INV-1' } } as never);
    try {
      await renderOrder();

      await act(async () => button('Send Order')!.click());

      await vi.waitFor(() => expect(container.textContent).toContain('tables'));
      expect(boundary.floorSignOut).not.toHaveBeenCalled();
    } finally {
      boundary.isFloorPadActive.mockReturnValue(true);
    }
  });

  it('has translated unsent-items prompt strings in every locale', async () => {
    const keys = ['unsent_items.title', 'unsent_items.description', 'unsent_items.leave'];
    for (const locale of ['en', 'ar', 'fr']) {
      const messages = (await import(`../src/i18n/locales/${locale}.json`)).default as Record<string, Record<string, string>>;
      for (const key of keys) {
        const [group, name] = key.split('.');
        expect(typeof messages[group]?.[name], `${locale} ${key}`).toBe('string');
      }
    }
  });

  it('keeps the waiter order when the send is rejected', async () => {
    boundary.posStore!.posProfile = {
      ...boundary.posStore!.posProfile,
      floor_signout_after_send: 1,
      custom_floor_stay_signed_in_roles: [],
    };
    vi.mocked(syncOrder).mockResolvedValue({ message: { status: 'Failure' } } as never);
    await renderOrder();

    await act(async () => button('Send Order')!.click());

    await vi.waitFor(() => expect(boundary.floorSignOut).not.toHaveBeenCalled());
    expect(container.textContent).not.toContain('tables');
  });

  it('asks before leaving when the waiter has unsent lines and keeps them on cancel', async () => {
    boundary.context!.newOrChangedLines = [{
      uniqueId: 'u3', id: 'ITEM-3', name: 'Cake', price: 50,
      baseQty: 0, curQty: 1, delta: 1, confirmedQty: 0,
    }];
    await renderOrder();

    await act(async () => button('Back to Tables')!.click());
    expect(container.textContent).toContain('Unsent items');
    expect(container.textContent).toContain('You have unsent items. Leave without sending them?');

    await act(async () => button('Cancel')!.click());
    expect(container.textContent).toContain('New / Changed');
    expect(container.textContent).not.toContain('tables');
  });

  it('leaves directly when there are no unsent lines', async () => {
    await renderOrder();

    await act(async () => button('Back to Tables')!.click());

    expect(container.textContent).toContain('tables');
    expect(container.textContent).not.toContain('Unsent items');
  });
});
