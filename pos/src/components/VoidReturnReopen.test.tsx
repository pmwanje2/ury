import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { act } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { showToast } from '@ury/ui';
import { initI18n } from '../i18n';
import SettledBills from '../pages/SettledBills';
import VoidItemDialog from './VoidItemDialog';
import ReturnCheckDialog from './ReturnCheckDialog';
import ReopenAdjustCheckDialog from './ReopenAdjustCheckDialog';
import { formatMoneyAmount } from '../lib/void-return-api';

const __dir = dirname(fileURLToPath(import.meta.url));
const LOCALE_DIR = join(__dir, '..', 'i18n', 'locales');

beforeEach(async () => {
  document.cookie = 'user_id=cashier%40example.com; Path=/';
  await initI18n('en');
  vi.spyOn(showToast, 'error').mockImplementation(() => '' as never);
  vi.spyOn(showToast, 'success').mockImplementation(() => '' as never);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('VoidItemDialog (sent item void & waste)', () => {
  it('loads POS reasons, marks waste write-off, opens ManagerApproval on POS-AUTH-REQUIRED, and retries with token', async () => {
    vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
      if (method === 'ury.ury_pos.override.list_reasons') {
        return {
          message: [
            { code: 'VOID-GUEST', description: 'Guest changed mind', requires_note: 0, applies_to: ['void_sent_item', 'reduce_sent_qty'] },
            { code: 'WASTE-SPOILED', description: 'Spoiled or dropped item (waste)', requires_note: 0, applies_to: ['void_sent_item', 'reduce_sent_qty'] },
            { code: 'OTHER', description: 'Other', requires_note: 1, applies_to: [] },
          ],
        };
      }
      if (method === 'ury.ury_pos.override.list_approvers') {
        return { message: [{ user: 'mgr@example.com', full_name: 'Alice Manager' }] };
      }
      if (method === 'ury.ury_pos.override.privilege_policy') {
        return { message: { privilege: 'reduce_sent_qty', label: 'Reduce Sent Qty', may_perform: false, requires_reason: false } };
      }
      return { message: [] };
    });

    const postSpy = vi.spyOn(call, 'post').mockImplementation(async (method: string, payload?: unknown) => {
      const body = (payload || {}) as Record<string, unknown>;
      if (method === 'ury.ury.api.ury_pos_invoice_qty_reduction.reduce_order_item_qty') {
        if (!body.approval_token) {
          throw new Error('A manager must approve this action. [POS-AUTH-REQUIRED]');
        }
        return {
          message: {
            invoice: 'POS-INV-10',
            item_row_name: 'ROW-1',
            item_code: 'STEAK',
            previous_qty: 2,
            new_qty: 1,
            delta: -1,
            cancel_kot_names: ['CNCL-KOT-1'],
            is_waste: true,
            waste_stock_entry: 'STE-WASTE-1',
            approver: 'mgr@example.com',
          },
        };
      }
      if (method === 'ury.ury_pos.override.authorise') {
        return { message: { token: 'tok-waste-1', approver: 'mgr@example.com', approver_name: 'Alice Manager', expires_in: 120 } };
      }
      return { message: null };
    });

    const onCompleted = vi.fn();
    render(
      <VoidItemDialog
        open
        posProfile="Till A"
        invoiceId="POS-INV-10"
        currency="UGX"
        item={{ name: 'ROW-1', item_code: 'STEAK', item_name: 'Steak', qty: 2, rate: 35000 }}
        onCompleted={onCompleted}
        onClose={vi.fn()}
      />,
    );

    const select = await screen.findByLabelText(/reason/i);
    fireEvent.change(select, { target: { value: 'WASTE-SPOILED' } });

    // Waste write-off checkbox auto-activates for WASTE-* reason
    const wasteCheckbox = screen.getByLabelText(/waste/i) as HTMLInputElement;
    expect(wasteCheckbox.checked).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: /confirm void/i }));

    // ManagerApproval opens with PIN pad
    await screen.findByText(/Alice Manager/);
    for (const digit of ['7', '4', '1', '2']) {
      fireEvent.click(screen.getByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /approve/i }));

    await waitFor(() => expect(onCompleted).toHaveBeenCalledTimes(1));
    const retryCall = postSpy.mock.calls.filter((c) => c[0] === 'ury.ury.api.ury_pos_invoice_qty_reduction.reduce_order_item_qty').at(-1);
    expect((retryCall?.[1] as Record<string, unknown>)?.approval_token).toBe('tok-waste-1');
    expect((retryCall?.[1] as Record<string, unknown>)?.is_waste).toBe(1);
    expect((retryCall?.[1] as Record<string, unknown>)?.reason).toBe('WASTE-SPOILED');
  });
});

