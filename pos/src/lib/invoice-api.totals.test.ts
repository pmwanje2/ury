import { describe, expect, it, vi } from 'vitest';

vi.mock('@ury/core', () => ({ call: { post: vi.fn(), get: vi.fn() }, db: {} }));

import { getCombinedOrderTotals } from './invoice-api';

// The till shows and charges the totals the server computed (G17): the server's rounded total
// (currency precision, smallest fraction, the site's rounding) or, where the profile does not
// round, its grand total. A legacy linked bill adds its partner's server total unrounded.

describe('getCombinedOrderTotals', () => {
  it('keeps the server rounded total and the grand total apart', () => {
    expect(getCombinedOrderTotals({ grand_total: 12.34, rounded_total: 12.35 })).toEqual({
      grandTotal: 12.34, roundedTotal: 12.35,
    });
  });

  it('uses the grand total when the profile does not round', () => {
    expect(getCombinedOrderTotals({ grand_total: 12.34, rounded_total: 0 })).toEqual({
      grandTotal: 12.34, roundedTotal: 12.34,
    });
  });

  it("adds a linked partner's server total without rounding it again", () => {
    expect(getCombinedOrderTotals({ grand_total: 12.34, rounded_total: 12.35, custom_merged_total: 7.5 })).toEqual({
      grandTotal: 19.84, roundedTotal: 19.85,
    });
  });
});
