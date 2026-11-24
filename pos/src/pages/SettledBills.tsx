import { useState } from 'react';
import { Link } from 'react-router-dom';
import { call, parseFrappeError, printWithQz } from '@ury/core';
import { Button } from '@ury/ui';
import { t, tData } from '../i18n';
import { isAuthRequiredError, overrideErrorMessage } from '../lib/override-api';
import { extractAuthChallenge, isReasonRequiredError } from '../lib/privileged-actions-api';
import ManagerApproval, { type ManagerApprovalResult } from '../components/ManagerApproval';
import TipAdjustDialog from '../components/TipAdjustDialog';
import { usePOSStore } from '../store/pos-store';
import { printOrder } from '../lib/print';
import { resolvePrintFormat } from '../lib/invoice-api';
import { formatMoneyAmount } from '../lib/void-return-api';
import ReturnCheckDialog from '../components/ReturnCheckDialog';
import ReopenAdjustCheckDialog from '../components/ReopenAdjustCheckDialog';

type Bill = {
  name: string;
  customer: string;
  status: string;
  posting_date: string;
  posting_time: string;
  currency: string;
  restaurant_table: string;
  pos_profile: string;
  consolidated_invoice: string | null;
  is_return?: number;
  return_against?: string | null;
  custom_reopened_from?: string | null;
  room_number?: string | null;
  efris_irn?: string | null;
  efris_status?: string | null;
  grand_total: number;
  rounded_total: number;
  outstanding_amount: number;
  total_taxes_and_charges: number;
  custom_charged_tip?: number;
  waiter?: string;
  items: { name?: string; item_code?: string; item_name: string; qty: number; rate?: number; amount: number }[];
  taxes: { description: string; tax_amount: number }[];
  payments: { mode_of_payment: string; amount: number }[];
};

const filters = {
  bill_no: 'Bill number',
  date_from: 'From date',
  date_to: 'To date',
  table: 'Table',
  pos_profile: 'Outlet',
  branch: 'Branch',
};
const PAGE = 20;
const method = 'ury.ury_pos.api.';

