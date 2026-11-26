import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { PosProfileCombined } from './pos-profile-api'

const printWithQz = vi.fn()

vi.mock('@ury/core', () => ({
  call: { post: vi.fn() },
  printWithQz: (...args: unknown[]) => printWithQz(...args),
}))

vi.mock('./invoice-api', async () => {
  const actual = await vi.importActual<typeof import('./invoice-api')>('./invoice-api')
  return {
    ...actual,
    getInvoicePrintHtml: vi.fn(async () => '<html />'),
    networkPrint: vi.fn(async () => 'Success'),
    selectNetworkPrinter: vi.fn(async () => 'Success'),
    printPosPage: vi.fn(async () => undefined),
    updatePrintStatus: vi.fn(async () => undefined),
    assertNetworkPrintSuccess: actual.assertNetworkPrintSuccess,
  }
})

import { call } from '@ury/core'
import { printOrder } from './print'
import {
  assertNetworkPrintSuccess,
  networkPrint,
  printPosPage,
  updatePrintStatus,
} from './invoice-api'

describe('printOrder transports', () => {
  const baseProfile = {
    name: 'POS-PROFILE',
    print_format: 'Restaurant Receipt 80mm',
    letter_head: 'Grand Terrace Receipt',
    printer: 'Printer-1',
    cashier: 'cashier@example.com',
    multiple_cashier: 0,
    qz_host: null,
  } as unknown as PosProfileCombined

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('uses print_pos_page for socket print with safe custom format and does not mark via qz_print_update', async () => {
    await printOrder({
      orderId: 'INV-1',
      posProfile: { ...baseProfile, print_type: 'socket' },
    })
    expect(printPosPage).toHaveBeenCalledWith('INV-1', 'Restaurant Receipt 80mm')
    expect(updatePrintStatus).not.toHaveBeenCalled()
  })

  it('replaces unsafe or empty print_format with Merged POS Invoice Format and forwards letter_head on QZ (P1-5)', async () => {
    await printOrder({
      orderId: 'INV-UNSAFE',
      posProfile: { ...baseProfile, print_type: 'socket', print_format: 'POS Invoice Classic' },
    })
    expect(printPosPage).toHaveBeenCalledWith('INV-UNSAFE', 'Merged POS Invoice Format')

    const copy = '<p>COPY #2 / REPRINT</p>'
    vi.mocked(call.post).mockResolvedValue({ message: { delivery_id: 'd', html: copy, invoice_revision: 'rev' } })
    await printOrder({
      orderId: 'INV-QZ',
      posProfile: {
        ...baseProfile,
        print_type: 'qz',
        qz_host: 'localhost',
        print_format: 'POS EFRIS Invoice',
        letter_head: 'Grand Terrace Receipt',
      },
    })
    expect(call.post).toHaveBeenNthCalledWith(1, expect.stringContaining('prepare_receipt_delivery'),
      expect.objectContaining({ print_format: 'Merged POS Invoice Format', letterhead: 'Grand Terrace Receipt' }))
    expect(printWithQz).toHaveBeenCalledWith('localhost', copy, 'Printer-1')
    expect(call.post).toHaveBeenNthCalledWith(2, expect.stringContaining('complete_receipt_delivery'),
      { invoice: 'INV-QZ', delivery_id: 'd' })
    expect(vi.mocked(call.post).mock.invocationCallOrder[0]).toBeLessThan(printWithQz.mock.invocationCallOrder[0])
  })

  it('honors network Success without double-marking', async () => {
    await printOrder({
      orderId: 'INV-2',
      posProfile: { ...baseProfile, print_type: 'network' },
    })
    expect(networkPrint).toHaveBeenCalled()
    expect(updatePrintStatus).not.toHaveBeenCalled()
  })

  it('assertNetworkPrintSuccess rejects printer failure strings', () => {
    expect(() => assertNetworkPrintSuccess('Failed to connect to the printer: down')).toThrow(
      /Failed to connect/
    )
    expect(() => assertNetworkPrintSuccess('Success')).not.toThrow()
  })
})
