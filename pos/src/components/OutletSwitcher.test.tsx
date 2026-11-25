import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import OutletSwitcher, { OutletPickerDialog } from './OutletSwitcher';
import type { AvailableOutlet } from '../lib/outlet-api';

const switchOutletMock = vi.fn();
const mockStoreState = vi.hoisted(() => ({
  posProfile: {
    name: 'Main Grill POS',
    branch: 'Main Grill',
    company: 'Maximus Hospitality',
    currency: 'UGX',
  } as Record<string, unknown> | null,
  availableOutlets: [] as AvailableOutlet[],
  requiresOutletSelection: false,
  activeOrders: [] as Array<{ uniqueId: string; name: string; quantity: number }>,
  switchOutlet: (...args: unknown[]) => switchOutletMock(...args),
}));

vi.mock('../store/pos-store', () => ({
  usePOSStore: (selector?: (s: typeof mockStoreState) => unknown) =>
    typeof selector === 'function' ? selector(mockStoreState) : mockStoreState,
}));

vi.mock('../i18n', () => ({
  t: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}:${JSON.stringify(params)}` : key,
}));

const VENDOR_RE = /\b(?:URY|Frappe|ERPNext|Kamra|HUF|Tridz|HeyKoala|BuildSuite)\b/i;

const TWO_OUTLETS: AvailableOutlet[] = [
  {
    pos_profile: 'Main Grill POS',
    name: 'Main Grill POS',
    branch: 'Main Grill',
    company: 'Maximus Hospitality',
    restaurant: 'Main Grill',
    currency: 'UGX',
    has_open_shift: true,
    open_entry: 'POS-OPE-001',
    is_active: true,
  },
  {
    pos_profile: 'Pool Bar POS',
    name: 'Pool Bar POS',
    branch: 'Pool Bar',
    company: 'Maximus Hospitality',
    restaurant: 'Pool Bar',
    currency: 'USD',
    has_open_shift: false,
    open_entry: null,
    is_active: false,
  },
];

describe('OutletSwitcher & OutletPickerDialog (G43)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    switchOutletMock.mockResolvedValue(undefined);
    mockStoreState.posProfile = {
      name: 'Main Grill POS',
      branch: 'Main Grill',
      company: 'Maximus Hospitality',
      currency: 'UGX',
    };
    mockStoreState.availableOutlets = [...TWO_OUTLETS];
    mockStoreState.requiresOutletSelection = false;
    mockStoreState.activeOrders = [];
  });

  it('hides completely when user has 0 or 1 outlet', () => {
    mockStoreState.availableOutlets = [TWO_OUTLETS[0]];
    const { container } = render(<OutletSwitcher />);
    expect(container.firstChild).toBeNull();
  });

  it('renders header pill with active outlet and currency, and switches outlet on empty cart', async () => {
    const user = userEvent.setup();
    render(<OutletSwitcher />);

    const trigger = screen.getByRole('button', { name: /Main Grill POS/i });
    expect(trigger).toBeInTheDocument();
    expect(trigger.textContent).toContain('UGX');
    expect(document.body.textContent || '').not.toMatch(VENDOR_RE);

    await user.click(trigger);

    expect(screen.getByText('Pool Bar POS')).toBeInTheDocument();
    expect(screen.getByText('USD')).toBeInTheDocument();
    expect(screen.getByText('outlets.open_shift_badge')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Pool Bar POS/i }));
    await waitFor(() => expect(switchOutletMock).toHaveBeenCalledWith('Pool Bar POS'));
  });

  it('confirms discarding unsent cart items before switching outlets', async () => {
    const user = userEvent.setup();
    mockStoreState.activeOrders = [{ uniqueId: 'line-1', name: 'Grilled Tilapia', quantity: 2 }];

    render(<OutletSwitcher />);
    await user.click(screen.getByRole('button', { name: /Main Grill POS/i }));
    await user.click(screen.getByRole('button', { name: /Pool Bar POS/i }));

    // Confirmation prompt appears, switchOutlet NOT called yet
    expect(screen.getByText(/outlets\.discard_cart_title/i)).toBeInTheDocument();
    expect(switchOutletMock).not.toHaveBeenCalled();

    // Cancel keeps current outlet
    await user.click(screen.getByRole('button', { name: 'common.cancel' }));
    expect(switchOutletMock).not.toHaveBeenCalled();

    // Confirming executes switch
    await user.click(screen.getByRole('button', { name: /Pool Bar POS/i }));
    await user.click(screen.getByRole('button', { name: 'outlets.discard_and_switch' }));
    await waitFor(() => expect(switchOutletMock).toHaveBeenCalledWith('Pool Bar POS'));
  });

  it('prompts mandatory OutletPickerDialog when requiresOutletSelection is true', async () => {
    const user = userEvent.setup();
    mockStoreState.posProfile = null;
    mockStoreState.requiresOutletSelection = true;

    render(<OutletPickerDialog open={true} mandatory={true} onOpenChange={vi.fn()} />);

    expect(screen.getByText('outlets.select_outlet_title')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Pool Bar POS/i }));
    await waitFor(() => expect(switchOutletMock).toHaveBeenCalledWith('Pool Bar POS'));
  });
});
