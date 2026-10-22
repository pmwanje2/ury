import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { showToast } from '@ury/ui';
import { initI18n } from '../src/i18n';
import CaptainOrder from '../src/captain/pages/CaptainOrder';

const state = vi.hoisted(() => ({
  activeOrders: [{ id: 'TEA', uniqueId: 'ROW-1', name: 'Tea', price: 100, quantity: 1 }],
  posProfile: { name: 'POS-1', cashier: 'Cashier', owner: 'Owner', customer: 'Native Walk In' as string | undefined },
  selectedCustomer: null as { name: string } | null,
  selectedRoom: 'Dining',
  paymentModes: ['Cash'],
  orderComment: '',
  noOfPax: 2,
  lastModifiedTime: '2026-10-22 10:00:00',
  orderId: null as string | null,
  isUpdatingOrder: false,
  user: { name: 'waiter@example.com' } as { name: string } | null,
  offline: false,
  clearTableOrder: vi.fn(),
  setNoOfPax: vi.fn(),
  setSelectedItem: vi.fn(),
  removeFromOrder: vi.fn(),
  updateQuantity: vi.fn(),
  updateItemComment: vi.fn(),
}));

const syncOrder = vi.hoisted(() => vi.fn());

vi.mock('../src/store/pos-store', () => ({
  usePOSStore: () => ({
    ...state,
    isOrderInteractionDisabled: () => false,
  }),
}));
vi.mock('../src/store/root-store', () => ({
  useRootStore: (select: (value: { user: typeof state.user }) => unknown) => select({ user: state.user }),
}));
vi.mock('../src/captain/hooks/useTableOrderContext', () => ({
  useTableOrderContext: () => ({
    context: { order: null, assignment: null },
    permissions: {
      view: true,
      modify: true,
      reduce_items: false,
      remove_items: false,
      reprint_kot: false,
      transfer_table: false,
      transfer_captain: false,
      print_bill: false,
      split_bill: false,
    },
    isContextLoading: false,
    contextError: null,
    isOrderReady: true,
    alreadyOrderedLines: [],
    newOrChangedLines: [],
    reductionPendingLines: [],
    refetchContext: vi.fn(),
    reloadOrder: vi.fn(),
  }),
}));
vi.mock('../src/offline/use-outbox', () => ({
  useOutbox: () => ({ offline: state.offline, rows: [], replaying: false, queue: null }),
}));
vi.mock('../src/lib/order-api', () => ({
  captainTransfer: vi.fn(),
  getCaptainTransferCandidates: vi.fn(),
  reprintKot: vi.fn(),
  splitBill: vi.fn(),
  syncOrder,
  tableTransfer: vi.fn(),
}));
vi.mock('../src/lib/print', () => ({ printOrder: vi.fn() }));
vi.mock('../src/lib/invoice-api', () => ({ resolvePrintFormat: vi.fn(), SplitGroupInvoice: {} }));
vi.mock('../src/lib/table-api', () => ({ getVacantTablesForBranch: vi.fn() }));
vi.mock('../src/lib/floor-session', () => ({ floorSignOut: vi.fn(), isFloorPadActive: vi.fn(() => false), isStaySignedIn: vi.fn(() => false) }));
vi.mock('../src/captain/components/CaptainMenu', () => ({ default: () => null }));
vi.mock('../src/captain/components/CaptainOrderLine', () => ({ default: () => null }));
vi.mock('../src/captain/components/CaptainActionsMenu', () => ({ default: () => null }));
vi.mock('../src/components/ProductDialog', () => ({ default: () => null }));
vi.mock('../src/components/CommentDialog', () => ({ default: () => null }));
vi.mock('../src/components/TableTransferDialog', () => ({ default: () => null }));
vi.mock('../src/components/CaptainTransferDialog', () => ({ default: () => null }));
vi.mock('../src/components/CustomerSelect', () => ({ CustomerSelect: () => null }));
vi.mock('../src/components/HoldFireControls', () => ({ HoldFireControls: () => null }));
vi.mock('../src/components/BillSplitDialog', () => ({ default: () => null }));
vi.mock('../src/components/SplitGroupPanel', () => ({ default: () => null }));

let host: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  await initI18n('en');
  Object.assign(state, {
    selectedCustomer: null,
    offline: false,
    user: { name: 'waiter@example.com' },
    posProfile: { name: 'POS-1', cashier: 'Cashier', owner: 'Owner', customer: 'Native Walk In' },
  });
  vi.clearAllMocks();
  vi.mocked(syncOrder).mockResolvedValue({ message: { name: 'INV-1' } });
  vi.spyOn(showToast, 'error').mockImplementation(() => {});
  vi.spyOn(showToast, 'success').mockImplementation(() => {});
  host = document.createElement('div');
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function render() {
  await act(async () => root.render(
    <MemoryRouter initialEntries={['/order/table/T-1']}>
      <Routes><Route path="/order/table/:table" element={<CaptainOrder />} /></Routes>
    </MemoryRouter>,
  ));
}

async function submit() {
  await render();
  const button = [...host.querySelectorAll('button')].find((candidate) => candidate.textContent?.includes('Send Order'));
  expect(button).toBeDefined();
  await act(async () => button!.click());
}

describe('Captain Dine In walk-in customer resolution', () => {
  it('sends an explicit empty customer online when no customer is selected', async () => {
    await submit();

    expect(syncOrder).toHaveBeenCalledWith(expect.objectContaining({ order_type: 'Dine In', table: 'T-1', customer: '' }));
    expect(showToast.error).not.toHaveBeenCalledWith(expect.stringMatching(/select a customer/i));
  });

  it('uses the POS profile customer offline when no customer is selected', async () => {
    state.offline = true;

    await submit();

    expect(syncOrder).toHaveBeenCalledWith(expect.objectContaining({ order_type: 'Dine In', table: 'T-1', customer: 'Native Walk In' }));
  });

  it('keeps the existing refusal when offline has no profile customer', async () => {
    state.offline = true;
    state.posProfile.customer = undefined;

    await submit();

    expect(syncOrder).not.toHaveBeenCalled();
    expect(showToast.error).toHaveBeenCalledWith(expect.stringMatching(/select a customer/i));
  });
});
