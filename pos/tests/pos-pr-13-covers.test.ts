import { describe, expect, it } from 'vitest';
import { coversLimit, normalizeCovers } from '@ury/core/covers';

describe('Outlet covers', () => {
  it('preserves integer cover counts above fifty and uses order-type overrides', () => {
    const profile = { max_covers: 80, order_type_cover_limits: { 'Dine In': 75, 'Take Away': 0 } };
    expect(normalizeCovers(' 66 ')).toBe(66);
    expect(normalizeCovers('4.5')).toBe(1);
    expect(coversLimit(profile, 'Dine In')).toBe(75);
    expect(coversLimit(profile, 'Take Away')).toBe(Infinity);
    expect(coversLimit(profile, 'Delivery')).toBe(80);
  });
});
