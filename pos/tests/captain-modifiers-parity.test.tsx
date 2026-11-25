import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import CaptainOrder from '../src/captain/pages/CaptainOrder';
import CaptainOrderLine from '../src/captain/components/CaptainOrderLine';
import { usePOSStore, type OrderItem } from '../src/store/pos-store';
import { useRootStore } from '../src/store/root-store';
import type { BillSplitItem } from '../src/components/BillSplitDialog';
import type { OrderDeltaLine } from '../src/captain/hooks/useTableOrderContext';

let capturedSplitItems: BillSplitItem[] = [];

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
  useParams: () => ({ table: 'T-3' }),
}));

vi.mock('../src/lib/table-order-context-api', () => ({
  getTableOrderContext: vi.fn().mockResolvedValue({
    table: { name: 'T-3', restaurant_room: 'Main Dining', is_take_away: 0 },
    order: {
      name: 'MB-00099',
      customer: 'Walk-In Customer',
      customer_name: 'Walk-In Customer',
      order_type: 'Dine In',
    },
    assignment: { waiter: 'captain@maximus.test' },
    permissions: {
      view: true,
      modify: true,
      reduce_items: true,
      remove_items: true,
      split_bill: true,
      print_bill: true,
      reprint_kot: true,
      transfer_table: true,
      transfer_captain: true,
    },
  }),
}));

vi.mock('../src/offline/use-online', () => ({
  useOnline: () => true,
}));

vi.mock('../src/components/BillSplitDialog', () => ({
  default: ({ items }: { items: BillSplitItem[] }) => {
    capturedSplitItems = items;
    return <div data-testid="captured-bill-split" data-count={String(items.length)} />;
  },
}));

vi.mock('../src/captain/components/CaptainMenu', () => ({
  default: () => <div data-testid="captain-menu" />,
}));

vi.mock('../src/components/CustomerSelect', () => ({
  CustomerSelect: () => <div data-testid="customer-select" />,
}));

vi.mock('../src/components/HoldFireControls', () => ({
  HoldFireControls: () => null,
}));

vi.mock('../src/components/ProductDialog', () => ({
  default: () => null,
}));

