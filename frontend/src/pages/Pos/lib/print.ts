import { prepareReceipt, completeReceipt, confirmBrowserDelivery } from "@ury/core/receipt";
import { call, printWithQz } from '@ury/core';
import {
  networkPrint,
  resolvePrintFormat,
  selectNetworkPrinter,
} from './invoice-api';
import { PosProfileCombined } from './pos-profile-api';

interface PrintOrderParams {
  orderId: string;
  posProfile: PosProfileCombined;
  printFormat?: string | null;
}

export async function printOrder({ orderId, posProfile, printFormat }: PrintOrderParams): Promise<'qz' | 'network' | 'socket'> {
  const { print_type, qz_host, print_format, printer, name, cashier, multiple_cashier, letter_head } = posProfile;
  const format = resolvePrintFormat({ custom_merged_pos_invoice: null }, printFormat || print_format);

  if (print_type === 'network') {
    if (cashier && !multiple_cashier) {
      await networkPrint(orderId, printer as string, format as string);
    } else {
      await selectNetworkPrinter(orderId, name, format);
    }
    return 'network';
  }

  const receipt = await prepareReceipt(call.post, { invoice: orderId, print_format: format, letterhead: letter_head });
  if (!receipt.delivered) {
    if (print_type === 'qz') {
      await printWithQz(qz_host || 'localhost', receipt.html, printer);
    } else {
      if (!receipt.print_url) throw new Error('Authorized browser receipt is unavailable.');
      await confirmBrowserDelivery(receipt.print_url);
    }
    await completeReceipt(call.post, orderId, receipt);
  }
  return print_type === 'qz' ? 'qz' : 'socket';
}
