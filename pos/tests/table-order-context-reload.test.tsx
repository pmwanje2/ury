import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { usePOSStore } from '../src/store/pos-store';
import useTableOrderContext from '../src/captain/hooks/useTableOrderContext';

const order = (quantity: number) => ({
  name: 'POS-INV-1',
  customer: 'Walk In',
  customer_name: 'Walk In',
  mobile_number: '',
  order_type: 'Dine In',
  grand_total: 100 * quantity,
  no_of_pax: 1,
  custom_restaurant_room: 'Dining',
  items: [{
    name: 'ROW-1',
    item_code: 'FOOD',
    item_name: 'Soup',
    qty: quantity,
    amount: 100 * quantity,
    rate: 100,
  }],
});

const context = {
  table: { name: 'T-05', branch: 'Branch A', restaurant_room: 'Dining' },
  order: order(2),
  assignment: { waiter: 'waiter@example.com', is_mine: true },
  permissions: { view: true },
};

let container: HTMLDivElement;
let root: Root;
let reloadOrder: (() => Promise<void>) | undefined;

function Probe() {
  const result = useTableOrderContext('T-05');
  const activeOrders = usePOSStore((state) => state.activeOrders);
  reloadOrder = result.reloadOrder;
  return (
    <output data-testid="reload-state">
      {JSON.stringify({
        ready: result.isOrderReady,
        quantity: activeOrders[0]?.quantity ?? null,
        reductions: result.reductionPendingLines.map((line) => line.delta),
      })}
    </output>
  );
}

function state() {
  return JSON.parse(container.querySelector('[data-testid="reload-state"]')!.textContent!);
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  document.cookie = 'user_id=waiter%40example.com';
  usePOSStore.setState({
    activeOrders: [],
    orderLoading: false,
    selectedTable: null,
    selectedRoom: null,
    tableOrder: null,
    posProfile: null,
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
  usePOSStore.getState().clearTableOrder();
  usePOSStore.setState({ orderLoading: false, activeOrders: [] });
  sessionStorage.clear();
  document.cookie = 'user_id=; Max-Age=0';
});

describe('useTableOrderContext reloadOrder', () => {
  it('loads the current table order and recaptures its baseline after a split', async () => {
    const orderResponses = [order(2), order(1)];
    const get = vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
      if (method === 'ury.ury.doctype.ury_order.ury_order.get_table_order_context') {
        return { message: context } as never;
      }
      if (method === 'ury.ury.doctype.ury_order.ury_order.get_order_invoice') {
        return { message: orderResponses.shift() } as never;
      }
      throw new Error(`Unexpected HTTP method: ${method}`);
    });

    await act(async () => root.render(<Probe />));
    await vi.waitFor(() => expect(state()).toMatchObject({ ready: true, quantity: 2 }));

    await act(async () => {
      await reloadOrder!();
    });

    await vi.waitFor(() => expect(state()).toMatchObject({ ready: true, quantity: 1, reductions: [] }));
    expect(get.mock.calls.filter(([method]) => method === 'ury.ury.doctype.ury_order.ury_order.get_order_invoice')).toHaveLength(2);
  });
});
