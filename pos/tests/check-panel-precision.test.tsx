import { render, screen } from '@testing-library/react';
import { expect, test, vi } from 'vitest';
import MergedBillPanel from '../src/components/MergedBillPanel';
import SplitGroupPanel from '../src/components/SplitGroupPanel';
import { getPOSInvoiceItems, getSplitGroup } from '../src/lib/invoice-api';

vi.mock('../src/lib/invoice-api', () => ({getPOSInvoiceItems: vi.fn(), getSplitGroup: vi.fn()}));

test.each([
  ['KWD', 1, 'KWD 1.000'], ['USD', 10, 'USD 10.00'], ['UGX', 5000, 'UGX 5,000'],
] as const)('related checks use full %s precision', async (currency, total, formatted) => {
  const order = {name: 'A', currency, rounded_total: total, custom_merged_pos_invoice: 'B', custom_merged_total: 0};
  vi.mocked(getSplitGroup).mockResolvedValue({invoices: [order, {...order, name: 'B'}]} as Awaited<ReturnType<typeof getSplitGroup>>);
  vi.mocked(getPOSInvoiceItems).mockResolvedValue({items: []} as Awaited<ReturnType<typeof getPOSInvoiceItems>>);
  render(<><SplitGroupPanel invoiceName="A" onOpenInvoice={vi.fn()} /><MergedBillPanel order={order} /></>);
  await vi.waitFor(() => expect(screen.getAllByText(formatted)).toHaveLength(3));
});
