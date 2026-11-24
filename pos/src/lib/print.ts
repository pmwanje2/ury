import { assertOfflineActionAllowed } from '../offline/cashier';
import { call, printWithQz } from '@ury/core';
import { buildLetterheadParams } from '@ury/core/print-format';
import {
  getInvoicePrintHtml,
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
  approvalToken?: string;
  reason?: string;
  reasonNote?: string;
  /** Skip calling qz_print_update (already-submitted fiscal receipts must not write invoice_printed). */
  skipStatusUpdate?: boolean;
}

export interface PrintBillCopyResult {
  status: string;
  failure_reason?: string | null;
  print_job_id?: string;
  copy_number?: number;
  copy_timestamp?: string;
  delivery?: 'network' | 'qz' | 'browser';
  html?: string;
  print_url?: string;
}

export interface PrintBillCopyParams {
  orderId: string;
  posProfile: Pick<PosProfileCombined, 'print_type' | 'qz_host' | 'letter_head'>;
  idempotencyKey: string;
  approvalToken?: string;
  reason?: string;
  reasonNote?: string;
}

export async function printBillCopy({
  orderId,
  posProfile,
  idempotencyKey,
  approvalToken,
  reason,
  reasonNote,
}: PrintBillCopyParams): Promise<PrintBillCopyResult> {
  await assertOfflineActionAllowed('fiscal print');
  const delivery: 'network' | 'qz' | 'browser' =
    posProfile.print_type === 'network'
      ? 'network'
      : posProfile.print_type === 'qz'
        ? 'qz'
        : 'browser';
  const response = await call.post<{ message: PrintBillCopyResult }>('ury.ury_pos.api.print_bill_copy', {
    invoice: orderId,
    idempotency_key: idempotencyKey,
    delivery,
    ...(approvalToken ? { approval_token: approvalToken } : {}),
    ...(reason ? { reason } : {}),
    ...(reasonNote ? { reason_note: reasonNote } : {}),
  });
  const result: PrintBillCopyResult = response?.message ?? { status: 'COMPLETED', delivery };
  if (result.status === 'FAILED') {
    throw new Error(result.failure_reason || 'Copy print failed');
  }
  if (delivery === 'qz') {
    if (!posProfile.qz_host) {
      throw new Error('QZ host is not set');
    }
    if (result.html) {
      await printWithQz(posProfile.qz_host, result.html);
    }
  } else if (delivery === 'browser') {
    const copyParam = result.copy_number ? `&copy_number=${result.copy_number}` : '';
    const { queryString: letterheadParams } = buildLetterheadParams(posProfile.letter_head);
    const url =
      result.print_url ||
      `/printview?doctype=POS%20Invoice&name=${encodeURIComponent(orderId)}&format=URY%20Bill%20Copy${letterheadParams}&trigger_print=1${copyParam}`;
    window.open(url, '_blank', 'noopener,noreferrer');
  }
  return result;
}

export async function printOrder({
  orderId,
  posProfile,
  printFormat,
  approvalToken,
  reason,
  reasonNote,
  skipStatusUpdate,
}: PrintOrderParams): Promise<'qz' | 'network' | 'socket'> {
  await assertOfflineActionAllowed('fiscal print');
  const { print_type, qz_host, print_format, printer, name, cashier, multiple_cashier, letter_head } = posProfile;
  const format = resolvePrintFormat({ custom_merged_pos_invoice: null }, printFormat || print_format);
  const auth = approvalToken || reason || reasonNote ? { approvalToken, reason, reasonNote } : undefined;

  if (print_type === 'qz') {
    if (!qz_host) {
      throw new Error('QZ host is not set');
    }
    const html = await getInvoicePrintHtml(orderId, format as string, letter_head);
    await printWithQz(qz_host, html);
    if (!skipStatusUpdate) {
      await updatePrintStatus(orderId, auth);
    }
    return 'qz';
  } else if (print_type === 'network') {
    if (cashier && !multiple_cashier) {
      await networkPrint(orderId, printer as string, format as string, auth);
    } else {
      await selectNetworkPrinter(orderId, name, format, auth);
    }
    if (!skipStatusUpdate) {
      await updatePrintStatus(orderId);
    }
    return 'network';
  } else {
    // Authorize and record bill print before opening the browser printview tab.
    if (!skipStatusUpdate) {
      await updatePrintStatus(orderId, auth);
    }
    // Redirect to printview page, respecting configured POS Profile letter_head
    const formatParam = format ? `&format=${encodeURIComponent(format)}` : '';
    const { queryString: letterheadParams } = buildLetterheadParams(letter_head);
    const url = `/printview?doctype=POS%20Invoice&name=${encodeURIComponent(orderId)}${formatParam}${letterheadParams}&settings=%7B%7D&trigger_print=1&_lang=en`;
    window.open(url, '_blank', 'noopener,noreferrer');
    return 'socket';
  }
}