export default function SettledBills() {
  const [rows, setRows] = useState<Bill[] | null>(null);
  const [bill, select] = useState<Bill | null>(null);
  const [filtersUsed, setFilters] = useState<Record<string, string>>({});
  const [start, setStart] = useState(0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [keys] = useState(() => new Map<string, string>());
  const [outcome, setOutcome] = useState('');
  const [showReturnDialog, setShowReturnDialog] = useState(false);
  const [showReopenDialog, setShowReopenDialog] = useState(false);
  const [pendingCopyApproval, setPendingCopyApproval] = useState<{
    bill: Bill;
    idempotencyKey: string;
    claim: Record<string, unknown>;
  } | null>(null);
  const [showTipDialog, setShowTipDialog] = useState(false);

  async function search(params: Record<string, string>, offset: number) {
    setFilters(params);
    setStart(offset);
    setBusy(true);
    select(null);
    setRows(null);
    setMessage('');
    try {
      const response = await call.get<{ message: Bill[] }>(method + 'search_settled_bills', {
        ...params,
        start: offset,
        page_length: PAGE,
      });
      setRows(response.message);
    } catch (error) {
      setMessage(parseFrappeError(error));
    } finally {
      setBusy(false);
    }
  }

  async function print(approval?: ManagerApprovalResult, overrideKey?: string) {
    const targetBill = pendingCopyApproval?.bill ?? bill;
    if (!targetBill || busy) return;
    const key = overrideKey || keys.get(targetBill.name) || crypto.randomUUID();
    keys.set(targetBill.name, key);
    setBusy(true);
    setMessage('');
    try {
      if (typeof navigator !== 'undefined' && navigator.onLine === false) {
        throw new Error(t('offline.refused', { action: 'closed receipt copy' }));
      }
      const { message: result } = await call.post<{
        message: {
          status: string;
          failure_reason?: string;
          delivery?: string;
          html?: string;
          print_url?: string;
          copy_number?: number;
        };
      }>(method + 'print_bill_copy', {
        invoice: targetBill.name,
        idempotency_key: key,
        ...(approval?.token ? { approval_token: approval.token } : {}),
        ...(approval?.reason ? { reason: approval.reason } : {}),
        ...(approval?.note ? { reason_note: approval.note } : {}),
      });
      if (result.delivery === 'qz' && result.html) {
        const qzHost = usePOSStore.getState().posProfile?.qz_host;
        if (qzHost) await printWithQz(qzHost, result.html);
      } else if (result.delivery === 'browser' && result.print_url) {
        window.open(result.print_url, '_blank', 'noopener,noreferrer');
      }
      setOutcome(result.status);
      setMessage(
        [tData('Copy'), result.copy_number ? `#${result.copy_number}` : '', result.status, result.failure_reason]
          .filter(Boolean)
          .join(' · '),
      );
      if (result.status === 'COMPLETED') keys.delete(targetBill.name);
    } catch (error) {
      if (!approval?.token && (isAuthRequiredError(error) || isReasonRequiredError(error))) {
        const challenge = extractAuthChallenge(error);
        setPendingCopyApproval({
          bill: targetBill,
          idempotencyKey: key,
          claim: challenge?.claim ?? {
            action: 'print_bill_copy',
            invoice: targetBill.name,
            copy_number: 1,
          },
        });
        return;
      }
      setOutcome('ERROR');
      setMessage(overrideErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  const canModifyClosed = Boolean(
    bill &&
      !bill.consolidated_invoice &&
      bill.status !== 'Consolidated' &&
      Number(bill.is_return || 0) !== 1,
  );

  return (
    <main className="flex-1 overflow-auto p-4 space-y-4">
      <Link className="text-blue-700 underline" to="/orders">
        {tData('Orders')}
      </Link>
      <h1 className="text-xl font-semibold">{tData('Settled / Closed')}</h1>
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          const params: Record<string, string> = {};
          new FormData(e.currentTarget).forEach((value, k) => {
            if (String(value).trim()) params[k] = String(value).trim();
          });
          void search(params, 0);
        }}
      >
        {Object.entries(filters).map(([name, label]) => (
          <label className="grid gap-1 text-sm" key={name}>
            {tData(label)}
            <input
              className="rounded border p-2"
              name={name}
              type={name.startsWith('date_') ? 'date' : 'text'}
            />
          </label>
        ))}
        <Button type="submit" disabled={busy}>
          {tData('Search')}
        </Button>
      </form>
      {busy && <p role="status">{tData('Working…')}</p>}
      {message && <p role="status">{message}</p>}
      {rows && (
        <section className="space-y-2" aria-label={tData('Search results')}>
          {!rows.length && <p>{tData('No closed checks found.')}</p>}
          {rows.map((row) => (
            <button
              type="button"
              disabled={busy}
              className="block w-full border rounded p-3 text-start"
              key={row.name}
              onClick={() => {
                select(row);
                setMessage('');
                setOutcome(keys.has(row.name) ? 'SUBMITTED' : '');
              }}
            >
              <strong>{row.name}</strong> · {row.posting_date} · {row.pos_profile} · {row.status} ·{' '}
              {row.currency} {row.grand_total}
            </button>
          ))}
          {start > 0 && (
            <Button disabled={busy} onClick={() => search(filtersUsed, start - PAGE)}>
              {tData('Previous')}
            </Button>
          )}
          {rows.length === PAGE && (
            <Button disabled={busy} onClick={() => search(filtersUsed, start + PAGE)}>
              {tData('Next')}
            </Button>
          )}
        </section>
      )}
      {bill && (
        <article className="space-y-3 rounded border bg-white p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="font-semibold">
              {tData('Original invoice')}: {bill.name}
            </h2>
            <div className="flex flex-wrap gap-1.5 text-xs">
              {Number(bill.is_return || 0) === 1 && (
                <span className="rounded bg-purple-100 px-2 py-0.5 font-medium text-purple-900">
                  {t('void_return.badge_return')}
                </span>
              )}
              {bill.consolidated_invoice && (
                <span className="rounded bg-gray-100 px-2 py-0.5 font-medium text-gray-800">
                  {t('void_return.badge_consolidated')}
                </span>
              )}
              {bill.room_number && (
                <span className="rounded bg-blue-100 px-2 py-0.5 font-medium text-blue-900">
                  {t('void_return.badge_room', { room: bill.room_number })}
                </span>
              )}
              {bill.efris_irn && (
                <span className="rounded bg-amber-100 px-2 py-0.5 font-medium text-amber-900">
                  {t('void_return.badge_fiscalised')}
                </span>
              )}
            </div>
          </div>
          <p>
            {[
              bill.posting_date,
              bill.posting_time,
              bill.restaurant_table,
              bill.pos_profile,
              bill.status,
              bill.consolidated_invoice,
            ]
              .filter(Boolean)
              .join(' · ')}
          </p>
          <table className="w-full text-start">
            <thead>
              <tr>
                {['Item', 'Qty', 'Amount'].map((x) => (
                  <th className="text-start" key={x}>
                    {tData(x)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {bill.items.map((r, i) => (
                <tr key={i}>
                  <td>{r.item_name}</td>
                  <td>{r.qty}</td>
                  <td>
                    {bill.currency} {r.amount}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>{tData('Taxes')}</h3>
          {bill.taxes.map((r, i) => (
            <p key={i}>
              <span>{r.description}</span>: {bill.currency} {r.tax_amount}
            </p>
          ))}
          <p>
            {tData('Grand total')}: {bill.currency} {bill.grand_total}
          </p>
          <p>
            {tData('Rounded total')}: {bill.currency} {bill.rounded_total}
          </p>
          <h3>{tData('Original tenders')}</h3>
          {bill.payments
            .filter((p) => p.amount !== 0)
            .map((r, i) => (
              <p key={i}>
                <span>{r.mode_of_payment}</span>: {bill.currency} {r.amount}
              </p>
            ))}
          <p>
            {tData('Amount on account / Balance due')}: {bill.currency} {bill.outstanding_amount}
            {bill.outstanding_amount > 0 && ` · ${bill.customer}`}
          </p>
          <div className="flex flex-wrap items-center gap-2 pt-2">
            <Button disabled={busy} className="min-h-11" onClick={() => void print()}>
              {tData(
                ['FAILED', 'CANCELED', 'ERROR'].includes(outcome)
                  ? 'Retry copy'
                  : keys.has(bill.name)
                    ? 'Check / retry copy'
                    : 'Print copy',
              )}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={busy || !canModifyClosed}
              className="min-h-11"
              onClick={() => setShowTipDialog(true)}
            >
              {t('privileged_actions.tip_adjust')}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={busy || !canModifyClosed}
              className="min-h-11"
              onClick={() => setShowReturnDialog(true)}
            >
              {t('void_return.return_refund_action')}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={busy || !canModifyClosed}
              className="min-h-11"
              onClick={() => setShowReopenDialog(true)}
            >
              {t('void_return.reopen_adjust_action')}
            </Button>
          </div>
          {!canModifyClosed && bill.consolidated_invoice && (
            <p className="text-xs text-gray-600">{t('void_return.consolidated_locked_hint')}</p>
          )}
        </article>
      )}

      {bill && showReturnDialog && (
        <ReturnCheckDialog
          open={showReturnDialog}
          invoiceId={bill.name}
          onCompleted={async (res) => {
            setShowReturnDialog(false);
            setMessage(
              t('void_return.return_success', {
                return_invoice: res.return_invoice,
                amount: formatMoneyAmount(res.refund_total, res.currency),
              }),
            );
            const profile = usePOSStore.getState().posProfile;
            if (profile && res.return_invoice) {
              try {
                await printOrder({
                  orderId: res.return_invoice,
                  posProfile: profile,
                  printFormat: resolvePrintFormat({}, profile.print_format),
                  skipStatusUpdate: true,
                });
              } catch {
                // Best-effort print; return is already posted and displayed above.
              }
            }
            void search(filtersUsed, start);
          }}
          onClose={() => setShowReturnDialog(false)}
        />
      )}

      {bill && showReopenDialog && (
        <ReopenAdjustCheckDialog
          open={showReopenDialog}
          bill={bill}
          onCompleted={(res) => {
            setShowReopenDialog(false);
            const target =
              res.action === 'adjust_closed_check'
                ? res.replacement_invoice
                : res.reopened_invoice;
            setMessage(
              t('void_return.reopen_success', {
                replacement: target,
                reversal: res.reversal_return,
              }),
            );
            void search(filtersUsed, start);
          }}
          onClose={() => setShowReopenDialog(false)}
        />
      )}

      {pendingCopyApproval && (
        <ManagerApproval
          open
          posProfile={pendingCopyApproval.bill.pos_profile}
          privilege="reprint_bill"
          title={t('privileged_actions.reprint_bill')}
          summaryLines={[
            `${t('privileged_actions.check_ref')}: ${pendingCopyApproval.bill.name}`,
            `${pendingCopyApproval.bill.currency} ${pendingCopyApproval.bill.grand_total}`,
          ]}
          reference={{ doctype: 'POS Invoice', name: pendingCopyApproval.bill.name }}
          claim={pendingCopyApproval.claim}
          requireReason
          onApproved={(res) => {
            const pending = pendingCopyApproval;
            setPendingCopyApproval(null);
            void print(res, pending.idempotencyKey);
          }}
          onCancel={() => setPendingCopyApproval(null)}
        />
      )}

      {bill && (
        <TipAdjustDialog
          open={showTipDialog}
          onOpenChange={setShowTipDialog}
          posProfile={bill.pos_profile}
          invoiceName={bill.name}
          currentTip={bill.custom_charged_tip ?? 0}
          tipServer={bill.waiter}
          modeOfPayment={bill.payments.find((p) => p.amount > 0)?.mode_of_payment}
          currency={bill.currency}
          onSuccess={() => search(filtersUsed, start)}
        />
      )}
    </main>
  );
}
