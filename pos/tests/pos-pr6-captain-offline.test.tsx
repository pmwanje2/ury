import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import CaptainTables from '../src/captain/pages/CaptainTables';

const mockGetCaptainContext = vi.fn();
const mockGetRooms = vi.fn();
const mockGetTables = vi.fn();
const mockUseOutbox = vi.fn();
const mockPosStoreState = vi.fn();
const mockRootStoreState = vi.fn();

vi.mock('../src/lib/captain-context-api', () => ({
  getCaptainContext: () => mockGetCaptainContext(),
}));

vi.mock('../src/lib/table-api', () => ({
  getRooms: (...args: unknown[]) => mockGetRooms(...args),
  getTables: (...args: unknown[]) => mockGetTables(...args),
  checkTableReservation: vi.fn().mockResolvedValue(null),
}));

vi.mock('../src/captain/lib/captain-table-api', () => ({
  getActiveTableOrders: vi.fn().mockRejectedValue(new Error('Offline')),
  getUpcomingBookings: vi.fn().mockRejectedValue(new Error('Offline')),
  getUserFullNames: vi.fn().mockResolvedValue(new Map()),
}));

vi.mock('../src/offline/use-outbox', () => ({
  useOutbox: (...args: unknown[]) => mockUseOutbox(...args),
}));

vi.mock('../src/store/pos-store', () => ({
  usePOSStore: (selector?: (s: any) => unknown) => {
    const state = mockPosStoreState();
    return typeof selector === 'function' ? selector(state) : state;
  },
}));

vi.mock('../src/store/root-store', () => {
  const hook: any = (selector?: (s: any) => unknown) => {
    const state = mockRootStoreState();
    return typeof selector === 'function' ? selector(state) : state;
  };
  hook.getState = () => mockRootStoreState();
  return { useRootStore: hook };
});

vi.mock('../src/components/ServiceRequestAlert', () => ({
  useOpenServiceRequestTables: () => new Map(),
}));

vi.mock('../src/components/Notifications', () => ({
  default: () => <div data-testid="notifications-stub" />,
}));

vi.mock('../src/captain/components/CaptainTableCard', () => ({
  default: ({ table, onTap }: any) => (
    <button type="button" onClick={onTap} data-testid={`table-${table.name}`}>
      {table.name}
    </button>
  ),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

vi.mock('@ury/ui', () => ({
  Button: ({ children, onClick, ...props }: any) => (
    <button onClick={onClick} {...props}>
      {children}
    </button>
  ),
  Spinner: ({ message }: any) => <div>{message}</div>,
  showToast: {
    error: vi.fn(),
    info: vi.fn(),
  },
}));

describe('POS-PR-6 R2 (Finding 6): CaptainTables offline cached fallback', () => {
  const origOnline = Object.getOwnPropertyDescriptor(navigator, 'onLine');

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
    mockRootStoreState.mockReturnValue({
      user: { name: 'waiter1@hotel.test', roles: ['URY Captain'] },
    });
    mockPosStoreState.mockReturnValue({
      currency: 'UGX',
      posProfile: {
        name: 'Main Till',
        currency: 'UGX',
        ury_offline_capture: 1,
      },
    });
    mockUseOutbox.mockReturnValue({
      rows: [],
      offline: false,
      replaying: false,
      queue: null,
    });
  });

  afterEach(() => {
    if (origOnline) Object.defineProperty(navigator, 'onLine', origOnline);
    else Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
  });

  it('renders cached tables, offline pill, and queued badge when returning to CaptainTables offline', async () => {
    // Phase 1 (online): warm cache by rendering CaptainTables successfully
    mockGetCaptainContext.mockResolvedValueOnce({
      user: 'waiter1@hotel.test',
      roles: ['URY Captain'],
      branch: 'Kampala Branch',
      rooms: [{ name: 'Main Hall', branch: 'Kampala Branch' }],
      pos_profile: {
        name: 'Main Till',
        transfer_role_permissions: false,
        role_allowed_for_billing: false,
        role_restricted_for_table_order: false,
        remove_items: false,
        show_image: false,
        custom_enable_kot_reprint: false,
        custom_enable_multiple_cashier: false,
      },
      role_restricted_for_table_order: false,
      opening_state: { pos_open: true },
      reservation_enabled: false,
    });
    mockGetRooms.mockResolvedValueOnce([{ name: 'Main Hall', branch: 'Kampala Branch' }]);
    mockGetTables.mockResolvedValueOnce([
      { name: 'T5', occupied: 0, latest_invoice_time: null, is_take_away: 0, restaurant_room: 'Main Hall', table_shape: 'Square' },
      { name: 'T6', occupied: 0, latest_invoice_time: null, is_take_away: 0, restaurant_room: 'Main Hall', table_shape: 'Square' },
    ]);

    const firstRender = render(<CaptainTables />);
    await waitFor(() => {
      expect(screen.getByTestId('table-T5')).toBeInTheDocument();
      expect(screen.getByTestId('table-T6')).toBeInTheDocument();
    });
    firstRender.unmount();

    // Phase 2 (offline after submitting an offline order): API calls fail with network error
    const consoleErrSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: false });
    mockGetCaptainContext.mockRejectedValueOnce(new Error('There was an error.'));
    mockGetRooms.mockRejectedValueOnce(new Error('There was an error.'));
    mockGetTables.mockRejectedValueOnce(new Error('There was an error.'));
    mockUseOutbox.mockReturnValue({
      rows: [
        {
          owner: 'waiter1@hotel.test',
          profile: 'Main Till',
          client_order_id: 'offline-order-uuid-1',
          outage_id: 'outage-1',
          order: { pos_profile: 'Main Till', items: [], no_of_pax: 1, invoice: null, last_invoice: null, order_type: 'Dine In', table: 'T5' },
          rounds: [],
          state: 'pending',
          next_round: 0,
          phase: 'order',
        },
      ],
      offline: true,
      replaying: false,
      queue: null,
    });

    render(<CaptainTables />);

    await waitFor(() => {
      expect(screen.getByTestId('table-T5')).toBeInTheDocument();
      expect(screen.getByTestId('table-T6')).toBeInTheDocument();
    });
    expect(screen.queryByText(/Unable to load tables/i)).not.toBeInTheDocument();
    expect(screen.getByTestId('captain-offline-pill')).toBeInTheDocument();
    expect(screen.getByTestId('captain-queued-badge')).toBeInTheDocument();
    expect(consoleErrSpy).not.toHaveBeenCalled();
    consoleErrSpy.mockRestore();
  });
});
