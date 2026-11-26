import { dispatchReceipt } from "@ury/core/receipt";
import { call, printWithQz } from '@ury/core'
import {
  assertNetworkPrintSuccess,
  networkPrint,
  printPosPage,
  resolvePrintFormat,
  selectNetworkPrinter,
} from './invoice-api'
import { PosProfileCombined } from './pos-profile-api'

interface PrintOrderParams {
  orderId: string
  posProfile: PosProfileCombined
  printFormat?: string | null
}

export async function printOrder({
  orderId,
  posProfile,
  printFormat,
}: PrintOrderParams): Promise<'qz' | 'network' | 'socket'> {
  const { print_type, qz_host, print_format, printer, name, cashier, multiple_cashier, letter_head } =
    posProfile
  const format = resolvePrintFormat({ custom_merged_pos_invoice: null }, printFormat || print_format)

  if (print_type === 'qz') {
    await dispatchReceipt(call.post, { invoice: orderId, print_format: format, letterhead: letter_head },
      html => printWithQz(qz_host || 'localhost', html, printer));
    return 'qz'
  }

  if (print_type === 'network') {
    if (!format) {
      throw new Error('Print format is not set')
    }
    const result =
      cashier && !multiple_cashier
        ? await networkPrint(orderId, printer as string, format as string)
        : await selectNetworkPrinter(orderId, name, format)
    assertNetworkPrintSuccess(result)
    // network_printing / select_network_printer already set invoice_printed on Success
    return 'network'
  }

  // Socket / desk print station — realtime channel print_{branch}
  if (!format) {
    throw new Error('Print format is not set')
  }
  await printPosPage(orderId, format as string)
  return 'socket'
}
