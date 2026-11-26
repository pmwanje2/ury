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

  beforeEach(() => {
    vi.clearAllMocks();
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

  it('offers browser receipt delivery when the local tray cannot connect', async () => {
    vi.mocked(printWithQz).mockRejectedValueOnce(new Error('QZ_NOT_RUNNING'));
    vi.spyOn(window, 'confirm').mockReturnValueOnce(true);
    const delivery = await printOrder({ orderId: 'INV-1', posProfile: makeProfile({
      print_type: 'qz', printer: 'Front Desk',
    }) });
    expect(printWithQz).toHaveBeenCalledWith('localhost', expect.any(String), 'Front Desk');
    expect(delivery).toBe('socket');
    expect(openSpy.mock.lastCall?.[0]).toContain('trigger_print=1');
    expect(call.post).toHaveBeenCalledTimes(1);
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

  it('forwards configured POS Profile letter_head in printBillCopy browser fallback URL (P1-5)', async () => {
    vi.mocked(call.post).mockResolvedValueOnce({
      message: { status: 'COMPLETED', delivery: 'browser', copy_number: 2 },
    });

    await printBillCopy({
      orderId: 'POS-INV-0004',
      posProfile: makeProfile({ print_type: 'socket', letter_head: 'Grand Terrace Receipt' }),
      idempotencyKey: 'copy-lh-1',
    });

    expect(openSpy).toHaveBeenCalledTimes(1);
    const url = openSpy.mock.calls[0][0] as string;
    expect(url).toContain('no_letterhead=0');
    expect(url).toContain(`letterhead=${encodeURIComponent('Grand Terrace Receipt')}`);
    expect(url).toContain('copy_number=2');
  });
});
