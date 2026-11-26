import { describe, expect, it, vi, beforeEach } from 'vitest';

vi.mock('../src/offline/cashier', () => ({
  assertOfflineActionAllowed: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('@ury/core', async () => {
  const actual = await vi.importActual<typeof import('@ury/core')>('@ury/core');
  return {
    ...actual,
    call: {
      get: vi.fn().mockResolvedValue({ message: { html: '<div class="sr">ok</div>' } }),
      post: vi.fn().mockResolvedValue({ message: {} }),
    },
    db: {
      getDoc: vi.fn(),
      getDocList: vi.fn(),
    },
    printWithQz: vi.fn(),
    ownedSessionKey: vi.fn(),
  };
});

import { call, printWithQz } from '@ury/core';
import {
  MERGED_POS_INVOICE_PRINT_FORMAT,
  UNSAFE_POS_INVOICE_PRINT_FORMATS,
  resolvePrintFormat,
} from '../src/lib/invoice-api';
import { printBillCopy, printOrder } from '../src/lib/print';
import type { PosProfileCombined } from '../src/lib/pos-profile-api';

describe('resolvePrintFormat safety & default guest-check fallback', () => {
  it('defaults to Merged POS Invoice Format when defaultFormat is empty or null', () => {
    expect(resolvePrintFormat({ custom_merged_pos_invoice: null }, null)).toBe(MERGED_POS_INVOICE_PRINT_FORMAT);
    expect(resolvePrintFormat({ custom_merged_pos_invoice: null }, '')).toBe(MERGED_POS_INVOICE_PRINT_FORMAT);
    expect(resolvePrintFormat({ custom_merged_pos_invoice: null }, '   ')).toBe(MERGED_POS_INVOICE_PRINT_FORMAT);
  });

  it('never returns any of the 8 unsafe ERPNext / EFRIS standard POS Invoice formats', () => {
    const allUnsafe = [
      'POS Invoice Classic',
      'POS Invoice Bordered',
      'POS Invoice Modern',
      'POS Invoice Modern with Images',
      'POS Invoice',
      'Return POS Invoice',
      'EFRIS POS Receipt 80mm',
      'POS EFRIS Invoice',
    ];
    for (const unsafe of allUnsafe) {
      expect(UNSAFE_POS_INVOICE_PRINT_FORMATS.has(unsafe), `Missing ${unsafe} from UNSAFE_POS_INVOICE_PRINT_FORMATS`).toBe(true);
      expect(resolvePrintFormat({ custom_merged_pos_invoice: null }, unsafe)).toBe(
        MERGED_POS_INVOICE_PRINT_FORMAT
      );
    }
  });

  it('preserves an explicitly configured safe custom print format on unmerged bills', () => {
    expect(resolvePrintFormat({ custom_merged_pos_invoice: null }, 'Restaurant Receipt 80mm')).toBe(
      'Restaurant Receipt 80mm'
    );
  });

  it('always uses Merged POS Invoice Format for merged bills', () => {
    expect(resolvePrintFormat({ custom_merged_pos_invoice: 'POS-INV-0002' }, 'Restaurant Receipt 80mm')).toBe(
      MERGED_POS_INVOICE_PRINT_FORMAT
    );
  });
});

describe('printOrder and printBillCopy letterhead forwarding', () => {
  const openSpy = vi.fn();
  const navigate = vi.fn();
  const snapshot = { html: '<p>COPY #2 / REPRINT</p>', invoice_revision: 'rev2', print_url: '/authorized-receipt?trigger_print=1' };

  beforeEach(() => {
    vi.clearAllMocks();
    openSpy.mockReturnValue({ closed: true, close: vi.fn(), location: { replace: navigate } });
    vi.spyOn(window, 'confirm').mockReset().mockReturnValue(true);
    vi.mocked(printWithQz).mockReset();
    vi.stubGlobal('open', openSpy);
  });

  function makeProfile(overrides: Partial<PosProfileCombined> = {}): PosProfileCombined {
    return {
      name: 'Main POS',
      owner: 'Administrator',
      creation: '',
      modified: '',
      modified_by: '',
      docstatus: 0,
      idx: 0,
      company: 'Grand Terrace Ltd',
      customer: 'Walk-in Guest',
      country: 'Uganda',
      disabled: 0,
      warehouse: 'Main',
      campaign: null,
      company_address: null,
      restaurant: 'Grand Terrace',
      branch: 'Main Dining',
      currency: 'UGX',
      role_allowed_for_billing: [],
      waiter: 'waiter@test',
      cashier: 'cashier@test',
      print_format: null,
      qz_print: 0,
      qz_host: null,
      printer: null,
      print_type: 'socket',
      tableAttention: 0,
      paid_limit: 0,
      disable_rounded_total: 0,
      enable_discount: 0,
      multiple_cashier: 0,
      ...overrides,
    };
  }

  it('prepares before dispatch and commits only a delivered receipt', async () => {
    const events: string[] = [];
    vi.mocked(call.post).mockImplementation(async (method: string) => {
      events.push(method.split('.').pop()!);
      return { message: { ...snapshot, delivery_id: 'receipt-1' } };
    });
    vi.mocked(printWithQz).mockImplementation(async () => { events.push('dispatch'); });
    const params = { orderId: 'INV-1', posProfile: makeProfile({ print_type: 'qz', printer: 'Front Desk' }) };
    await printOrder(params);
    expect(events).toEqual(['prepare_receipt_delivery', 'dispatch', 'complete_receipt_delivery']);
    expect(printWithQz).toHaveBeenCalledWith('localhost', snapshot.html, 'Front Desk');
    events.length = 0;
    vi.mocked(printWithQz).mockRejectedValueOnce(new Error('QZ_NOT_RUNNING'));
    vi.spyOn(window, 'confirm').mockReturnValueOnce(false);
    await expect(printOrder(params)).rejects.toThrow('QZ_NOT_RUNNING');
    expect(events).toEqual(['prepare_receipt_delivery']);
  });

  it('records fallback delivery only when the cashier confirms the browser receipt printed', async () => {
    vi.mocked(call.post).mockResolvedValue({ message: { ...snapshot, delivery_id: 'receipt-2' } });
    vi.mocked(printWithQz).mockRejectedValue(new Error('QZ_NOT_RUNNING'));
    const popup = { closed: true, opener: window };
    openSpy.mockReturnValue(popup);
    vi.spyOn(window, 'confirm').mockReturnValueOnce(true).mockReturnValueOnce(false);
    const params = { orderId: 'INV-2', posProfile: makeProfile({ print_type: 'qz' }) };
    await expect(printOrder(params)).rejects.toThrow();
    expect(call.post).not.toHaveBeenCalledWith(expect.stringContaining('complete_receipt_delivery'), expect.anything());
    vi.mocked(window.confirm).mockReturnValue(true);
    await expect(printOrder(params)).resolves.toBe('socket');
    expect(openSpy.mock.lastCall?.[0]).toBe(snapshot.print_url);
    expect(call.post).toHaveBeenCalledWith(expect.stringContaining('complete_receipt_delivery'), {
      invoice: 'INV-2', delivery_id: 'receipt-2',
    });
    vi.mocked(printWithQz).mockResolvedValue(undefined);
  });

  it.each([{ status: 'Failure', message: 'queue offline' }, {}, null])('does not finalize a rejected or missing network acknowledgement %j', async (message) => {
    vi.mocked(call.post).mockResolvedValue({ message });
    await expect(printOrder({ orderId: 'N', posProfile: makeProfile({ print_type: 'network' }) })).rejects.toThrow();
    expect(call.post).toHaveBeenCalledTimes(1);
  });

  it('cannot complete a browser-only receipt when the cashier cancels or the popup is blocked', async () => {
    vi.mocked(call.post).mockResolvedValue({ message: { ...snapshot, delivery_id: 'd' } });
    vi.mocked(window.confirm).mockReturnValue(false);
    const params = { orderId: 'B', posProfile: makeProfile() };
    await expect(printOrder(params)).rejects.toThrow();
    openSpy.mockReturnValue(null);
    HTMLDialogElement.prototype.showModal ??= function () { this.open = true; };
    const blocked = printOrder(params);
    const cancelled = expect(blocked).rejects.toThrow();
    await vi.waitFor(() => expect(document.querySelector('dialog button:last-child')).toBeTruthy());
    (document.querySelector('dialog button:last-child') as HTMLButtonElement).click();
    await cancelled;
    expect(call.post).not.toHaveBeenCalledWith(expect.stringMatching(/complete_receipt_delivery|qz_print_update/), expect.anything());
  });

  it('rechecks a deferred copy after 301 seconds before navigating to printview', async () => {
    let elapsed = 0;
    vi.mocked(call.post).mockImplementation(async method => {
      if (method.endsWith('begin_bill_copy_dispatch') && elapsed > 300) throw new Error('Replacement already printed');
      return {message: {status: 'RESERVED', delivery: 'browser', copy_number: 1, dispatch_token: 'old', print_url: '/printview?copy_number=1'}};
    });
    openSpy.mockReturnValueOnce(null);
    HTMLDialogElement.prototype.showModal ??= function () { this.open = true; };
    const pending = printBillCopy({orderId: 'C', posProfile: makeProfile(), idempotencyKey: 'key'});
    await vi.waitFor(() => expect(document.querySelector('dialog button')).toBeTruthy());
    elapsed = 301;
    (document.querySelector('dialog button') as HTMLButtonElement).click();
    await expect(pending).rejects.toThrow();
    expect(openSpy.mock.calls.every(([url]) => url === 'about:blank')).toBe(true);
    expect(navigate).not.toHaveBeenCalled();
  });

  it.each([false, true])('retains a lost QZ acknowledgement and accepts physical confirmation=%s', async printed => {
    vi.mocked(call.post).mockImplementation(async method => {
      if (method.endsWith('begin_bill_copy_dispatch')) return { message: { status: 'DISPATCHING' } };
      if (method.endsWith('confirm_bill_copy')) return { message: { status: 'COMPLETED' } };
      return {message: {status: 'RESERVED', delivery: 'qz', html: 'COPY #1', copy_number: 1, dispatch_token: 'owner'}};
    });
    vi.mocked(printWithQz).mockImplementationOnce(async (...args: unknown[]) => {
      await (args[3] as (() => Promise<void>) | undefined)?.();
      throw new Error('QZ acknowledgement lost');
    });
    vi.mocked(window.confirm).mockReturnValueOnce(printed).mockReturnValue(false);
    const job = printBillCopy({orderId: 'C', posProfile: makeProfile({print_type:'qz'}), idempotencyKey: 'key'});
    if (printed) await expect(job).resolves.toMatchObject({status:'COMPLETED'});
    else await expect(job).rejects.toThrow();
    expect(window.confirm).toHaveBeenCalledWith(expect.stringMatching(/did.*print/i));
    expect(openSpy).not.toHaveBeenCalled();
    expect(call.post).not.toHaveBeenCalledWith(expect.stringContaining('release_bill_copy'), expect.anything());
  });

  it('keeps POS open and requires confirmation when a numbered QZ copy falls back', async () => {
    vi.mocked(call.post).mockImplementation(async method => method.endsWith('begin_bill_copy_dispatch') ? { message: {status: 'DISPATCHING'} } : ({ message: { status: 'RESERVED', dispatch_token: 'attempt-1', delivery: 'qz', copy_number: 4,
      html: 'COPY #4', print_url: '/printview?copy_number=4' } }));
    vi.mocked(printWithQz).mockRejectedValue(new Error('QZ_NOT_RUNNING'));
    vi.mocked(window.confirm).mockReturnValueOnce(true).mockReturnValueOnce(false);
    await expect(printBillCopy({ orderId: 'C', posProfile: makeProfile({print_type: 'qz'}), idempotencyKey: 'key' })).rejects.toThrow();
    expect(navigate).toHaveBeenCalledWith('/printview?copy_number=4');
    expect(call.post).not.toHaveBeenCalledWith('ury.ury_pos.api.release_bill_copy', { invoice: 'C', idempotency_key: 'key', dispatch_token: 'attempt-1' });
  });

  it('uses configured letterhead and defaults to Merged POS Invoice Format when print_format is empty or unsafe', async () => {
    await printOrder({
      orderId: 'POS-INV-0001',
      posProfile: makeProfile({ print_format: 'POS Invoice Classic', letter_head: 'Grand Terrace Receipt' }),
      skipStatusUpdate: true,
    });

    expect(openSpy).toHaveBeenCalledTimes(1);
    const url = openSpy.mock.calls[0][0] as string;
    expect(url).toContain(`format=${encodeURIComponent(MERGED_POS_INVOICE_PRINT_FORMAT)}`);
    expect(url).toContain('no_letterhead=0');
    expect(url).toContain(`letterhead=${encodeURIComponent('Grand Terrace Receipt')}`);
  });

  it('sets no_letterhead=1 when no letterhead is configured on the POS Profile', async () => {
    await printOrder({
      orderId: 'POS-INV-0002',
      posProfile: makeProfile({ print_format: '', letter_head: null }),
      skipStatusUpdate: true,
    });

    const url = openSpy.mock.calls[0][0] as string;
    expect(url).toContain(`format=${encodeURIComponent(MERGED_POS_INVOICE_PRINT_FORMAT)}`);
    expect(url).toContain('no_letterhead=1');
  });

  it('forwards configured POS Profile letter_head on QZ Tray getInvoicePrintHtml (P1-5)', async () => {
    await printOrder({
      orderId: 'POS-INV-0003',
      posProfile: makeProfile({
        print_type: 'qz',
        qz_host: 'localhost',
        print_format: 'POS EFRIS Invoice',
        letter_head: 'Grand Terrace Receipt',
      }),
      skipStatusUpdate: true,
    });

    expect(call.get).toHaveBeenCalledWith(
      'frappe.www.printview.get_html_and_style',
      expect.objectContaining({
        doc: 'POS Invoice',
        name: 'POS-INV-0003',
        print_format: MERGED_POS_INVOICE_PRINT_FORMAT,
        no_letterhead: 0,
        letterhead: 'Grand Terrace Receipt',
      })
    );
  });

  it('does not dispatch a numbered copy again after confirmed delivery', async () => {
    vi.mocked(call.post).mockResolvedValueOnce({ message: { status: 'COMPLETED', copy_number: 3 } });
    const result = await printBillCopy({ orderId: 'C', posProfile: makeProfile({ print_type: 'socket' }), idempotencyKey: 'same' });
    expect(result.status).toBe('COMPLETED');
    expect(openSpy).not.toHaveBeenCalled();
    expect(call.post).toHaveBeenCalledTimes(1);
  });

  it('forwards configured POS Profile letter_head in printBillCopy browser fallback URL (P1-5)', async () => {
    vi.mocked(call.post).mockResolvedValueOnce({
      message: { status: 'RESERVED', dispatch_token: 'attempt-1', delivery: 'browser', copy_number: 2 },
    }).mockResolvedValueOnce({ message: { status: 'DISPATCHING' } }).mockResolvedValueOnce({ message: { status: 'COMPLETED' } });

    await printBillCopy({
      orderId: 'POS-INV-0004',
      posProfile: makeProfile({ print_type: 'socket', letter_head: 'Grand Terrace Receipt' }),
      idempotencyKey: 'copy-lh-1',
    });

    expect(openSpy).toHaveBeenCalledTimes(1);
    const url = navigate.mock.lastCall?.[0] as string;
    expect(url).toContain('no_letterhead=0');
    expect(url).toContain(`letterhead=${encodeURIComponent('Grand Terrace Receipt')}`);
    expect(url).toContain('copy_number=2');
  });
});
