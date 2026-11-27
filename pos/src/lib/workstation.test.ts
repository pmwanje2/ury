import { describe, expect, it } from 'vitest';
import { workstation, parsePLU } from './workstation';

describe('profile workstation', () => {
  it.each([
    ['UGX', [5000, 10000, 20000, 50000]],
    ['USD', [1, 5, 10, 20, 50, 100]],
    ['KWD', [1, 5, 10, 20]],
  ])('uses denominations of %s', (currency, expected) => {
    expect(workstation(undefined, String(currency)).cash).toEqual(expected);
  });
  it('accepts profile pages, ordered function keys and currency-specific denominations', () => {
    const pages = [{ label: 'Terrace', items: ['001', '010'] }];
    const json = JSON.stringify({ pages, keys: ['repeat', 'quantity'], cash: { USD: [2, 5, -1, 2] } });
    expect(workstation(json, 'USD')).toEqual({ pages, keys: ['repeat', 'quantity'], cash: [2, 5] });
    expect(workstation(json, 'KWD').cash).toEqual([1, 5, 10, 20]);
  });
  it('falls back safely on stale configuration', () => {
    expect(workstation('{', 'JPY').pages).toEqual([]);
  });
  it('keeps leading zeroes and rejects invalid quantities', () => {
    expect(parsePLU('3×0012')).toEqual({ code: '0012', quantity: 3 });
    expect(parsePLU('0012')).toEqual({ code: '0012', quantity: 1 });
    expect(parsePLU('0×1')).toBeNull();
    expect(parsePLU('-3×2')).toBeNull();
  });
});