describe('ReturnCheckDialog (whole/partial return, UGX rounding, room charge, EFRIS, PIN)', () => {
  it('previews proportional UGX refund, enforces room charge folio lock, and completes with ManagerApproval', async () => {
    vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
      if (method === 'ury.ury_pos.void_return_reopen.get_returnable_check') {
        return {
          message: {
            invoice: 'POS-INV-55',
            pos_profile: 'Till A',
            branch: 'Branch A',
            customer: 'Walk-in',
            currency: 'UGX',
            precision: 0,
            grand_total: 25000,
            discount_amount: 5000,
            total_taxes_and_charges: 0,
            net_paid_amount: 25000,
            already_refunded_amount: 0,
            remaining_refundable_amount: 25000,
            original_payments: [{ mode_of_payment: 'Room Charge', amount: 25000 }],
            available_modes: ['Cash', 'Card', 'Room Charge'],
            is_room_charge: true,
            room_number: '304',
            is_fiscalised: true,
            items: [
              { item_row_name: 'ROW-1', item_code: 'BURGER', item_name: 'Burger', sold_qty: 3, returned_qty: 0, remaining_qty: 3, rate: 10000, amount: 30000 },
            ],
            reasons: [
              { code: 'RET-QUALITY', description: 'Item quality complaint', requires_note: 0, applies_to: ['return'] },
            ],
          },
        };
      }
      if (method === 'ury.ury_pos.void_return_reopen.preview_pos_return') {
        return {
          message: {
            original_invoice: 'POS-INV-55',
            currency: 'UGX',
            precision: 0,
            items: [{ item_row_name: 'ROW-1', item_code: 'BURGER', item_name: 'Burger', qty: 1, rate: 10000, gross: 10000 }],
            subtotal: 10000,
            discount_amount: 1667,
            tax_amount: 0,
            refund_total: 8333,
            max_cash_refundable: 8333,
            unpaid_credit_reduction: 0,
            default_refund_payments: [{ mode_of_payment: 'Room Charge', amount: 8333 }],
            refund_payments: [{ mode_of_payment: 'Room Charge', amount: 8333 }],
            is_room_charge: true,
            is_fiscalised: true,
          },
        };
      }
      if (method === 'ury.ury_pos.override.list_approvers') {
        return { message: [{ user: 'mgr2@example.com', full_name: 'Bob Manager' }] };
      }
      if (method === 'ury.ury_pos.override.privilege_policy') {
        return { message: { privilege: 'return', label: 'Return / Refund', may_perform: false, requires_reason: false } };
      }
      return { message: [] };
    });

    const postSpy = vi.spyOn(call, 'post').mockImplementation(async (method: string, payload?: unknown) => {
      const body = (payload || {}) as Record<string, unknown>;
      if (method === 'ury.ury_pos.void_return_reopen.create_pos_return') {
        if (!body.approval_token) {
          throw new Error('A manager must approve this action. [POS-AUTH-REQUIRED]');
        }
        return {
          message: {
            return_invoice: 'POS-RET-55',
            original_invoice: 'POS-INV-55',
            currency: 'UGX',
            refund_total: 8333,
            refund_payments: [{ mode_of_payment: 'Room Charge', amount: 8333 }],
            approver: 'mgr2@example.com',
            reason: 'RET-QUALITY',
            is_fiscalised: true,
            efris_creditnote_reasoncode: '101:Return of products due to expiry or damage, etc.',
          },
        };
      }
      if (method === 'ury.ury_pos.override.authorise') {
        return { message: { token: 'tok-ret-55', approver: 'mgr2@example.com', approver_name: 'Bob Manager', expires_in: 120 } };
      }
      return { message: null };
    });

    const onCompleted = vi.fn();
    render(
      <ReturnCheckDialog
        open
        invoiceId="POS-INV-55"
        onCompleted={onCompleted}
        onClose={vi.fn()}
      />,
    );

    // Loads room charge & fiscalised notices and Burger line
    await screen.findByText(/Burger/);
    expect(screen.getByText(/304/)).toBeTruthy();
    expect(screen.getByText(/credit note/i)).toBeTruthy();

    // Select 1 Burger to return
    fireEvent.click(screen.getByRole('button', { name: /increase return quantity for burger/i }));
    expect((await screen.findAllByText(/8,333|8333/)).length).toBeGreaterThanOrEqual(1);

    // Pick reason and submit
    fireEvent.change(screen.getByLabelText(/return reason/i), { target: { value: 'RET-QUALITY' } });
    fireEvent.click(screen.getByRole('button', { name: /process return/i }));

    // ManagerApproval opens
    await screen.findByText(/Bob Manager/);
    for (const digit of ['1', '2', '3', '4']) {
      fireEvent.click(screen.getByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /approve/i }));

    await waitFor(() => expect(onCompleted).toHaveBeenCalledTimes(1));
    const retCall = postSpy.mock.calls.filter((c) => c[0] === 'ury.ury_pos.void_return_reopen.create_pos_return').at(-1);
    expect((retCall?.[1] as Record<string, unknown>)?.approval_token).toBe('tok-ret-55');
  });
});

