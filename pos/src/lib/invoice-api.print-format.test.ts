import { describe, expect, it, vi, beforeEach } from 'vitest';

vi.mock('../offline/cashier', () => ({
  assertOfflineActionAllowed: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('@ury/core', () => ({
  call: {
    get: vi.fn(),
    post: vi.fn().mockResolvedValue({ message: {} }),
  },
  db: {
    getDoc: vi.fn(),
    getDocList: vi.fn(),
  },
  printWithQz: vi.fn(),
  ownedSessionKey: vi.fn(),
}));

import { MERGED_POS_INVOICE_PRINT_FORMAT, resolvePrintFormat } from './invoice-api';
import { printOrder } from './print';
import type { PosProfileCombined } from './pos-profile-api';

describe('resolvePrintFormat safety & default guest-check fallback', () => {
  it('defaults to Merged POS Invoice Format when defaultFormat is empty or null', () => {
    expect(resolvePrintFormat({ custom_merged_pos_invoice: null }, null)).toBe(MERGED_POS_INVOICE_PRINT_FORMAT);
    expect(resolvePrintFormat({ custom_merged_pos_invoice: null }, '')).toBe(MERGED_POS_INVOICE_PRINT_FORMAT);
    expect(resolvePrintFormat({ custom_merged_pos_invoice: null }, '   ')).toBe(MERGED_POS_INVOICE_PRINT_FORMAT);
  });

  it('never returns unsafe ERPNext standard POS Invoice formats', () => {
    for (const unsafe of [
      'POS Invoice Classic',
      'POS Invoice Bordered',
      'POS Invoice Modern',
      'POS Invoice Modern with Images',
      'POS Invoice',
      'Return POS Invoice',
      'EFRIS POS Receipt 80mm',
    ]) {
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

describe('printOrder browser printview URL', () => {
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
});
