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

import { call } from '@ury/core';
import { MERGED_POS_INVOICE_PRINT_FORMAT, resolvePrintFormat } from './invoice-api';
import { printOrder } from './print';
import type { PosProfileCombined } from './pos-profile-api';

describe('frontend Pos printOrder & resolvePrintFormat (P1-5)', () => {
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
    const url = openSpy.mock.calls[0][0] as string;
    expect(url).toContain(`format=${encodeURIComponent(MERGED_POS_INVOICE_PRINT_FORMAT)}`);
    expect(url).toContain('no_letterhead=0');
    expect(url).toContain(`letterhead=${encodeURIComponent('Grand Terrace Receipt')}`);

    await printOrder({
      orderId: 'POS-INV-FE-2',
      posProfile: makeProfile({
        print_type: 'qz',
        qz_host: 'localhost',
        print_format: 'POS EFRIS Invoice',
        letter_head: 'Grand Terrace Receipt',
      } as Partial<PosProfileCombined>),
    });
    expect(call.get).toHaveBeenCalledWith(
      'frappe.www.printview.get_html_and_style',
      expect.objectContaining({
        doc: 'POS Invoice',
        name: 'POS-INV-FE-2',
        print_format: MERGED_POS_INVOICE_PRINT_FORMAT,
        no_letterhead: 0,
        letterhead: 'Grand Terrace Receipt',
      })
    );
  });
});
