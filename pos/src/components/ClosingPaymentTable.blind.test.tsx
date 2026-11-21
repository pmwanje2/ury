// Ported from fork pos/src/components/ClosingPaymentTable.test.tsx (43ea6fb1):
// node:test static-markup harness converted to Vitest.
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { formatCurrency } from '@ury/core';
import ClosingPaymentTable from './ClosingPaymentTable';

const row = {
  mode_of_payment: 'Cash',
  opening_amount: 10,
  expected_amount: 123,
  closing_amount: 150,
  difference: 27,
};
const props = {
  rows: [row],
  touchedModes: new Set(['Cash']),
  onChange: () => {},
};

describe('ClosingPaymentTable blind cash count', () => {
  it('hides the expected and difference columns and their values', () => {
    const blind = renderToStaticMarkup(<ClosingPaymentTable {...props} blindCashCount />);
    expect(blind).not.toContain('Expected');
    expect(blind).not.toContain('Difference');
    expect(blind).not.toContain(formatCurrency(row.expected_amount));
    expect(blind).not.toContain(formatCurrency(row.difference));
    expect(blind).not.toContain('—');
    expect((blind.match(/<th\b/g) || []).length).toBe(3);
    expect((blind.match(/<td\b/g) || []).length).toBe(3);
    expect(blind).toContain('Cash');
    expect(blind).toContain('Opening');
    expect(blind).toContain('Closing');
    expect(blind).toContain('type="number"');
    expect(blind).toContain('value="150"');
  });

  it('keeps the normal five-column closing table when blind count is off', () => {
    const normal = renderToStaticMarkup(<ClosingPaymentTable {...props} />);
    expect(normal).toContain('Expected');
    expect(normal).toContain('Difference');
    expect(normal).toContain(formatCurrency(row.expected_amount));
    expect(normal).toContain(formatCurrency(row.difference));
    expect((normal.match(/<th\b/g) || []).length).toBe(5);
    expect((normal.match(/<td\b/g) || []).length).toBe(5);
    expect(row.expected_amount).toBe(123);
  });
});
