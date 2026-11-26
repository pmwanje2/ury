import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@ury/core', async () => {
  const actual = await vi.importActual<typeof import('@ury/core')>('@ury/core');
  return {
    ...actual,
    call: {
      get: vi.fn().mockResolvedValue({ message: { html: '<div class="sr">ok</div>' } }),
      post: vi.fn().mockResolvedValue({ message: 'Success' }),
    },
    db: {
      getDoc: vi.fn(),
      getDocList: vi.fn(),
    },
    printWithQz: vi.fn(),
  };
});

import { call, printWithQz } from '@ury/core';
import { MERGED_POS_INVOICE_PRINT_FORMAT, resolvePrintFormat } from './invoice-api';
import { printOrder } from './print';
import type { PosProfileCombined } from './pos-profile-api';

describe('frontend Pos printOrder & resolvePrintFormat (P1-5)', () => {
  const openSpy = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('open', openSpy.mockReturnValue({ closed: true }));
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
    vi.mocked(call.post).mockResolvedValue({ message: {
      delivery_id: 'd', invoice_revision: 'v', html: 'Receipt', print_url: '/authorized-receipt',
    } });
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
    } as PosProfileCombined;
  }

  it('rejects all unsafe POS Invoice print formats and defaults to Merged POS Invoice Format', () => {
    for (const unsafe of [
      '',
      null,
      'POS Invoice Classic',
      'POS Invoice Bordered',
      'POS Invoice Modern',
      'POS Invoice Modern with Images',
      'POS Invoice',
      'Return POS Invoice',
      'EFRIS POS Receipt 80mm',
      'POS EFRIS Invoice',
    ]) {
      expect(resolvePrintFormat({ custom_merged_pos_invoice: null }, unsafe)).toBe(
        MERGED_POS_INVOICE_PRINT_FORMAT
      );
    }
    expect(resolvePrintFormat({ custom_merged_pos_invoice: null }, 'Restaurant Receipt 80mm')).toBe(
      'Restaurant Receipt 80mm'
    );
  });

  it.each([0, 1])('requires an explicit network acknowledgement (multiple_cashier=%s)', async multiple_cashier => {
    vi.mocked(call.post).mockResolvedValue({ message: { status: 'Failure', message: 'paper out' } });
    await expect(printOrder({ orderId: 'INV', posProfile: makeProfile({ print_type: 'network', multiple_cashier }) }))
      .rejects.toThrow('paper out');
    expect(call.post).toHaveBeenCalledTimes(1);
  });

  it('accepts a network receipt without a second status mutation', async () => {
    vi.mocked(call.post).mockResolvedValue({ message: { status: 'Success' } });
    expect(await printOrder({ orderId: 'INV', posProfile: makeProfile({ print_type: 'network' }) })).toBe('network');
    expect(call.post).toHaveBeenCalledTimes(1);
  });

  it('leaves a cancelled browser receipt pending', async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    await expect(printOrder({ orderId: 'INV', posProfile: makeProfile() })).rejects.toThrow(/confirm/i);
    expect(call.post).toHaveBeenCalledTimes(1);
    expect(call.post).toHaveBeenCalledWith(expect.stringContaining('prepare_receipt_delivery'), expect.anything());
  });

  it('lets the cashier cancel a blocked popup without completing the receipt', async () => {
    openSpy.mockReturnValue(null);
    HTMLDialogElement.prototype.showModal = function () { this.querySelectorAll('button')[1].click(); };
    await expect(printOrder({ orderId: 'INV', posProfile: makeProfile() })).rejects.toThrow(/cancel/i);
    expect(call.post).toHaveBeenCalledTimes(1);
    expect(call.post).toHaveBeenCalledWith(expect.stringContaining('prepare_receipt_delivery'), expect.anything());
  });

  it('forwards configured letter_head on browser printview and QZ Tray', async () => {
    await printOrder({
      orderId: 'POS-INV-FE-1',
      posProfile: makeProfile({
        print_type: 'socket',
        print_format: 'POS Invoice Classic',
        letter_head: 'Grand Terrace Receipt',
      } as Partial<PosProfileCombined>),
    });
    expect(openSpy).toHaveBeenCalledTimes(1);
    expect(openSpy).toHaveBeenCalledWith('/authorized-receipt', '_blank');
    expect(call.post).toHaveBeenNthCalledWith(1, expect.stringContaining('prepare_receipt_delivery'),
      expect.objectContaining({ print_format: MERGED_POS_INVOICE_PRINT_FORMAT, letterhead: 'Grand Terrace Receipt' }));
    expect(call.post).toHaveBeenNthCalledWith(2, expect.stringContaining('complete_receipt_delivery'),
      { invoice: 'POS-INV-FE-1', delivery_id: 'd' });

    vi.mocked(call.post).mockClear().mockResolvedValue({ message: { delivery_id: 'd', invoice_revision: 'v', html: 'COPY #2 / REPRINT' } });
    await printOrder({
      orderId: 'POS-INV-FE-2',
      posProfile: makeProfile({
        print_type: 'qz',
        qz_host: 'localhost',
        print_format: 'POS EFRIS Invoice',
        letter_head: 'Grand Terrace Receipt',
      } as Partial<PosProfileCombined>),
    });
    expect(call.post).toHaveBeenNthCalledWith(1, expect.stringContaining('prepare_receipt_delivery'),
      expect.objectContaining({ print_format: MERGED_POS_INVOICE_PRINT_FORMAT, letterhead: 'Grand Terrace Receipt' }));
    expect(printWithQz).toHaveBeenCalledWith('localhost', 'COPY #2 / REPRINT', null);
    expect(vi.mocked(call.post).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(printWithQz).mock.invocationCallOrder[0]);
    expect(call.post).toHaveBeenNthCalledWith(2, expect.stringContaining('complete_receipt_delivery'),
      { invoice: 'POS-INV-FE-2', delivery_id: 'd' });
  });
});
