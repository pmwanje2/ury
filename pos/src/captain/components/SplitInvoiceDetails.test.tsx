import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import SplitInvoiceDetails from './SplitInvoiceDetails';

vi.mock('../../lib/invoice-api', () => ({ getPOSInvoiceItems: vi.fn() }));

const items = [{ name: 'ROW-1', item_name: 'Soup', qty: 1, rate: 10, price_list_rate: 10, amount: 10 }];

describe('SplitInvoiceDetails guest subtitle', () => {
  it('does not repeat a customer the guest label already names', () => {
    render(<SplitInvoiceDetails onClose={vi.fn()} loadedItems={items} invoice={{
      name: 'INV-B', custom_guest_label: 'Dr. Elena Rostova (Rm 408)', customer_name: 'Dr. Elena Rostova', customer: 'CUST-0001',
    }} />);
    expect(screen.getByText('Dr. Elena Rostova (Rm 408)')).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('(Rm 408) · Dr. Elena Rostova');
  });

  it('keeps both when the label and the customer differ', () => {
    render(<SplitInvoiceDetails onClose={vi.fn()} loadedItems={items} invoice={{
      name: 'INV-B', custom_guest_label: 'Susan', customer_name: 'Walk In', customer: 'Walk In',
    }} />);
    expect(screen.getByText('Susan · Walk In')).toBeInTheDocument();
  });
});
