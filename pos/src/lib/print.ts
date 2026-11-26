import { v4 as uuid } from 'uuid';
import { assertOfflineActionAllowed } from '../offline/cashier';
import { call, isLinkDown, printWithQz } from '@ury/core';
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
  posProfile: Pick<PosProfileCombined, 'print_type' | 'qz_host' | 'letter_head'> & Partial<Pick<PosProfileCombined, 'printer'>>;
  idempotencyKey: string;
  approvalToken?: string;
  reason?: string;
  reasonNote?: string;
}

export async function deliverQzReceipt(
  profile: { qz_host?: string | null; printer?: string | null },
  html: string,
): Promise<boolean> {
  try {
    await printWithQz(profile.qz_host || 'localhost', html, profile.printer);
    return true;
  } catch (error) {
    if (error instanceof Error && error.name === 'QzSigningRefusedError') throw error;
    const detail = error instanceof Error ? error.message : String(error);
    if (!window.confirm(`QZ Tray could not confirm printing (${detail}). Check the printer for a receipt. Print in the browser?`)) {
      throw error;
    }
    return false;
  }
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
  let fallback = false;
  if (delivery === 'qz' && result.html) {
    fallback = !await deliverQzReceipt(posProfile, result.html);
  }
  if (delivery === 'browser' || fallback) {
    const copyParam = result.copy_number ? `&copy_number=${result.copy_number}` : '';
    const { queryString: letterheadParams } = buildLetterheadParams(posProfile.letter_head);
    const url =
      result.print_url ||
      `/printview?doctype=POS%20Invoice&name=${encodeURIComponent(orderId)}&format=URY%20Bill%20Copy${letterheadParams}&trigger_print=1${copyParam}`;
    window.open(url, fallback ? '_self' : '_blank', fallback ? undefined : 'noopener,noreferrer');
  }
  return result;
}

async function receiptRequest<T>(method: string, args: Record<string, unknown>): Promise<T> {
  const post = () => call.post<T>(`ury.ury.api.ury_print.${method}`, args);
  try {
    return await post();
  } catch (error) {
    // The server keys preparation and confirmation independently of the HTTP response.
    if (!isLinkDown(error)) throw error;
    return post();
  }
}

async function confirmBrowserDelivery(url: string): Promise<void> {
  let popup = window.open(url, '_blank');
  if (!popup) {
    // The QZ attempt may have outlived browser user activation. An actual click
    // opens printview without repeating the receipt authorization.
    popup = await new Promise<Window>((resolve, reject) => {
      const dialog = document.createElement('dialog');
      const open = document.createElement('button');
      open.textContent = 'Open receipt for browser printing';
      const cancel = document.createElement('button');
      cancel.textContent = 'Cancel';
      dialog.append(open, cancel);
      document.body.append(dialog);
      const dismiss = () => {
        dialog.remove();
        reject(new Error('Browser printing was cancelled.'));
      };
      cancel.onclick = dismiss;
      dialog.oncancel = dismiss;
      open.onclick = () => {
        const receipt = window.open(url, '_blank');
        if (!receipt) {
          open.textContent = 'Allow pop-ups, then open receipt';
          return;
        }
        dialog.remove();
        resolve(receipt);
      };
      dialog.showModal();
    });
  }
  popup.opener = null;
  // Frappe closes printview after the native dialog. Browsers cannot report
  // whether the operator printed or cancelled, so confirm the physical receipt.
  while (!popup.closed) await new Promise(resolve => setTimeout(resolve, 300));
  if (!window.confirm('Did the browser print the receipt?')) throw new Error('Receipt delivery was not confirmed.');
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

  const fallback = print_type === 'qz';
  let deliveryId: string | undefined;
  const completeDelivery = async () => {
    if (!deliveryId) return;
    try {
      await receiptRequest('complete_receipt_delivery', { invoice: orderId, delivery_id: deliveryId });
    } catch {
      throw new Error('Receipt delivered, but its status could not be confirmed. Check the invoice before reprinting.');
    }
  };
  if (fallback) {
    const html = await getInvoicePrintHtml(orderId, format as string, letter_head);
    if (!skipStatusUpdate) {
      const { message } = await receiptRequest<{ message: { delivery_id: string } }>(
        'prepare_receipt_delivery', {
          invoice: orderId, request_id: uuid(), approval_token: approvalToken,
          reason, reason_note: reasonNote,
        },
      );
      if (!message?.delivery_id) throw new Error('Receipt authorization was unavailable.');
      deliveryId = message.delivery_id;
    }
    if (await deliverQzReceipt({ qz_host, printer }, html)) {
      await completeDelivery();
      return 'qz';
    }
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
  }
  if (!fallback && !skipStatusUpdate) await updatePrintStatus(orderId, auth);
  const formatParam = format ? `&format=${encodeURIComponent(format)}` : '';
  const { queryString: letterheadParams } = buildLetterheadParams(letter_head);
  const url = `/printview?doctype=POS%20Invoice&name=${encodeURIComponent(orderId)}${formatParam}${letterheadParams}&settings=%7B%7D&trigger_print=1&_lang=en`;
  if (fallback) {
    await confirmBrowserDelivery(url);
    await completeDelivery();
  } else {
    window.open(url, '_blank', 'noopener,noreferrer');
  }
  return 'socket';
}
