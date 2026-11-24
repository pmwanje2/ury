import { assertOfflineActionAllowed } from '../offline/cashier';
import { printWithQz } from '@ury/core';
import {
  getInvoicePrintHtml,
  networkPrint,
  selectNetworkPrinter,
  updatePrintStatus
} from './invoice-api';
import { PosProfileCombined } from './pos-profile-api';

interface PrintOrderParams {
  orderId: string;
  posProfile: PosProfileCombined;
  printFormat?: string | null;
  approvalToken?: string;
  reason?: string;
  reasonNote?: string;
}

export async function printOrder({
  orderId,
  posProfile,
  printFormat,
  approvalToken,
  reason,
  reasonNote,
}: PrintOrderParams): Promise<'qz' | 'network' | 'socket'> {
  await assertOfflineActionAllowed('fiscal print');
  const { print_type, qz_host, print_format, printer, name, cashier, multiple_cashier } = posProfile;
  const format = printFormat || print_format || '';
  const auth = approvalToken || reason || reasonNote ? { approvalToken, reason, reasonNote } : undefined;

  if (print_type === 'qz') {
    if (!qz_host) {
      throw new Error('QZ host is not set');
    }
    const html = await getInvoicePrintHtml(orderId, format as string);
    await printWithQz(qz_host, html);
    await updatePrintStatus(orderId, auth);
    return 'qz';
  } else if (print_type === 'network') {
    if (cashier && !multiple_cashier) {
      await networkPrint(orderId, printer as string, format as string, auth);
    } else {
      await selectNetworkPrinter(orderId, name, format, auth);
    }
    await updatePrintStatus(orderId);
    return 'network';
  } else {
    // Redirect to printview page
    await updatePrintStatus(orderId, auth);
    const formatParam = format ? `&format=${encodeURIComponent(format)}` : '';
    const url = `/printview?doctype=POS Invoice&name=${orderId}${formatParam}&no_letterhead=1&settings={}&letterhead=No Letterhead&trigger_print=1&_lang=en`;
    window.open(url, '_blank', 'noopener,noreferrer');
    return 'socket';
  }
}