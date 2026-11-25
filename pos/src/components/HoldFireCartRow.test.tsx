import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { initI18n } from '../i18n';
import { usePOSStore, type OrderItem } from '../store/pos-store';
import { HoldFireControls } from './HoldFireControls';

vi.mock('@ury/core', async (original) => ({
  ...await original<object>(),
  call: { get: vi.fn(), post: vi.fn() },
}));
vi.mock('../lib/realtime', () => ({ getRealtimeSocket: () => Promise.resolve({
  on: vi.fn(), off: vi.fn(), emit: vi.fn(), connected: true,
}) }));

it('fires from its cart row and updates the saved kitchen baseline while preserving unsaved edits', async () => {
  await initI18n('en');
  const held = {
    id: 'Fish', item: 'Fish', name: 'Fish', uniqueId: 'fish', invoiceItemName: 'held',
    reservationLineKey: 'fish', quantity: 2, price: 100, course: 'Main', ury_held: 1,
  } as OrderItem;
  const draft = {
    id: 'Tea', item: 'Tea', name: 'Tea', uniqueId: 'tea', quantity: 1, price: 50,
  } as OrderItem;
  usePOSStore.setState({
    activeOrders: [held, draft],
    originalSentItems: [{
      item_code: 'Fish', item_name: 'Fish', reservation_line_key: 'fish',
      qty: 2, rate: 100, comment: '', ury_held: 1,
    }],
    orderId: 'check', lastModifiedTime: 'version-1', orderLoading: false,
    posProfile: { ury_enable_hold_fire: 1 } as never,
  });
  let finishRequest!: (response: unknown) => void;
  vi.mocked(call.post).mockImplementation(() => new Promise(resolve => { finishRequest = resolve; }) as never);
  const view = render(<>
    <HoldFireControls />
    <section aria-label="Fish cart row"><h2>Fish</h2><HoldFireControls lineId="fish" /></section>
    <section aria-label="Tea cart row"><h2>Tea</h2><HoldFireControls lineId="tea" /></section>
  </>);
  try {
    const fish = screen.getByRole('region', { name: 'Fish cart row' });
    const tea = screen.getByRole('region', { name: 'Tea cart row' });
    expect(screen.getAllByRole('heading', { name: 'Fish', exact: true })).toHaveLength(1);
    expect(within(tea).getByRole('checkbox', { name: 'Hold for kitchen' })).not.toBeChecked();
    fireEvent.click(within(fish).getByRole('button', { name: 'Fire now' }));
    await waitFor(() => expect(call.post).toHaveBeenCalledOnce());
    await act(async () => {
      usePOSStore.getState().updateItemComment('fish', 'New unsaved note');
      await usePOSStore.getState().updateQuantity('tea', 3);
      finishRequest({ message: {
        name: 'check', modified: 'version-2', held_update_previous_modified: 'version-1',
        items: [{ name: 'held', reservation_line_key: 'fish', ury_held: 0 }],
      } });
    });
    const state = usePOSStore.getState();
    expect(state.activeOrders.map(row => [row.uniqueId, row.quantity, row.comment])).toEqual([
      ['fish', 2, 'New unsaved note'], ['tea', 3, undefined],
    ]);
    expect(state.lastModifiedTime).toBe('version-2');
    expect(state.originalSentItems[0]).toMatchObject({ qty: 2, comment: '', ury_held: 0 });
    expect(within(fish).queryByRole('button', { name: 'Fire now' })).toBeNull();
  } finally {
    view.unmount();
  }
});
