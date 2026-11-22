import { beforeEach, describe, expect, it, vi } from 'vitest'

const { callGetMock, callPostMock } = vi.hoisted(() => ({ callGetMock: vi.fn(), callPostMock: vi.fn() }))
vi.mock('@ury/core', () => ({ call: { get: callGetMock, post: callPostMock } }))

import { AVAILABILITY_REASON_MESSAGES, getAvailabilityMessage, getMenuAvailability, isUnavailable } from './availability'

const payload = {
  FISH: { item_code: 'FISH', sellable: true, available_qty: null, reason_code: 'AVAILABLE' },
}

describe('availability service (self-order)', () => {
  beforeEach(() => vi.resetAllMocks())

  it('posts only the ordering session to the guest batch endpoint', async () => {
    callPostMock.mockResolvedValueOnce({ message: payload })
    expect(await getMenuAvailability('ORDERING-SESSION')).toEqual(payload)
    expect(callPostMock).toHaveBeenCalledExactlyOnceWith(
      'ury.ury.api.self_ordering.get_customer_menu_availability', { session: 'ORDERING-SESSION' },
    )
    expect(callGetMock).not.toHaveBeenCalled()
  })

  it('makes a live request every time, including at submit', async () => {
    const disabled = { FISH: { ...payload.FISH, sellable: false, available_qty: 0, reason_code: 'ITEM_DISABLED' } }
    callPostMock.mockResolvedValueOnce({ message: payload }).mockResolvedValueOnce({ message: disabled })
    expect(await getMenuAvailability('ORDERING-SESSION')).toEqual(payload)
    expect(await getMenuAvailability('ORDERING-SESSION')).toEqual(disabled)
    expect(callPostMock).toHaveBeenCalledTimes(2)
  })

  it('distinguishes unknown, unconstrained, and explicitly unavailable', () => {
    expect(isUnavailable(undefined)).toBe(false)
    expect(isUnavailable(payload.FISH)).toBe(false)
    expect(isUnavailable({ ...payload.FISH, sellable: false })).toBe(true)
    expect(isUnavailable({ ...payload.FISH, available_qty: 0 })).toBe(true)
  })

  it('maps every documented reason_code to a user-facing message', () => {
    for (const code of Object.keys(AVAILABILITY_REASON_MESSAGES)) {
      expect(getAvailabilityMessage(code)).toBe(AVAILABILITY_REASON_MESSAGES[code])
    }
    expect(getAvailabilityMessage('SOME_UNKNOWN_CODE')).toBe('Currently unavailable')
    expect(getAvailabilityMessage(null)).toBe('Currently unavailable')
  })
})
