import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import * as controls from './HoldFireControls';

const { state, scheduleItems } = vi.hoisted(() => ({
  state: {
    activeOrders: [
      { uniqueId: 'starter', invoiceItemName: 'sent', name: 'Tomato soup', course: 'Starter' },
      { uniqueId: 'fish', invoiceItemName: 'held', name: 'Grilled fish', course: 'Main', ury_held: 1, quantity: 2 },
    ],
    posProfile: { ury_enable_hold_fire: 1, default_fire_delay_minutes: 15 },
    orderId: 'check',
    lastModifiedTime: 'version',
    orderLoading: false,
  },
  scheduleItems: vi.fn(),
}));

vi.mock('../store/pos-store', () => ({
  usePOSStore: Object.assign((selector?: (value: typeof state) => unknown) => selector ? selector(state) : state, {
    setState: vi.fn(),
  }),
}));
vi.mock('../offline/use-online', () => ({ useOnline: () => true }));
vi.mock('../lib/order-api', () => ({ scheduleItems, fireItems: vi.fn() }));
vi.mock('../i18n', () => ({ t: (key: string) => key }));

describe('Hold and fire', () => {
  beforeEach(() => vi.clearAllMocks());

  it('keeps item names and quantities in the cart alone', () => {
    render(<controls.HoldFireControls />);
    expect(screen.queryByText(/Grilled fish|Tomato soup/)).not.toBeInTheDocument();
  });

  it('schedules a saved course using the configured initial delay', async () => {
    scheduleItems.mockResolvedValue({ message: { name: 'check', items: [] } });
    render(<controls.HoldFireControls />);
    fireEvent.click(screen.getByRole('button', { name: 'hold_fire.schedule_course' }));
    await waitFor(() => expect(scheduleItems).toHaveBeenCalledWith('check', {
      course: 'Main', delay_minutes: 15, last_modified_time: 'version',
    }));
  });
});
