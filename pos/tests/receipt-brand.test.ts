import { expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { getInvoicePrintHtml as posHtml } from '../src/lib/invoice-api';
import { getInvoicePrintHtml as dashboardHtml } from '../../frontend/src/pages/Pos/lib/invoice-api';
import { getInvoicePrintHtml as serveHtml } from '../../serve/src/lib/invoice-api';
import { printOrder as posPrint } from '../src/lib/print';

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

it('POS fiscal browser printview explicitly suppresses letterhead', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    try {
      await posPrint({ orderId: 'INV-1', posProfile: { print_type: 'socket' } as never, skipStatusUpdate: true, printFormat: 'Thermal receipt' });
      const params = new URL(String(open.mock.lastCall![0]), location.origin).searchParams;
      expect(params.get('format')).toBe('Thermal receipt');
      expect(params.get('no_letterhead')).toBe('1');
      expect(params.get('letterhead')).toBe('No Letterhead');
      expect(params.get('settings')).toBe('{}');
    } finally { open.mockRestore(); }
  },
);

it('Dashboard authorizes its browser snapshot with the selected format and default letterhead', async () => {
  const { printOrder } = await import('../../frontend/src/pages/Pos/lib/print');
  vi.mocked(call.post).mockResolvedValue({ message: { html: 'Receipt', invoice_revision: 'v', delivery_id: 'd',
    print_url: '/snapshot/d' } });
  const open = vi.spyOn(window, 'open').mockReturnValue({ closed: true } as Window);
  const confirmed = vi.spyOn(window, 'confirm').mockReturnValue(true);
  try {
    await printOrder({ orderId: 'INV-1', posProfile: { print_type: 'socket' } as never, printFormat: 'Thermal receipt' });
    expect(call.post).toHaveBeenCalledWith(expect.stringContaining('prepare_receipt_delivery'),
      expect.objectContaining({ print_format: 'Thermal receipt', letterhead: undefined }));
    expect(open).toHaveBeenCalledWith('/snapshot/d', '_blank');
  } finally { open.mockRestore(); confirmed.mockRestore(); }
});
