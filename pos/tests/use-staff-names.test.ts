import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useStaffNames } from '../src/lib/use-staff-names';

// Order cards name the waiter from their User record. A lookup that fails for
// a passing reason (network drop, 502) must be asked again, or the queue shows
// email-guessed names for as long as the Orders page stays open.

const { getUserFullNames } = vi.hoisted(() => ({ getUserFullNames: vi.fn() }));
vi.mock('../src/captain/lib/captain-table-api', () => ({ getUserFullNames }));

const WAITER = 'samuel.otieno@grandhotel.example';

async function settle(ms = 0) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

beforeEach(() => {
  vi.useFakeTimers();
  getUserFullNames.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('useStaffNames', () => {
  it('asks again after a lookup fails, and names the waiter once it succeeds', async () => {
    getUserFullNames
      .mockRejectedValueOnce(Object.assign(new Error('Bad Gateway'), { httpStatus: 502 }))
      .mockResolvedValueOnce(new Map([[WAITER, 'Samuel Otieno']]));
    const { result } = renderHook(() => useStaffNames([WAITER]));
    await settle();
    expect(getUserFullNames).toHaveBeenCalledTimes(1);
    expect(result.current.get(WAITER)).toBeUndefined();

    await settle(30_000);
    expect(getUserFullNames).toHaveBeenCalledTimes(2);
    expect(getUserFullNames).toHaveBeenLastCalledWith([WAITER]);
    expect(result.current.get(WAITER)).toBe('Samuel Otieno');
  });

  it('keeps asking while the lookups keep failing', async () => {
    getUserFullNames.mockRejectedValue(new TypeError('Failed to fetch'));
    renderHook(() => useStaffNames([WAITER]));
    await settle();
    await settle(30_000);
    await settle(30_000);
    expect(getUserFullNames).toHaveBeenCalledTimes(3);
  });

  it('does not ask again when reading User records is refused', async () => {
    getUserFullNames.mockRejectedValue(Object.assign(new Error('Not permitted'), { httpStatus: 403, exc_type: 'PermissionError' }));
    renderHook(() => useStaffNames([WAITER]));
    await settle();
    await settle(120_000);
    expect(getUserFullNames).toHaveBeenCalledTimes(1);
  });

  it('asks once for each id it has read', async () => {
    getUserFullNames.mockResolvedValue(new Map([[WAITER, 'Samuel Otieno']]));
    const { result, rerender } = renderHook(({ ids }) => useStaffNames(ids), {
      initialProps: { ids: [WAITER] as Array<string | null> },
    });
    await settle();
    rerender({ ids: [WAITER, null] });
    await settle(120_000);
    expect(getUserFullNames).toHaveBeenCalledTimes(1);
    expect(result.current.get(WAITER)).toBe('Samuel Otieno');
  });
});
