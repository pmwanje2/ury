import { prepareReceipt, completeReceipt, confirmBrowserDelivery } from '@ury/core/receipt';
import { assertOfflineActionAllowed } from '../offline/cashier';
import { call, isLinkDown, printWithQz } from '@ury/core';
import { buildLetterheadParams } from '@ury/core/print-format';
import {
  getInvoicePrintHtml,
  networkPrint,
  resolvePrintFormat,
  selectNetworkPrinter,
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
  dispatch_token?: string;
  recovery_token?: string;
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
  if (!response?.message) throw new Error('Copy reservation was unavailable.');
  const result = await deliverReservedCopy(orderId, idempotencyKey, posProfile, response.message);
  if (result.status !== 'COMPLETED' && result.status !== 'SUBMITTED') {
    throw new Error(result.failure_reason || 'Copy delivery was not acknowledged.');
  }
  return result;
}

export async function deliverReservedCopy(
  invoice: string, key: string,
  profile: { qz_host?: string | null; printer?: string | null; letter_head?: string | null },
  result: PrintBillCopyResult,
): Promise<PrintBillCopyResult> {
  const rendered = result.status === 'BROWSER_DISPATCHING';
  const recovering = (rendered || result.status === 'QZ_DISPATCHING') && Boolean(result.recovery_token);
  if (!recovering && result.status !== 'RESERVED') return result;
  const token = recovering ? result.recovery_token : result.dispatch_token;
  if (!token) throw new Error('Client copy dispatch lease is unavailable.');
  const attempt = { invoice, idempotency_key: key, dispatch_token: token };
  const confirmPrinted = () => window.confirm(`Did COPY #${result.copy_number} print? Check the printer. OK records delivery. Cancel shows recovery options.`);
  let dispatchStarted = recovering;
  let claimError = false;
  const begin = async (transport: 'qz' | 'browser') => {
    try {
      const response = await call.post('ury.ury_pos.api.begin_bill_copy_dispatch', { ...attempt, transport });
      if (response?.message?.status !== 'DISPATCHING') throw new Error('Copy dispatch was not authorized.');
      dispatchStarted = true;
      return response.message;
    } catch (error) {
      claimError = true;
      throw error;
    }
  };
  try {
    if (!recovering && result.delivery !== 'qz' && result.delivery !== 'browser') {
      throw new Error('Client copy reservation is unavailable.');
    }
    let delivered = false;
    if (recovering && rendered) {
      if (!window.confirm(`Did the browser print COPY #${result.copy_number}? Check the receipt before confirming.`)) {
        throw new Error('Copy delivery is still pending.');
      }
      delivered = true;
    } else if (recovering) {
      delivered = confirmPrinted();
      if (!delivered && !window.confirm('Print this copy in the browser? Cancel retains it for later confirmation.')) {
        throw new Error('Copy delivery is still pending.');
      }
    } else if (result.delivery === 'qz') {
      if (!result.html) throw new Error('Authorized QZ copy is unavailable.');
      try {
        await printWithQz(profile.qz_host || 'localhost', result.html, profile.printer, async () => { await begin('qz'); });
        delivered = true;
      } catch (error) {
        if (error instanceof Error && error.name === 'QzSigningRefusedError') {
          throw error;
        }
        if (claimError) throw error;
        if (dispatchStarted) {
          delivered = confirmPrinted();
        }
        if (!delivered && !window.confirm(dispatchStarted
          ? 'Print the same copy in the browser? Cancel retains the unconfirmed copy without printing again.'
          : 'QZ Tray could not start printing. Print the copy in the browser?')) throw error;
      }
    }
    if (!delivered) {
      if (!result.print_url && !result.copy_number) throw new Error('Authorized browser copy is unavailable.');
      const { queryString } = buildLetterheadParams(profile.letter_head);
      const url = result.print_url ||
        `/printview?doctype=POS%20Invoice&name=${encodeURIComponent(invoice)}&format=URY%20Bill%20Copy${queryString}&trigger_print=1&copy_number=${result.copy_number}`;
      await confirmBrowserDelivery(url, async () => {
        const { render_grant } = await begin('browser');
        if (!render_grant) throw new Error('Browser copy authorization is unavailable.');
        return url + '&render_grant=' + encodeURIComponent(render_grant);
      });
    }
  } catch (error) {
    if (!dispatchStarted && !claimError) {
      await call.post('ury.ury_pos.api.release_bill_copy', attempt).catch(() => undefined);
    }
    throw error;
  }
  // A lost confirmation response must never release a copy already dispatched.
  const response = await receiptRequest('ury.ury_pos.api.confirm_bill_copy', attempt);
  const confirmed = response.message as Partial<PrintBillCopyResult> | undefined;
  if (confirmed?.status !== 'COMPLETED') throw new Error('Copy delivery confirmation was unavailable.');
  return { ...result, ...confirmed };
}

async function receiptRequest(method: string, args: Record<string, unknown>) {
  const post = () => call.post<{ message: unknown }>(method, args);
  try {
    return await post();
  } catch (error) {
    // The server keys preparation and confirmation independently of the HTTP response.
    if (!isLinkDown(error)) throw error;
    return post();
  }
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

  if (print_type === 'network') {
    if (cashier && !multiple_cashier) {
      await networkPrint(orderId, printer as string, format as string, auth);
    } else {
      await selectNetworkPrinter(orderId, name, format, auth);
    }
    return 'network';
  }
  if (!skipStatusUpdate) {
    const receipt = await prepareReceipt(receiptRequest, {
      invoice: orderId, print_format: format, letterhead: letter_head,
      approval_token: approvalToken, reason, reason_note: reasonNote,
    });
    if (receipt.delivered) return print_type === 'qz' ? 'qz' : 'socket';
    const qzDelivered = print_type === 'qz' && await deliverQzReceipt({ qz_host, printer }, receipt.html);
    if (!qzDelivered) {
      if (!receipt.print_url) throw new Error('Authorized browser receipt is unavailable.');
      await confirmBrowserDelivery(receipt.print_url);
    }
    await completeReceipt(receiptRequest, orderId, receipt);
    return qzDelivered ? 'qz' : 'socket';
  }
  // Already-fiscalized receipts do not update draft check or table state.
  if (print_type === 'qz') {
    const html = await getInvoicePrintHtml(orderId, format as string, letter_head);
    if (await deliverQzReceipt({ qz_host, printer }, html)) return 'qz';
  }
  const formatParam = format ? `&format=${encodeURIComponent(format)}` : '';
  const { queryString: letterheadParams } = buildLetterheadParams(letter_head);
  const url = `/printview?doctype=POS%20Invoice&name=${encodeURIComponent(orderId)}${formatParam}${letterheadParams}&settings=%7B%7D&trigger_print=1&_lang=en`;
  await confirmBrowserDelivery(url);
  return 'socket';
}
