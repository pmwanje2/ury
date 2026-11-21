import { expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { getInvoicePrintHtml as posHtml } from '../src/lib/invoice-api';
import { getInvoicePrintHtml as dashboardHtml } from '../../frontend/src/pages/Pos/lib/invoice-api';
import { getInvoicePrintHtml as serveHtml } from '../../serve/src/lib/invoice-api';
import { printOrder as posPrint } from '../src/lib/print';
import { printOrder as dashboardPrint } from '../../frontend/src/pages/Pos/lib/print';

vi.mock('@ury/core', async (importOriginal) => ({
  ...await importOriginal<typeof import('@ury/core')>(),
  call: { get: vi.fn(), post: vi.fn() },
}));

it.each([['POS', posHtml], ['dashboard', dashboardHtml], ['Serve', serveHtml]] as const)(
  '%s QZ does not override the invoice letter head or site print settings', async (_, html) => {
    vi.mocked(call.get).mockResolvedValue({ message: { html: '<p>Receipt</p>' } });
    await html('INV-1', 'Thermal receipt');
    const [, params] = vi.mocked(call.get).mock.lastCall!;
    expect(params).toMatchObject({ name: 'INV-1', print_format: 'Thermal receipt' });
    expect(params).not.toHaveProperty('no_letterhead');
    expect(params).not.toHaveProperty('letterhead');
  },
);

it.each([['POS', posPrint], ['dashboard', dashboardPrint]] as const)(
  '%s printview respects the native receipt identity', async (_, print) => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    try {
      await print({ orderId: 'INV-1', posProfile: { print_type: 'socket' } as never, printFormat: 'Thermal receipt' });
      const params = new URL(String(open.mock.lastCall![0]), location.origin).searchParams;
      expect(params.get('format')).toBe('Thermal receipt');
      expect(params.has('no_letterhead') || params.has('letterhead')).toBe(false);
    } finally { open.mockRestore(); }
  },
);
