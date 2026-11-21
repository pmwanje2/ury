import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import CaptainOrder from '../src/captain/pages/CaptainOrder';
import { syncOrder } from '../src/lib/order-api';

// Theme T4 (fork #70): sign out after a successful send on a paired floor tablet,
// and ask before leaving a table with unsent lines.

const boundary = vi.hoisted(() => ({
  context: null as Record<string, unknown> | null,
  posStore: null as Record<string, unknown> | null,
  floorSignOut: vi.fn(),
  isFloorPadActive: vi.fn(() => true),
  isStaySignedIn: vi.fn((user: { roles: string[] }, profile: { custom_floor_stay_signed_in_roles?: Array<{ role?: string }> }) =>
    user.roles.some((role) => profile.custom_floor_stay_signed_in_roles?.some((row) => row.role === role))),
}));

vi.mock('../src/captain/hooks/useTableOrderContext', () => ({
  useTableOrderContext: () => boundary.context,
}));
vi.mock('../src/store/pos-store', () => ({ usePOSStore: () => boundary.posStore }));
vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector: (state: unknown) => unknown) => selector({
    user: { name: 'waiter@example.com', roles: ['URY Captain'] },
  }),
}));
vi.mock('../src/lib/order-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/order-api')>();
  return { ...actual, syncOrder: vi.fn(), reprintKot: vi.fn(), captainTransfer: vi.fn(), tableTransfer: vi.fn() };
});
vi.mock('../src/lib/print', () => ({ printOrder: vi.fn() }));
vi.mock('../src/lib/table-api', () => ({ getVacantTablesForBranch: vi.fn(async () => []) }));
vi.mock('../src/lib/floor-session', () => ({
  floorSignOut: boundary.floorSignOut,
  isFloorPadActive: boundary.isFloorPadActive,
  isStaySignedIn: boundary.isStaySignedIn,
}));
vi.mock('@ury/ui', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@ury/ui')>();
  return { ...actual, showToast: { error: vi.fn(), success: vi.fn() } };
});
vi.mock('../src/captain/components/CaptainMenu', () => ({ default: () => null }));
vi.mock('../src/captain/components/CaptainOrderLine', () => ({ default: () => null }));
vi.mock('../src/captain/components/CaptainActionsMenu', () => ({ default: () => null }));
vi.mock('../src/components/CommentDialog', () => ({ default: () => null }));
vi.mock('../src/components/CustomerSelect', () => ({ CustomerSelect: () => null }));
vi.mock('../src/components/ProductDialog', () => ({ default: () => null }));
vi.mock('../src/components/TableTransferDialog', () => ({ default: () => null }));
vi.mock('../src/components/CaptainTransferDialog', () => ({ default: () => null }));

const permissions = { view: true, modify: true, reduce_items: true, remove_items: true };

let container: HTMLDivElement;
let root: Root;

async function renderOrder() {
  await act(async () => root.render(
    <MemoryRouter initialEntries={['/order/table/T-05']}>
      <Routes>
        <Route path="/order/table/:table" element={<CaptainOrder />} />
        <Route path="/order" element={<div>tables page</div>} />
      </Routes>
    </MemoryRouter>,
  ));
}

function button(label: string) {
  return Array.from(container.querySelectorAll<HTMLButtonElement>('button'))
    .find((candidate) => candidate.textContent?.trim() === label || candidate.getAttribute('aria-label') === label);
}

