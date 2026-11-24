import { useEffect, useState } from 'react';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Textarea,
  showToast,
} from '@ury/ui';
import { formatCurrency } from '@ury/core';
import { t } from '../i18n';
import {
  isAuthRequiredError,
  listReasons,
  overrideErrorMessage,
  type OverrideReason,
} from '../lib/override-api';
import { adjustClosedCheckTip, extractAuthChallenge } from '../lib/privileged-actions-api';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';

export interface TipAdjustDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  posProfile: string;
  invoiceName: string;
  currentTip?: number;
  tipServer?: string;
  modeOfPayment?: string;
  currency?: string;
  onSuccess: () => void | Promise<void>;
}

export default function TipAdjustDialog({
  open,
  onOpenChange,
  posProfile,
  invoiceName,
  currentTip = 0,
  tipServer,
  modeOfPayment,
  currency = 'UGX',
  onSuccess,
}: TipAdjustDialogProps) {
  const [tipInput, setTipInput] = useState(String(currentTip || ''));
  const [reasons, setReasons] = useState<OverrideReason[]>([]);
  const [selectedReason, setSelectedReason] = useState('');
  const [reasonNote, setReasonNote] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = useState<{
    claim: Record<string, unknown>;
    amount: number;
  } | null>(null);

  useEffect(() => {
    if (!open) {
      setPendingApproval(null);
      setError(null);
      return;
    }
    setTipInput(String(currentTip ?? 0));
    setSelectedReason('');
    setReasonNote('');
    setError(null);
    let active = true;
    listReasons('tip_adjust')
      .then((rows) => {
        if (active) setReasons(rows);
      })
      .catch(() => {
        if (active) setReasons([]);
      });
    return () => {
      active = false;
    };
  }, [open, currentTip]);

  const selectedReasonObj = reasons.find((r) => r.code === selectedReason) ?? null;
  const needsNote = Boolean(selectedReasonObj && Number(selectedReasonObj.requires_note) === 1);

  const submit = async (approval?: ManagerApprovalResult) => {
    const parsedTip = Number(tipInput);
    if (!Number.isFinite(parsedTip) || parsedTip < 0) {
      setError(t('privileged_actions.invalid_tip'));
      return;
    }
    setSubmitting(true);
    setError(null);
    const effectiveReason = selectedReason || approval?.reason || '';
    const effectiveNote = reasonNote.trim() || approval?.note || '';

    try {
      await adjustClosedCheckTip({
        invoice: invoiceName,
        newTipAmount: parsedTip,
        tipServer,
        modeOfPayment,
        reason: effectiveReason,
        reasonNote: effectiveNote,
        approvalToken: approval?.token,
      });
      showToast.success(t('privileged_actions.tip_adjusted'));
      await onSuccess();
      onOpenChange(false);
    } catch (err) {
      if (!approval?.token && isAuthRequiredError(err)) {
        const challenge = extractAuthChallenge(err);
        setPendingApproval({
          amount: typeof challenge?.amount === 'number' ? challenge.amount : Math.abs(parsedTip - currentTip),
          claim: challenge?.claim ?? {
            action: 'tip_adjust',
            invoice: invoiceName,
            from_tip: currentTip,
            to_tip: parsedTip,
            delta: parsedTip - currentTip,
          },
        });
        return;
      }
      setError(overrideErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <Dialog open={open} onOpenChange={(next) => !submitting && onOpenChange(next)}>
        <DialogContent size="sm" onClose={() => !submitting && onOpenChange(false)}>
          <DialogHeader>
            <DialogTitle>{t('privileged_actions.tip_adjust')}</DialogTitle>
            <DialogDescription>
              {invoiceName} · {t('privileged_actions.current_tip')}: {formatCurrency(currentTip, currency)}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 px-6 pb-2">
            <div className="space-y-1.5">
              <label htmlFor="tip-adjust-amount" className="block text-sm font-medium text-gray-900">
                {t('privileged_actions.tip_amount')}
              </label>
              <input
                id="tip-adjust-amount"
                type="number"
                inputMode="decimal"
                min={0}
                step="any"
                value={tipInput}
                onChange={(e) => setTipInput(e.target.value)}
                disabled={submitting}
                className="min-h-11 w-full rounded-md border border-border px-3 py-2 text-sm tabular-nums"
              />
            </div>

            <div className="space-y-1.5">
              <label htmlFor="tip-adjust-reason" className="block text-sm font-medium text-gray-900">
                {t('privileged_actions.reason')}
              </label>
              {reasons.length > 0 ? (
                <select
                  id="tip-adjust-reason"
                  value={selectedReason}
                  onChange={(e) => setSelectedReason(e.target.value)}
                  disabled={submitting}
                  className="min-h-11 w-full rounded-md border border-border bg-white px-3 py-2 text-sm"
                >
                  <option value="">{t('privileged_actions.select_reason')}</option>
                  {reasons.map((r) => (
                    <option key={r.code} value={r.code}>
                      {r.description || r.code}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id="tip-adjust-reason"
                  type="text"
                  value={selectedReason}
                  onChange={(e) => setSelectedReason(e.target.value)}
                  disabled={submitting}
                  placeholder={t('privileged_actions.enter_reason')}
                  className="min-h-11 w-full rounded-md border border-border px-3 py-2 text-sm"
                />
              )}
            </div>

            {(needsNote || reasonNote) && (
              <div className="space-y-1.5">
                <label htmlFor="tip-adjust-note" className="block text-sm font-medium text-gray-900">
                  {t('privileged_actions.reason_note')}
                </label>
                <Textarea
                  id="tip-adjust-note"
                  value={reasonNote}
                  onChange={(e) => setReasonNote(e.target.value)}
                  disabled={submitting}
                  rows={2}
                />
              </div>
            )}

            {error && (
              <p role="alert" className="rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700">
                {error}
              </p>
            )}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting} className="min-h-11">
              {t('common.cancel')}
            </Button>
            <Button onClick={() => void submit()} disabled={submitting} className="min-h-11">
              {t('privileged_actions.save_tip')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {pendingApproval && (
        <ManagerApproval
          open
          posProfile={posProfile}
          privilege="tip_adjust"
          title={t('privileged_actions.tip_adjust')}
          summaryLines={[
            `${t('privileged_actions.tip_amount')}: ${formatCurrency(currentTip, currency)} → ${formatCurrency(Number(tipInput || 0), currency)}`,
            `${t('privileged_actions.check_ref')}: ${invoiceName}`,
          ]}
          reference={{ doctype: 'POS Invoice', name: invoiceName }}
          claim={pendingApproval.claim}
          amount={pendingApproval.amount}
          requireReason={false}
          onApproved={(res) => {
            setPendingApproval(null);
            void submit(res);
          }}
          onCancel={() => setPendingApproval(null)}
        />
      )}
    </>
  );
}
