import { dispatchReceipt } from "@ury/core/receipt";
import { call, printWithQz } from '@ury/core';
import { buildLetterheadParams } from '@ury/core/print-format';
import {
  networkPrint,
  resolvePrintFormat,
  selectNetworkPrinter,
  updatePrintStatus
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

  if (print_type === 'qz') {
    await dispatchReceipt(call.post, { invoice: orderId, print_format: format, letterhead: letter_head },
      html => printWithQz(qz_host || 'localhost', html, printer));
    return 'qz';
  } else if (print_type === 'network') {
    if (cashier && !multiple_cashier) {
      await networkPrint(orderId, printer as string, format as string);
    } else {
      await selectNetworkPrinter(orderId, name, format);
    }
    await updatePrintStatus(orderId);
    return 'network';
  } else {
    // Redirect to printview page, respecting configured POS Profile letter_head
    const formatParam = format ? `&format=${encodeURIComponent(format)}` : '';
    const { queryString: letterheadParams } = buildLetterheadParams(letter_head);
    const url = `/printview?doctype=POS%20Invoice&name=${encodeURIComponent(orderId)}${formatParam}${letterheadParams}&settings=%7B%7D&trigger_print=1&_lang=en`;
    window.open(url, '_blank', 'noopener,noreferrer');
    await updatePrintStatus(orderId);
    return 'socket';
  }
}