function withProfile(profile: Record<string, unknown>) {
  boundary.posStore!.posProfile = { ...(boundary.posStore!.posProfile as object), ...profile };
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  boundary.context = {
    context: { table: { name: 'T-05', restaurant_room: 'Dining' }, order: null, assignment: { waiter: 'waiter@example.com' } },
    permissions,
    isContextLoading: false,
    contextError: null,
    isOrderReady: true,
    alreadyOrderedLines: [],
    newOrChangedLines: [],
    reductionPendingLines: [],
  };
  boundary.posStore = {
    activeOrders: [{ id: 'ITEM-1', name: 'Soup', price: 100, quantity: 1, uniqueId: 'u1' }],
    removeFromOrder: vi.fn(), updateQuantity: vi.fn(), updateItemComment: vi.fn(), setSelectedItem: vi.fn(),
    isUpdatingOrder: false, orderId: null, loadTableOrder: vi.fn(async () => {}),
    posProfile: { name: 'Floor POS', branch: 'Branch A', print_format: 'Receipt', cashier: 'c', owner: 'o' },
    paymentModes: ['Cash'], orderComment: '', noOfPax: 2, setNoOfPax: vi.fn(), lastModifiedTime: null,
    selectedRoom: 'Dining', selectedCustomer: { id: 'Walk In', name: 'Walk In' }, clearTableOrder: vi.fn(),
    isOrderInteractionDisabled: () => false, selectedOrderType: 'Dine In', setSelectedOrderType: vi.fn(),
  };
  boundary.floorSignOut.mockReset().mockResolvedValue(undefined);
  boundary.isFloorPadActive.mockReset().mockReturnValue(true);
  vi.mocked(syncOrder).mockReset();
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

describe('captain floor-session parity', () => {
  it('signs out after a successful send when the profile enables it', async () => {
    withProfile({ floor_signout_after_send: 1, custom_floor_stay_signed_in_roles: [] });
    vi.mocked(syncOrder).mockResolvedValue({ message: { name: 'POS-INV-1' } } as never);
    await renderOrder();

    await act(async () => button('Send Order')!.click());

    await vi.waitFor(() => expect(boundary.floorSignOut).toHaveBeenCalledExactlyOnceWith());
    expect(container.textContent).not.toContain('tables page');
  });

  it.each([
    ['the setting is off', { floor_signout_after_send: 0, custom_floor_stay_signed_in_roles: [] }],
    ['the user is stay-signed-in', { floor_signout_after_send: 1, custom_floor_stay_signed_in_roles: [{ role: 'URY Captain' }] }],
  ])('returns to the tables when %s', async (_case, profile) => {
    withProfile(profile);
    vi.mocked(syncOrder).mockResolvedValue({ message: { name: 'POS-INV-1' } } as never);
    await renderOrder();

    await act(async () => button('Send Order')!.click());

    await vi.waitFor(() => expect(container.textContent).toContain('tables page'));
    expect(boundary.floorSignOut).not.toHaveBeenCalled();
  });

  it('returns to the tables on a device that is not a paired floor tablet', async () => {
    boundary.isFloorPadActive.mockReturnValue(false);
    withProfile({ floor_signout_after_send: 1, custom_floor_stay_signed_in_roles: [] });
    vi.mocked(syncOrder).mockResolvedValue({ message: { name: 'POS-INV-1' } } as never);
    await renderOrder();

    await act(async () => button('Send Order')!.click());

    await vi.waitFor(() => expect(container.textContent).toContain('tables page'));
    expect(boundary.floorSignOut).not.toHaveBeenCalled();
  });

  it('keeps the waiter order when the send is rejected', async () => {
    withProfile({ floor_signout_after_send: 1, custom_floor_stay_signed_in_roles: [] });
    vi.mocked(syncOrder).mockResolvedValue({ message: { status: 'Failure' } } as never);
    await renderOrder();

    await act(async () => button('Send Order')!.click());

    await vi.waitFor(() => expect(syncOrder).toHaveBeenCalledOnce());
    expect(boundary.floorSignOut).not.toHaveBeenCalled();
    expect(container.textContent).not.toContain('tables page');
  });

  it('asks before leaving when the waiter has unsent lines and keeps them on cancel', async () => {
    boundary.context!.newOrChangedLines = [{
      uniqueId: 'u3', id: 'ITEM-3', name: 'Cake', price: 50, baseQty: 0, curQty: 1, delta: 1, confirmedQty: 0,
    }];
    await renderOrder();

    await act(async () => button('Back to Tables')!.click());
    expect(container.textContent).toContain('Unsent items');
    expect(container.textContent).toContain('You have unsent items. Leave without sending them?');

    await act(async () => button('Cancel')!.click());
    expect(container.textContent).not.toContain('Unsent items');
    expect(container.textContent).toContain('New / Changed');
    expect(container.textContent).not.toContain('tables page');

    await act(async () => button('Back to Tables')!.click());
    await act(async () => button('Leave')!.click());
    expect(container.textContent).toContain('tables page');
  });

  it('leaves directly when there are no unsent lines', async () => {
    await renderOrder();

    await act(async () => button('Back to Tables')!.click());

    expect(container.textContent).toContain('tables page');
    expect(container.textContent).not.toContain('Unsent items');
  });

  it('has translated unsent-items prompt strings in every locale', async () => {
    for (const locale of ['en', 'ar', 'fr', 'es']) {
      const messages = (await import(`../src/i18n/locales/${locale}.json`)).default as Record<string, Record<string, string>>;
      for (const key of ['title', 'description', 'leave']) {
        expect(typeof messages.unsent_items?.[key], `${locale} unsent_items.${key}`).toBe('string');
      }
      expect(typeof messages.header?.switch_waiter, `${locale} header.switch_waiter`).toBe('string');
    }
  });
});