describe('Captain Split & Mobile Cart Modifier Parity (R1 M3)', () => {
  beforeEach(() => {
    capturedSplitItems = [];
    useRootStore.setState({
      user: { name: 'captain@maximus.test', roles: ['URY Captain'], full_name: 'Captain' } as any,
    });
  });

  it('CaptainOrderLine nests modifier child with + Extra prefix and renders zero +/- controls', () => {
    const parentLine: OrderDeltaLine = {
      uniqueId: 'p-rib',
      id: 'RIBEYE',
      name: 'Ribeye Steak',
      price: 45000,
      baseQty: 0,
      curQty: 1,
      delta: 1,
      confirmedQty: 0,
      reservationLineKey: 'k-rib',
      isModifier: false,
      selectedModifiers: [
        { group: 'Cooking Temperature', option_code: 'MED-RARE', label: 'Medium Rare', price_mode: 'Unpriced Prep' },
      ],
    };
    const childLine: OrderDeltaLine = {
      uniqueId: 'c-ch',
      id: 'EXTRA-CHEESE',
      name: 'Aged Cheddar',
      price: 4000,
      baseQty: 0,
      curQty: 1,
      delta: 1,
      confirmedQty: 0,
      reservationLineKey: 'k-ch',
      parentLineKey: 'k-rib',
      isModifier: true,
      modifierPrefix: 'Extra',
    };

    render(
      <div>
        <CaptainOrderLine
          line={parentLine}
          variant="delta"
          currency="UGX"
          onIncrement={vi.fn()}
          onDecrement={vi.fn()}
        />
        <CaptainOrderLine
          line={childLine}
          variant="delta"
          currency="UGX"
          onIncrement={vi.fn()}
          onDecrement={vi.fn()}
        />
      </div>,
    );

    // Parent shows unpriced modifier summary pill pre-send
    expect(screen.getByText('Medium Rare')).toBeInTheDocument();

    // Child renders indented with "+ Extra Aged Cheddar" and ZERO independent -/+ buttons
    const childEl = screen.getByTestId('captain-modifier-child-c-ch');
    expect(childEl).toHaveTextContent('+ Extra Aged Cheddar');
    expect(within(childEl).queryByRole('button', { name: /increase/i })).not.toBeInTheDocument();
    expect(within(childEl).queryByRole('button', { name: /decrease/i })).not.toBeInTheDocument();
  });

  it('CaptainOrder passes modifier linkage fields to BillSplitDialog and nests modifiers in Mobile cart', async () => {
    const loadedOrders: OrderItem[] = [
      {
        id: 'RIBEYE',
        item: 'RIBEYE',
        name: 'Ribeye Steak',
        item_name: 'Ribeye Steak',
        image: null,
        price: 45000,
        quantity: 1,
        course: 'Mains',
        special_dish: 0,
        tax_rate: 0,
        uniqueId: 'row-rib',
        invoiceItemName: 'row-rib',
        reservationLineKey: 'key-rib',
        isModifier: false,
        selectedModifiers: [
          { group: 'Cooking Temperature', option_code: 'MED-RARE', label: 'Medium Rare', price_mode: 'Unpriced Prep' },
        ],
      },
      {
        id: 'EXTRA-CHEESE',
        item: 'EXTRA-CHEESE',
        name: 'Aged Cheddar',
        item_name: 'Aged Cheddar',
        image: null,
        price: 4000,
        quantity: 1,
        course: 'Mains',
        special_dish: 0,
        tax_rate: 0,
        uniqueId: 'row-ch',
        invoiceItemName: 'row-ch',
        reservationLineKey: 'key-ch',
        parentLineKey: 'key-rib',
        isModifier: true,
        lineRole: 'Modifier',
        modifierPrefix: 'Extra',
      },
    ];

    usePOSStore.setState({
      orderLoading: false,
      activeOrders: loadedOrders,
      orderId: 'MB-00099',
      isUpdatingOrder: true,
      currency: 'UGX',
      posProfile: {
        name: 'Main POS',
        branch: 'Main',
        currency: 'UGX',
        cashier: 'cashier@maximus.test',
        owner: 'owner@maximus.test',
      } as any,
      setSelectedTable: vi.fn(() => {
        // Trigger falling edge of orderLoading so useTableOrderContext captures baseline
        usePOSStore.setState({ orderLoading: true });
        queueMicrotask(() => {
          usePOSStore.setState({ orderLoading: false, activeOrders: loadedOrders });
        });
      }),
      loadTableOrder: vi.fn().mockResolvedValue(undefined),
      clearTableOrder: vi.fn(),
    });

    render(<CaptainOrder />);

    const splitHost = await screen.findByTestId('captured-bill-split');
    expect(splitHost).toHaveAttribute('data-count', '2');

    // BillSplitDialog MUST receive linkage fields for atomic parent+child split
    expect(capturedSplitItems[0]).toMatchObject({
      name: 'row-rib',
      reservation_line_key: 'key-rib',
      custom_is_modifier: 0,
    });
    expect(capturedSplitItems[1]).toMatchObject({
      name: 'row-ch',
      reservation_line_key: 'key-ch',
      custom_parent_line_key: 'key-rib',
      custom_is_modifier: 1,
      custom_modifier_prefix: 'Extra',
    });

    // Mobile & Tablet order view nests child modifier under parent without independent reduce/remove controls
    const nestedChildren = await screen.findAllByTestId('captain-modifier-child-row-ch');
    expect(nestedChildren.length).toBeGreaterThan(0);
    expect(nestedChildren[0]).toHaveTextContent('+ Extra Aged Cheddar');
    expect(within(nestedChildren[0]).queryByRole('button')).not.toBeInTheDocument();
  });
});
