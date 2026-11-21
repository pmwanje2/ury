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
  '%s QZ explicitly requests the invoice without a letterhead', async (_, html) => {
    vi.mocked(call.get).mockResolvedValue({ message: { html: '<p>Receipt</p>' } });
    await html('INV-1', 'Thermal receipt');
    const [, params] = vi.mocked(call.get).mock.lastCall!;
    expect(params).toMatchObject({ name: 'INV-1', print_format: 'Thermal receipt' });
    expect(params).toMatchObject({ no_letterhead: 1, letterhead: 'No Letterhead', settings: {} });
  },
);

it.each([['POS', posPrint], ['dashboard', dashboardPrint]] as const)(
  '%s browser printview explicitly suppresses letterhead', async (_, print) => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    try {
      await print({ orderId: 'INV-1', posProfile: { print_type: 'socket' } as never, printFormat: 'Thermal receipt' });
      const params = new URL(String(open.mock.lastCall![0]), location.origin).searchParams;
      expect(params.get('format')).toBe('Thermal receipt');
      expect(params.get('no_letterhead')).toBe('1');
      expect(params.get('letterhead')).toBe('No Letterhead');
      expect(params.get('settings')).toBe('{}');
    } finally { open.mockRestore(); }
  },
);
