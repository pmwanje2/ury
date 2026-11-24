import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';
import { useOutbox } from '../offline/use-outbox';
import { t } from '../i18n';
import { useState } from 'react';
import { Button, showToast } from '@ury/ui';
import { parseFrappeError } from '@ury/core';
import {
  canCaptureOffline,
  type OfflineEntry,
  type OfflineOutbox,
  type OfflinePaymentModeConfig,
} from '../offline/outbox';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';

function PendingOrder({
  row,
  queue,
  eligibleModes,
  offlineLimit,
  disabled,
}: {
  row: OfflineEntry;
  queue: OfflineOutbox;
  eligibleModes: OfflinePaymentModeConfig[];
  offlineLimit: number;
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
  const overLimit = offlineLimit > 0 && total > offlineLimit + 0.005;

  const action = async (run: () => Promise<unknown>) => {
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
        {t('payment.total')}: {total}
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
          {!row.bill_printed && (
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
      ) : (
        <div className="space-y-1.5 mt-1">
          {overLimit && (
            <p className="text-xs font-medium text-amber-900">
              {t('offline.over_limit_notice', { limit: offlineLimit })}
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
      )}
    </li>
  );
}

export default function OfflineBanner() {
  const { posProfile } = usePOSStore();
  const { user } = useRootStore();
  const { rows, replaying, offline, queue } = useOutbox(posProfile, user, true);
  const [approvalRow, setApprovalRow] = useState<OfflineEntry | null>(null);

  const pending = rows.filter((row) => row.state === 'pending').length;
  const review = rows.filter((row) => row.state === 'review');
  if (!rows.length && (!offline || !canCaptureOffline(posProfile, user))) return null;

  const roomChargeSet = new Set(posProfile?.room_charge_modes || []);
  const eligibleModes = (posProfile?.offline_payment_modes || []).filter(
    (mode) => mode.allow_offline && mode.type !== 'Phone' && !roomChargeSet.has(mode.mode_of_payment),
  );
  const offlineLimit = Number(posProfile?.offline_tender_limit || 0);

  const handleApprovalGranted = async (result: ManagerApprovalResult) => {
    const target = approvalRow;
    setApprovalRow(null);
    if (!target || !queue) return;
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
                  offlineLimit={offlineLimit}
                  disabled={replaying || queue.sessionBlocked || !canCaptureOffline(posProfile, user)}
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
              {row.needs_approval && !offline && posProfile && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setApprovalRow(row)}
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
              amount: approvalAmount,
              limit: offlineLimit,
            }),
            `${t('offline.client_order')}: ${approvalRow.client_order_id.slice(0, 8)}`,
          ]}
          reference={{ doctype: 'POS Invoice', name: approvalRow.server_invoice || '' }}
          claim={{
            amount: Math.round(approvalAmount * 100) / 100,
            limit: Math.round(offlineLimit * 100) / 100,
            modes: approvalRow.settlement ? [approvalRow.settlement.mode_of_payment] : [],
          }}
          amount={approvalAmount}
          requireReason={true}
          onApproved={handleApprovalGranted}
          onCancel={() => setApprovalRow(null)}
        />
      )}
    </section>
  );
}
