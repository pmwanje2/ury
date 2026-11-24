import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import en from '../i18n/locales/en.json';
import fr from '../i18n/locales/fr.json';
import es from '../i18n/locales/es.json';
import ar from '../i18n/locales/ar.json';

const callGetMock = vi.fn();
const callPostMock = vi.fn();
const showToastSuccess = vi.fn();
const showToastError = vi.fn();
const loadTableOrderMock = vi.fn().mockResolvedValue(undefined);

vi.mock('@ury/core', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@ury/core');
  return {
    ...actual,
    call: {
      get: (...args: unknown[]) => callGetMock(...args),
      post: (...args: unknown[]) => callPostMock(...args),
    },
    formatCurrency: (val: number, cur = 'UGX') => `${cur} ${Number(val || 0).toLocaleString()}`,
    parseFrappeError: (err: unknown, fallback = 'Error') =>
      err instanceof Error ? err.message : typeof err === 'string' ? err : fallback,
  };
});

vi.mock('@ury/ui', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@ury/ui');
  return {
    ...actual,
    showToast: {
      success: (...args: unknown[]) => showToastSuccess(...args),
      error: (...args: unknown[]) => showToastError(...args),
      info: vi.fn(),
    },
  };
});

vi.mock('./Sidebar', () => ({ default: () => <div data-testid="sidebar" /> }));
vi.mock('./OrderPanel', () => ({ default: () => <div data-testid="order-panel" /> }));
vi.mock('./ProductDialog', () => ({ default: () => null }));
vi.mock('./MenuList', () => ({
  default: ({ onItemClick }: { onItemClick?: (item: unknown) => void }) => (
    <div data-testid="menu-list">
      <button
        type="button"
        data-testid="menu-open-item-card"
        onClick={() =>
          onItemClick?.({
            id: 'OPEN-MARKET-FISH',
            item: 'OPEN-MARKET-FISH',
            name: 'Market Catch Open Item',
            item_name: 'Market Catch Open Item',
            price: 0,
            custom_is_open_priced: 1,
          })
        }
      >
        Market Catch Open Item
      </button>
    </div>
  ),
}));
vi.mock('./InitialLoader', () => ({ default: () => <div /> }));
vi.mock('./SoldOutSheet', () => ({ SoldOutSheet: () => null }));
vi.mock('./ManagerApproval', () => ({
  default: ({ open, privilege, onApproved }: { open: boolean; privilege: string; onApproved: (r: unknown) => void }) =>
    open ? (
      <div data-testid="mock-manager-approval" data-privilege={privilege}>
        <button
          type="button"
          data-testid="mock-approve-pin"
          onClick={() =>
            onApproved({
              token: 'TOK-OPEN-UI',
              approver: 'manager@example.com',
              approverName: 'Alex Manager',
              reason: 'PRICE-SPECIAL',
              note: 'Approved live',
            })
          }
        >
          Approve
        </button>
      </div>
    ) : null,
}));
vi.mock('../lib/use-menu-availability', () => ({
  useMenuAvailability: () => ({ availabilityMap: {}, availabilityStale: false, refetchAll: vi.fn() }),
}));

const mockPosStoreState: Record<string, unknown> = {
  quickFilter: 'all',
  setQuickFilter: vi.fn(),
  setSelectedItem: vi.fn(),
  addToOrder: vi.fn(),
  loading: false,
  error: null,
  isMenuInteractionDisabled: () => false,
  isInitializing: false,
  orderId: 'MH-POS-26-00099',
  selectedOrderType: 'Dine In',
  selectedTable: 'T-101',
  selectedCustomer: { id: 'Walk-in Guest', name: 'Walk-in Guest' },
  noOfPax: 2,
  currency: 'UGX',
  loadTableOrder: loadTableOrderMock,
  posProfile: {
    name: 'Maximus Main Till',
    company: 'Maximus Hospitality Ltd',
    branch: 'Maximus Main Branch',
    currency: 'UGX',
  },
  menuItems: [
    {
      id: 'GRILL-PLATTER',
      item: 'GRILL-PLATTER',
      name: 'Tilapia & Plantain Platter',
      item_name: 'Tilapia & Plantain Platter',
      price: 10000,
      custom_is_open_priced: 0,
    },
    {
      id: 'OPEN-MARKET-FISH',
      item: 'OPEN-MARKET-FISH',
      name: 'Market Catch Open Item',
      item_name: 'Market Catch Open Item',
      price: 0,
      custom_is_open_priced: 1,
    },
  ],
};

vi.mock('../store/pos-store', () => ({
  usePOSStore: () => mockPosStoreState,
}));

vi.mock('../store/root-store', () => ({
  useRootStore: (selector?: (s: { user: { name: string; roles: string[] } }) => unknown) => {
    const state = { user: { name: 'cashier@example.com', roles: ['URY Cashier'] } };
    return typeof selector === 'function' ? selector(state) : state;
  },
}));

describe('R2 Blocker 4: Live POS Register Open Item Dialog Wiring & 4-Locale i18n', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window.navigator, 'onLine', { value: true, configurable: true });
    callGetMock.mockResolvedValue({ message: [] });
  });

  afterEach(() => {
    cleanup();
  });

  it('mounts an accessible Open Item button on POS Register that opens PriceOverrideDialog in mode="open_item" and submits to add_open_item', async () => {
    callPostMock.mockResolvedValueOnce({
      message: { invoice: 'MH-POS-26-00099', item_code: 'OPEN-MARKET-FISH', rate: 45000, qty: 1 },
    });

    const { default: POS } = await import('../pages/POS');
    render(<POS />);

    const openItemBtn = screen.getByTestId('register-open-item-btn');
    expect(openItemBtn).toBeInTheDocument();
    expect(openItemBtn.className).toContain('min-h-11');

    fireEvent.click(openItemBtn);

    const refInput = await screen.findByLabelText(/dish name|reference/i);
    fireEvent.change(refInput, { target: { value: 'Grilled Red Snapper' } });
    fireEvent.change(screen.getByLabelText(/open item price|price/i), { target: { value: '45000' } });
    fireEvent.click(screen.getByRole('button', { name: /add open item/i }));

    await waitFor(() => {
      expect(callPostMock).toHaveBeenCalledWith(
        'ury.ury_pos.pricing_actions.add_open_item',
        expect.objectContaining({
          invoice: 'MH-POS-26-00099',
          item_code: 'OPEN-MARKET-FISH',
          rate: 45000,
          reference: 'Grilled Red Snapper',
        }),
      );
    });
  });

  it('ships white-label 4-locale i18n keys for Open Item picker with zero vendor leaks', () => {
    const forbidden = /\b(ERPNext|Frappe|URY|Kamra|HUF|HeyKoala|Tridz|BuildSuite|Simphony)\b/i;
    for (const [lang, bundle] of Object.entries({ en, fr, es, ar })) {
      const section = (bundle as Record<string, Record<string, string>>).privileged_actions || {};
      expect(section.select_open_item, `${lang}.privileged_actions.select_open_item`).toBeTruthy();
      expect(section.select_open_item).not.toMatch(forbidden);
      expect(section.no_open_items_configured, `${lang}.privileged_actions.no_open_items_configured`).toBeTruthy();
      expect(section.no_open_items_configured).not.toMatch(forbidden);
    }
  });
});
