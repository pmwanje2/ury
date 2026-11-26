import { isFloorSessionConfirmedForTab } from '../lib/floor-tab';
import { useOutbox } from '../offline/use-outbox';
import { t } from '../i18n';
import { useState } from 'react';
import { Button, showToast } from '@ury/ui';
import { currencyFractionDigits, formatCurrency, parseFrappeError } from '@ury/core';
import {
  canCaptureOfflineOrder,
  canSettleOffline,
  canRecoverOfflineOrder,
  canRecoverOfflineTender,
  offlineCaptureEnabled,
  exceedsOfflineTenderLimit,
  offlineCompanyAmount,
  type OfflineProfile,
  type OfflineUser,
  type OfflineEntry,
  type OfflineOutbox,
  type OfflinePaymentModeConfig,
} from '../offline/outbox';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';

function roundForCurrency(value: number, currencyCode?: string): number {
  const digits = currencyCode ? currencyFractionDigits(currencyCode) : 2;
  const factor = Math.pow(10, digits);
  return Math.round(value * factor) / factor;
}

function PendingOrder({
  row,
  queue,
  eligibleModes,
  profile,
  currencyCode,
  canSettle,
  canReprintBill,
  confirmed,
  disabled,
}: {
  row: OfflineEntry;
  queue: OfflineOutbox;
  eligibleModes: OfflinePaymentModeConfig[];
  profile: OfflineProfile | null;
  currencyCode?: string;
  canSettle: boolean;
  canReprintBill: boolean;
  confirmed: () => boolean;
  disabled: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [reference, setReference] = useState('');
  const directModes = eligibleModes.filter((m) => !m.requires_reference);
  const refModes = eligibleModes.filter((m) => m.requires_reference);
  const [selectedRefMode, setSelectedRefMode] = useState(refModes[0]?.mode_of_payment || '');
  const activeRefMode =
    refModes.find((m) => m.mode_of_payment === selectedRefMode)?.mode_of_payment ||
    refModes[0]?.mode_of_payment ||
    '';
  const total = row.rounds[row.rounds.length - 1].expected_total;
  const offlineLimit = Number(profile?.offline_tender_limit || 0);
  const overLimit = profile ? exceedsOfflineTenderLimit(profile, total) : false;
  const formatAmount = (amount: number) =>
    currencyCode ? formatCurrency(amount, currencyCode) : String(amount);

  const action = async (run: () => Promise<unknown>) => {
    if (!confirmed()) return;
    setBusy(true);
    try {
      await run();
    } catch (error) {
      showToast.error(error instanceof Error ? error.message : t('errors.payment_failed'));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="border-t border-amber-300 py-2" data-client-order={row.client_order_id}>
      <span>
        {row.order.customer || row.order.table} · {row.client_order_id.slice(0, 8)} ·{' '}
        {t('payment.total')}: {formatAmount(total)}
      </span>
      {row.rounds.some((round) => !round.offline_printed) && (
        <Button
          size="sm"
          className="ms-2"
          disabled={disabled || busy}
          onClick={() => action(() => queue.reprint(row.client_order_id, 'kot'))}
        >
          {t('offline.reprint_kot')}
        </Button>
      )}
      {row.settlement ? (
        <span className="ms-2">
          {t('offline.settled')} · {row.settlement.mode_of_payment}
          {row.settlement.needs_online_approval && (
            <span className="ms-2 text-xs font-medium text-amber-800">
              ({t('offline.pending_manager_pin')})
            </span>
          )}
          {!row.bill_printed && canReprintBill && (
            <Button
              size="sm"
              className="ms-2"
              disabled={disabled || busy}
              onClick={() => action(() => queue.reprint(row.client_order_id, 'bill'))}
            >
              {t('offline.reprint_bill')}
            </Button>
          )}
        </span>
      ) : canSettle ? (
        <div className="space-y-1.5 mt-1">
          {overLimit && (
            <p className="text-xs font-medium text-amber-900">
              {t('offline.over_limit_notice', { limit: formatCurrency(offlineLimit, profile?.approval_currency || currencyCode) })}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {directModes.map((mode, idx) => {
              const isCash = mode.type === 'Cash' || mode.mode_of_payment === 'Cash';
              return (
                <Button
                  key={mode.mode_of_payment}
                  size="sm"
                  data-testid={isCash || idx === 0 ? 'settle-cash' : `settle-offline-${mode.mode_of_payment}`}
                  disabled={disabled || busy || row.next_round > 0}
                  onClick={() =>
                    action(() =>
                      queue.settle(row.client_order_id, {
                        kind: isCash ? 'cash' : 'offline',
                        mode_of_payment: mode.mode_of_payment,
                        amount: total,
                        ...(overLimit ? { needs_online_approval: true } : {}),
                      }),
                    )
                  }
                >
                  {isCash ? t('offline.cash') : t('offline.settle_mode', { mode: mode.mode_of_payment })}
                </Button>
              );
            })}
            {refModes.length > 0 && (
              <>
                <select
                  aria-label={t('offline.mobile_mode')}
                  value={activeRefMode}
                  disabled={disabled || busy}
                  onChange={(event) => setSelectedRefMode(event.target.value)}
                >
                  {refModes.map((mode) => (
                    <option key={mode.mode_of_payment} value={mode.mode_of_payment}>
                      {mode.mode_of_payment}
                    </option>
                  ))}
                </select>
                <input
                  aria-label={t('offline.reference')}
                  placeholder={t('offline.reference')}
                  value={reference}
                  disabled={disabled || busy}
                  onChange={(event) => setReference(event.target.value)}
                />
                <Button
                  size="sm"
                  disabled={disabled || busy || !reference.trim() || !activeRefMode || row.next_round > 0}
                  onClick={() =>
                    action(() =>
                      queue.settle(row.client_order_id, {
                        kind: 'mobile-money',
                        mode_of_payment: activeRefMode,
                        amount: total,
                        reference,
                        ...(overLimit ? { needs_online_approval: true } : {}),
                      }),
                    )
                  }
                >
                  {t('offline.settle_mode', { mode: activeRefMode })}
                </Button>
              </>
            )}
          </div>
        </div>
      ) : (
        <p className="mt-1 text-xs text-amber-800">{t('offline.waiter_settle_blocked')}</p>
      )}
    </li>
  );
}

export default function OutboxPanel({ posProfile, user, currency }: {
  posProfile: OfflineProfile | null; user: OfflineUser | null; currency?: string | null;
}) {
  const { rows, replaying, offline, queue } = useOutbox(posProfile, user, true);
  const [approvalRow, setApprovalRow] = useState<OfflineEntry | null>(null);
  const currencyCode = posProfile?.currency || currency || undefined;
  const formatAmount = (amount: number) =>
    currencyCode ? formatCurrency(amount, currencyCode) : String(amount);

  const confirmed = () => isFloorSessionConfirmedForTab(user?.name);
  if ((rows.length || approvalRow) && !confirmed()) return null;
  const pending = rows.filter((row) => row.state === 'pending').length;
  const review = rows.filter((row) => row.state === 'review');
  const canCaptureOrder = canCaptureOfflineOrder(posProfile, user);
  const canSettle = canSettleOffline(posProfile, user);
  if (!rows.length && (!offline || !canCaptureOrder)) return null;

  const roomChargeSet = new Set(posProfile?.room_charge_modes || []);
  const eligibleModes = (posProfile?.offline_payment_modes || []).filter(
    (mode) => mode.allow_offline && mode.type !== 'Phone' && !roomChargeSet.has(mode.mode_of_payment),
  );
  const offlineLimit = Number(posProfile?.offline_tender_limit || 0);

  const handleApprovalGranted = async (result: ManagerApprovalResult) => {
    const target = approvalRow;
    setApprovalRow(null);
    if (!target || !queue || !confirmed()) return;
    try {
      await queue.approveAndReplay(target.client_order_id, result.token, result.reason || '');
      showToast.success(t('offline.approved_and_replayed'));
    } catch (error) {
      showToast.error(error instanceof Error ? error.message : t('errors.payment_failed'));
    }
  };

  const approvalAmount = approvalRow
    ? Number(
        approvalRow.settlement?.amount ??
          approvalRow.rounds[approvalRow.rounds.length - 1]?.expected_total ??
          0,
      )
    : 0;

  return (
    <section
      role="status"
      data-testid="offline-banner"
      className="bg-amber-100 border-b border-amber-300 px-4 py-2 text-sm text-amber-950"
    >
      <strong>{t(replaying ? 'offline.replaying' : offline ? 'offline.capture' : 'offline.outbox')}</strong>
      <span className="ms-2">
        {t('offline.pending', { count: pending })}
        {review.length ? ` · ${t('offline.review_count', { count: review.length })}` : ''}
      </span>
      {offline && <p>{t('offline.refusals')}</p>}
      {queue?.persistent === false && <p role="alert">{t('offline.persistence_warning')}</p>}
      {queue && pending > 0 && (
        <details open className="max-h-48 overflow-y-auto">
          <summary>{t('offline.outbox')}</summary>
          <ul>
            {rows
              .filter((row) => row.state === 'pending')
              .map((row) => (
                <PendingOrder
                  key={row.sequence}
                  row={row}
                  queue={queue}
                  eligibleModes={eligibleModes}
                  profile={posProfile}
                  currencyCode={currencyCode}
                  canSettle={canSettle}
                  confirmed={confirmed}
                  canReprintBill={canRecoverOfflineTender(posProfile, user)}
                  disabled={replaying || queue.sessionBlocked || !canRecoverOfflineOrder(posProfile, user, row.order.order_type)}
                />
              ))}
          </ul>
        </details>
      )}
      {review.length > 0 && (
        <ul className="mt-1 list-disc ps-5 space-y-1">
          {review.map((row) => (
            <li key={row.sequence} className="flex flex-wrap items-center gap-2">
              <span>
                {row.client_order_id.slice(0, 8)} ·{' '}
                {parseFrappeError({ _server_messages: row.reason }, row.reason || '')}
              </span>
              {!offlineCaptureEnabled(posProfile) && (
                <details>
                  <summary>{row.order.customer || row.order.table} · {formatAmount(row.rounds[row.rounds.length - 1].expected_total)}</summary>
                  <ul>{row.rounds.flatMap((round) => round.items.map((item, index) => (
                    <li key={round.round_id + ':' + index}>{item.qty} × {item.item_name} · {formatAmount(item.rate)}</li>
                  )))}</ul>
                  {row.settlement && <p>{row.settlement.mode_of_payment} · {formatAmount(row.settlement.amount)} {row.settlement.reference}</p>}
                </details>
              )}
              {!offlineCaptureEnabled(posProfile) && queue && !offline &&
                canRecoverOfflineOrder(posProfile, user, row.order.order_type) && (!row.settlement || canRecoverOfflineTender(posProfile, user)) && (
                <Button size="sm" variant="outline" disabled={replaying} onClick={() => {
                  const receipt = window.prompt(t('offline.invoice_prompt', {
                    order: row.client_order_id, customer: row.order.customer || row.order.table || '',
                    amount: formatAmount(row.rounds[row.rounds.length - 1].expected_total),
                    tender: row.settlement?.mode_of_payment || '',
                  }));
                  if (confirmed() && receipt?.trim()) void queue.resolveReviewed(row.client_order_id, receipt)
                    .catch((error) => showToast.error(parseFrappeError(error, t('offline.invoice_mismatch'))));
                }}>{t('offline.reconcile_invoice')}</Button>
              )}
              {!row.needs_approval && queue && !offline && (
                <Button size="sm" variant="outline" disabled={replaying} onClick={() => {
                  if (!confirmed()) return;
                  void queue.retryReviewed(row.client_order_id).catch((error) => showToast.error(parseFrappeError(error, t('offline.retry_unavailable'))));
                }}>{t('offline.retry_review')}</Button>
              )}
              {row.needs_approval && !offline && posProfile && canSettle && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => { if (confirmed()) setApprovalRow(row); }}
                >
                  {t('offline.approve_and_replay')}
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {approvalRow && posProfile && (
        <ManagerApproval
          open={true}
          posProfile={posProfile.name}
          privilege="offline_tender"
          title={t('offline.approval_title')}
          summaryLines={[
            t('offline.approval_summary', {
              mode: approvalRow.settlement?.mode_of_payment || '',
              amount: formatAmount(approvalAmount),
              limit: formatCurrency(offlineLimit, posProfile.approval_currency || currencyCode),
            }),
            `${t('offline.client_order')}: ${approvalRow.client_order_id.slice(0, 8)}`,
          ]}
          reference={{ doctype: 'POS Invoice', name: approvalRow.server_invoice || '' }}
          claim={{
            amount: roundForCurrency(approvalAmount, currencyCode),
            limit: roundForCurrency(offlineLimit, currencyCode),
            modes: approvalRow.settlement ? [approvalRow.settlement.mode_of_payment] : [],
          }}
          amount={offlineCompanyAmount(posProfile, approvalAmount) ?? undefined}
          requireReason={true}
          onApproved={handleApprovalGranted}
          onCancel={() => setApprovalRow(null)}
        />
      )}
    </section>
  );
}
