import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getMenuAvailability = vi.fn()

vi.mock('./availability-api', () => ({
  getMenuAvailability: (...args: any[]) => getMenuAvailability(...args),
}))
vi.mock('./realtime', () => ({ useMenuAvailabilityChannel: () => {} }))

import { cartQtyForItem, remainingHeadroom, useCartAvailability } from './cart-capacity'
import type { ItemAvailability } from './availability-api'

const buildAvailability = (overrides: Partial<ItemAvailability> = {}): ItemAvailability => ({
  item_code: 'COFFEE',
  sellable: true,
  available_qty: 5,
  production_policy: 'MADE_TO_ORDER',
  company: 'URY',
  branch: 'Kozhikode',
  department: null,
  production_unit: null,
  warehouse: null,
  plan_qty: null,
  plan_remaining: null,
  fg_available: null,
  max_producible: null,
  blocking_component: null,
  reason_code: 'AVAILABLE',
  as_of: '2026-09-15 10:00:00',
  ...overrides,
})

describe('cartQtyForItem', () => {
  it('sums quantity only across lines matching the given item_code', () => {
    const lines = [
      { item: 'COFFEE', quantity: 2 },
      { item: 'TEA', quantity: 1 },
      { item: 'COFFEE', quantity: 3 },
    ]
    expect(cartQtyForItem(lines, 'COFFEE')).toBe(5)
    expect(cartQtyForItem(lines, 'TEA')).toBe(1)
    expect(cartQtyForItem(lines, 'NOT-IN-CART')).toBe(0)
  })

  it('returns 0 for an empty cart', () => {
    expect(cartQtyForItem([], 'COFFEE')).toBe(0)
  })
})

describe('remainingHeadroom', () => {
  it('returns undefined ("do not gate") when availability is unknown', () => {
    expect(remainingHeadroom(undefined, 2)).toBeUndefined()
  })

  it('subtracts the current cart qty from available_qty', () => {
    expect(remainingHeadroom(buildAvailability({ available_qty: 5 }), 2)).toBe(3)
  })

  it('can go negative when the cart already exceeds known availability (does not clamp)', () => {
    expect(remainingHeadroom(buildAvailability({ available_qty: 2 }), 4)).toBe(-2)
  })
})

// B04: this hook is the actual fix for "no real-time cart capacity
// validation" -- it is DISPLAY-ONLY (never blocks sync_order itself) and
// must fail open (not gate) on lookup failure, per the module's own
// documented policy. These tests pin both properties down.
describe('useCartAvailability', () => {
  beforeEach(() => {
    getMenuAvailability.mockReset()
  })

  it('does not fetch when branch or company is missing', () => {
    const { result } = renderHook(() =>
      useCartAvailability({ itemCodes: ['COFFEE'], branch: undefined, company: 'URY' }),
    )
    expect(getMenuAvailability).not.toHaveBeenCalled()
    expect(result.current.availabilityByItem).toEqual({})
    expect(result.current.loading).toBe(false)
  })

  it('does not fetch when itemCodes is empty', () => {
    const { result } = renderHook(() =>
      useCartAvailability({ itemCodes: [], branch: 'Kozhikode', company: 'URY' }),
    )
    expect(getMenuAvailability).not.toHaveBeenCalled()
    expect(result.current.availabilityByItem).toEqual({})
  })

  it('fetches one batch of distinct item codes', async () => {
    getMenuAvailability.mockResolvedValue({ COFFEE: buildAvailability(), TEA: buildAvailability({ item_code: 'TEA' }) })

    const { result } = renderHook(() =>
      useCartAvailability({ itemCodes: ['COFFEE', 'COFFEE', 'TEA'], branch: 'Kozhikode', company: 'URY' }),
    )

    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(getMenuAvailability).toHaveBeenCalledExactlyOnceWith('Kozhikode', 'URY', ['COFFEE', 'TEA'])
    expect(Object.keys(result.current.availabilityByItem).sort()).toEqual(['COFFEE', 'TEA'])
  })

  it('fails open when the batch cannot be loaded', async () => {
    getMenuAvailability.mockRejectedValue(new Error('network error'))

    const { result } = renderHook(() =>
      useCartAvailability({ itemCodes: ['COFFEE', 'TEA'], branch: 'Kozhikode', company: 'URY' }),
    )

    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.availabilityByItem).toEqual({})
  })

  it('resets to empty and does not re-fetch stale results when the item set changes to empty', async () => {
    getMenuAvailability.mockResolvedValue({ COFFEE: buildAvailability() })

    const { result, rerender } = renderHook(
      ({ itemCodes }) => useCartAvailability({ itemCodes, branch: 'Kozhikode', company: 'URY' }),
      { initialProps: { itemCodes: ['COFFEE'] } },
    )
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(Object.keys(result.current.availabilityByItem)).toEqual(['COFFEE'])

    rerender({ itemCodes: [] })
    await waitFor(() => expect(result.current.availabilityByItem).toEqual({}))
  })
})
