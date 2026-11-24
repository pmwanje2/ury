import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { call } from '@ury/core';
import { initI18n } from '../i18n';
import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';
import PaymentDialog from './PaymentDialog';
import { submitSettlementOfflineAware } from '../offline/cashier';
import { OfflineOutbox } from '../offline/outbox';

const rootInit = useRootStore.getState();
const posInit = usePOSStore.getState();

beforeEach(async () => {
  await initI18n('en');
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  useRootStore.setState(rootInit, true);
  usePOSStore.setState(posInit, true);
});

function renderPayment(opts?: {
  currency?: string;
  grandTotal?: number;
  paymentModes?: string[];
  paymentModeMeta?: Record<string, { type: string; overtender_rule: string; halo_max_amount?: number }>;
}) {
  useRootStore.setState({ user: { name: 'cashier@example.com', full_name: 'Grace Cashier', roles: ['URY Cashier'] } });
  usePOSStore.setState({
    currency: opts?.currency ?? 'UGX',
    paymentModes: opts?.paymentModes ?? ['Cash', 'Mobile Money'],
    fetchPaymentModes: async () => {},
    posProfile: {
      name: 'Main Till',
      company: 'Hotel Co',
      currency: opts?.currency ?? 'UGX',
      customer: 'Walk-in',
      allow_partial_payment: 0,
      enable_discount: 0,
      order_type_customer_map: {},
      default_customer: null,
      payment_mode_meta: opts?.paymentModeMeta ?? {
        Cash: { type: 'Cash', overtender_rule: 'Refuse', halo_max_amount: 500000 },
        'Mobile Money': { type: 'Bank', overtender_rule: 'Refuse', halo_max_amount: 2000000 },
      },
    } as unknown as ReturnType<typeof usePOSStore.getState>['posProfile'],
  });

  render(
    <PaymentDialog
      customer="Walk-in"
      grandTotal={opts?.grandTotal ?? 50000}
      roundedTotal={opts?.grandTotal ?? 50000}
      order={{ invoice_printed: 1 }}
      invoice="POS-INV-HALO-UI-1"
      posProfile="Main Till"
      table={null}
      cashier="cashier@example.com"
      owner="cashier@example.com"
      onClose={vi.fn()}
      fetchOrders={async () => {}}
      clearSelectedOrder={vi.fn()}
    />,
  );
}

describe('PaymentDialog High Amount Lockout (HALO) per tender', () => {
  it('catches mistyped 5,000,000 for 50,000 on Cash, opens ManagerApproval, and sends halo_approval_token', async () => {
    vi.spyOn(call, 'get').mockImplementation((method: string) => {
      if (method === 'ury.ury_pos.override.list_approvers') {
        return Promise.resolve({ message: [{ user: 'manager@example.com', full_name: 'Alex Manager' }] });
      }
      if (method === 'ury.ury_pos.override.privilege_policy') {
        return Promise.resolve({ message: { may_perform: false, requires_reason: false } });
      }
      return Promise.resolve({ message: {} });
    });
    const postSpy = vi.spyOn(call, 'post').mockImplementation((method: string) => {
      if (method === 'ury.ury_pos.override.authorise') {
        return Promise.resolve({
          message: { token: 'halo-pin-tok-1', approver: 'manager@example.com', approver_name: 'Alex Manager', expires_in: 120 },
        });
      }
      return Promise.resolve({ message: { status: 'Submitted' } });
    });

    renderPayment({ grandTotal: 50000 });
    const [cashInput] = screen.getAllByRole('spinbutton');
    fireEvent.change(cashInput, { target: { value: '5000000' } });

    // Immediate HALO warning shown before Pay is clicked
    expect(screen.getByText(/High Amount Lockout/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^Pay /i }));
    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();

    for (const digit of ['2', '4', '6', '8']) {
      fireEvent.click(await screen.findByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury.doctype.ury_order.ury_order.make_invoice',
        expect.objectContaining({
          invoice: 'POS-INV-HALO-UI-1',
          halo_approval_token: 'halo-pin-tok-1',
          payments: [{ mode_of_payment: 'Cash', amount: 5000000 }],
        }),
      ),
    );
    expect(useRootStore.getState().user?.name).toBe('cashier@example.com');
  });

  it('enforces HALO on non-cash tenders (Mobile Money) and allows amounts at or below HALO without PIN', async () => {
    const postSpy = vi.spyOn(call, 'post').mockResolvedValue({ message: { status: 'Submitted' } });
    vi.spyOn(call, 'get').mockResolvedValue({ message: [{ user: 'manager@example.com', full_name: 'Alex Manager' }] });

    renderPayment({ grandTotal: 2500000 });
    const [, momoInput] = screen.getAllByRole('spinbutton');
    fireEvent.change(momoInput, { target: { value: '2500000' } });

    expect(screen.getByText(/High Amount Lockout/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /^Pay /i }));
    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();
    expect(postSpy).not.toHaveBeenCalledWith('ury.ury.doctype.ury_order.ury_order.make_invoice', expect.anything());

    cleanup();
    renderPayment({ grandTotal: 500000 });
    const [cashAtLimit] = screen.getAllByRole('spinbutton');
    fireEvent.change(cashAtLimit, { target: { value: '500000' } });
    expect(screen.queryByText(/High Amount Lockout/i)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /^Pay /i }));
    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury.doctype.ury_order.ury_order.make_invoice',
        expect.objectContaining({ payments: [{ mode_of_payment: 'Cash', amount: 500000 }] }),
      ),
    );
  });

  it('never captures an over-HALO tender offline silently', async () => {
    const gatewayErr = { httpStatus: 503, message: 'Service Unavailable' };
    const onlineSettle = vi.fn().mockRejectedValue(gatewayErr);
    vi.spyOn(OfflineOutbox.prototype, 'list').mockResolvedValue([]);
    const queueSpy = vi
      .spyOn(OfflineOutbox.prototype, 'queueGatewaySettlement')
      .mockResolvedValue({ client_order_id: 'fake' } as never);
    await expect(
      submitSettlementOfflineAware({
        profile: {
          name: 'Main Till',
          ury_offline_capture: 1,
          role_allowed_for_billing: [{ role: 'URY Cashier' }],
          payment_mode_meta: { Cash: { type: 'Cash', overtender_rule: 'Refuse', halo_max_amount: 500000 } },
        } as never,
        user: { name: 'cashier@example.com', roles: ['URY Cashier'] },
        invoice: 'POS-INV-OFF-1',
        amount: 5000000,
        due: 50000,
        modeOfPayment: 'Cash',
        onlineSettle,
      }),
    ).rejects.toEqual(gatewayErr);
    expect(queueSpy).not.toHaveBeenCalled();
  });
});