describe('ReopenAdjustCheckDialog & SettledBills integration', () => {
  it('requires explicit credit-and-rebill confirmation on fiscalised check before adjusting tender', async () => {
    vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
      if (method === 'ury.ury_pos.override.list_reasons') {
        return {
          message: [
            { code: 'REOPEN-TENDER', description: 'Wrong payment method', requires_note: 0, applies_to: ['adjust_closed_check', 'reopen_check'] },
          ],
        };
      }
      return { message: [] };
    });

    const postSpy = vi.spyOn(call, 'post').mockResolvedValue({
      message: {
        action: 'adjust_closed_check',
        original_invoice: 'POS-INV-99',
        reversal_return: 'POS-REV-99',
        replacement_invoice: 'POS-REP-99',
        approver: 'mgr@example.com',
        credit_and_rebill: true,
      },
    });

    const onCompleted = vi.fn();
    render(
      <ReopenAdjustCheckDialog
        open
        bill={{
          name: 'POS-INV-99',
          pos_profile: 'Till A',
          currency: 'UGX',
          grand_total: 50000,
          rounded_total: 50000,
          efris_irn: 'FDN-99',
          items: [{ item_name: 'Steak', qty: 1, amount: 50000 }],
          payments: [{ mode_of_payment: 'Cash', amount: 50000 }],
        }}
        availableModes={['Cash', 'Card']}
        onCompleted={onCompleted}
        onClose={vi.fn()}
      />,
    );

    const reasonSel = await screen.findByLabelText(/reason/i);
    fireEvent.change(reasonSel, { target: { value: 'REOPEN-TENDER' } });

    const submitBtn = screen.getByRole('button', { name: /apply adjustment/i }) as HTMLButtonElement;
    expect(submitBtn.disabled).toBe(true);

    // Tick explicit credit-and-rebill confirmation
    fireEvent.click(screen.getByLabelText(/credit note and rebill/i));
    expect(submitBtn.disabled).toBe(false);

    fireEvent.click(submitBtn);
    await waitFor(() => expect(onCompleted).toHaveBeenCalledTimes(1));
    expect(postSpy).toHaveBeenCalledWith(
      'ury.ury_pos.void_return_reopen.adjust_closed_check',
      expect.objectContaining({ invoice_id: 'POS-INV-99', credit_and_rebill: 1, reason: 'REOPEN-TENDER' }),
    );
  });

  it('exposes Return / Refund and Reopen / Adjust on SettledBills for non-consolidated closed checks', async () => {
    vi.spyOn(call, 'get').mockResolvedValue({
      message: [
        {
          name: 'INV-OPEN-DAY',
          status: 'Paid',
          posting_date: '2026-11-24',
          posting_time: '10:00:00',
          currency: 'UGX',
          pos_profile: 'Till A',
          restaurant_table: 'T1',
          consolidated_invoice: null,
          is_return: 0,
          grand_total: 30000,
          rounded_total: 30000,
          outstanding_amount: 0,
          total_taxes_and_charges: 0,
          items: [{ name: 'ROW-1', item_code: 'BURGER', item_name: 'Burger', qty: 3, rate: 10000, amount: 30000 }],
          taxes: [],
          payments: [{ mode_of_payment: 'Cash', amount: 30000 }],
        },
      ],
    });

    render(<MemoryRouter><SettledBills /></MemoryRouter>);
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    fireEvent.click(await screen.findByRole('button', { name: /INV-OPEN-DAY/ }));

    expect(screen.getByRole('button', { name: /return \/ refund/i })).toBeTruthy();
    expect(screen.getByRole('button', { name: /reopen \/ adjust/i })).toBeTruthy();
  });

  it('ships complete en/fr/es/ar void_return translations with zero vendor names (URY/HUF/Kamra)', () => {
    const enJson = JSON.parse(readFileSync(join(LOCALE_DIR, 'en.json'), 'utf-8'));
    expect(enJson.void_return).toBeDefined();
    const enKeys = Object.keys(enJson.void_return).sort();
    expect(enKeys.length).toBeGreaterThanOrEqual(20);

    for (const lang of ['en', 'fr', 'es', 'ar']) {
      const json = JSON.parse(readFileSync(join(LOCALE_DIR, `${lang}.json`), 'utf-8'));
      expect(json.void_return, `${lang}.json missing void_return`).toBeDefined();
      expect(Object.keys(json.void_return).sort()).toEqual(enKeys);
      const serialized = JSON.stringify(json.void_return);
      expect(serialized).not.toMatch(/\b(URY|HUF|Kamra|Tridz)\b/i);
    }
  });

  it('formats 0dp (UGX), 2dp (USD), and 3dp (KWD) currencies accurately', () => {
    expect(formatMoneyAmount(8333, 'UGX')).toMatch(/UGX\s+8,333$/);
    expect(formatMoneyAmount(12.5, 'USD')).toMatch(/USD\s+12\.50$/);
    expect(formatMoneyAmount(3.25, 'KWD')).toMatch(/KWD\s+3\.250$/);
  });

  it('reloads POS reasons when switching from Adjust to Reopen tab and hydrates bill details when payments are missing', async () => {
    const requestedPrivs: string[] = [];
    const getSpy = vi.spyOn(call, 'get').mockImplementation(async (method: string, params?: unknown) => {
      const q = (params || {}) as Record<string, unknown>;
      if (method === 'ury.ury_pos.override.list_reasons') {
        requestedPrivs.push(String(q.privilege || ''));
        return {
          message: [
            { code: 'REOPEN-SPLIT', description: 'Split after close', requires_note: 0, applies_to: ['reopen_check'] },
          ],
        };
      }
      if (method === 'ury.ury_pos.void_return_reopen.get_returnable_check') {
        return {
          message: {
            invoice: 'POS-INV-HYDRATE',
            pos_profile: 'Till A',
            branch: 'Branch A',
            customer: 'Walk-in',
            currency: 'KWD',
            precision: 3,
            grand_total: 12.5,
            discount_amount: 0,
            total_taxes_and_charges: 0,
            net_paid_amount: 12.5,
            already_refunded_amount: 0,
            remaining_refundable_amount: 12.5,
            original_payments: [{ mode_of_payment: 'Card', amount: 12.5 }],
            available_modes: ['Cash', 'Card'],
            is_room_charge: false,
            is_fiscalised: true,
            items: [{ item_row_name: 'R-1', item_code: 'STEAK', item_name: 'Steak', sold_qty: 1, returned_qty: 0, remaining_qty: 1, rate: 12.5, amount: 12.5 }],
            reasons: [],
          },
        };
      }
      return { message: [] };
    });

    render(
      <ReopenAdjustCheckDialog
        open
        bill={{
          name: 'POS-INV-HYDRATE',
          pos_profile: 'Till A',
          currency: 'KWD',
          grand_total: 12.5,
          rounded_total: 12.5,
          items: [{ item_name: 'Steak', qty: 1, amount: 12.5 }],
          payments: [],
        }}
        onCompleted={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    // Hydrates fiscalised notice & original payments from get_returnable_check when bill.payments is empty
    await screen.findByLabelText(/credit note and rebill/i);
    expect(getSpy).toHaveBeenCalledWith(
      'ury.ury_pos.void_return_reopen.get_returnable_check',
      { invoice_id: 'POS-INV-HYDRATE' },
    );

    // Switching to Reopen tab fetches reopen_check reasons
    fireEvent.click(screen.getByRole('tab', { name: /reopen check/i }));
    await waitFor(() => expect(requestedPrivs).toContain('reopen_check'));
  });

  it('uses localized aria-labels on ReturnCheckDialog quantity buttons in French', async () => {
    await initI18n('fr');
    vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
      if (method === 'ury.ury_pos.void_return_reopen.get_returnable_check') {
        return {
          message: {
            invoice: 'POS-INV-FR',
            pos_profile: 'Till A',
            branch: 'Branch A',
            customer: 'Walk-in',
            currency: 'EUR',
            precision: 2,
            grand_total: 20,
            discount_amount: 0,
            total_taxes_and_charges: 0,
            net_paid_amount: 20,
            already_refunded_amount: 0,
            remaining_refundable_amount: 20,
            original_payments: [{ mode_of_payment: 'Cash', amount: 20 }],
            available_modes: ['Cash'],
            is_room_charge: false,
            is_fiscalised: false,
            items: [{ item_row_name: 'R-FR', item_code: 'FISH', item_name: 'Poisson', sold_qty: 2, returned_qty: 0, remaining_qty: 2, rate: 10, amount: 20 }],
            reasons: [{ code: 'RET-QUALITY', description: 'Qualité', requires_note: 0, applies_to: ['return'] }],
          },
        };
      }
      return { message: [] };
    });

    render(<ReturnCheckDialog open invoiceId="POS-INV-FR" onCompleted={vi.fn()} onClose={vi.fn()} />);
    await screen.findByText(/Poisson/);
    expect(screen.queryByRole('button', { name: /Increase return quantity for Poisson/i })).toBeNull();
    expect(screen.getAllByRole('button', { name: /Poisson/i }).length).toBeGreaterThanOrEqual(2);
  });
});
