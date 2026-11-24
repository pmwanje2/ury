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
import { addOpenItem, overrideLinePrice } from '../lib/privileged-actions-api';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';

export interface PriceOverrideDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: 'override' | 'open_item';
  posProfile: string;
  invoiceName: string;
  itemRowName?: string;
  itemCode?: string;
  itemName: string;
  currentRate: number;
  currency?: string;
  onSuccess: () => void | Promise<void>;
}

export default function PriceOverrideDialog({
  open,
  onOpenChange,
  mode,
  posProfile,
  invoiceName,
  itemRowName,
  itemCode,
  itemName,
  currentRate,
  currency = 'UGX',
  onSuccess,
}: PriceOverrideDialogProps) {
  const privilege = mode === 'override' ? 'price_override' : 'open_item';
  const [rateInput, setRateInput] = useState(String(currentRate || ''));
  const [qtyInput, setQtyInput] = useState('1');
  const [reference, setReference] = useState('');
  const [reasons, setReasons] = useState<OverrideReason[]>([]);
  const [selectedReason, setSelectedReason] = useState('');
  const [reasonNote, setReasonNote] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = useState<{
    claim: Record<string, unknown>;
    amount: number;
    percent?: number;
  } | null>(null);

  useEffect(() => {
    if (!open) {
      setPendingApproval(null);
      setError(null);
      return;
    }
    setRateInput(mode === 'override' ? String(currentRate ?? '') : '');
    setQtyInput('1');
    setReference('');
    setSelectedReason('');
    setReasonNote('');
    setError(null);
    setPendingApproval(null);
    let active = true;
    listReasons(privilege)
      .then((rows) => {
        if (active) setReasons(rows);
      })
      .catch(() => {
        if (active) setReasons([]);
      });
    return () => {
      active = false;
    };
  }, [open, mode, currentRate, privilege]);

  const selectedReasonObj = reasons.find((r) => r.code === selectedReason) ?? null;
  const needsNote = Boolean(selectedReasonObj && Number(selectedReasonObj.requires_note) === 1);

  const submit = async (approval?: ManagerApprovalResult) => {
    const parsedRate = Number(rateInput);
    if (!Number.isFinite(parsedRate) || parsedRate < 0) {
      setError(t('privileged_actions.invalid_price'));
      return;
    }
    const parsedQty = Number(qtyInput || 1);
    if (mode === 'open_item' && (!Number.isFinite(parsedQty) || parsedQty <= 0)) {
      setError(t('privileged_actions.invalid_qty'));
      return;
    }

    setSubmitting(true);
    setError(null);
    const effectiveReason = selectedReason || approval?.reason || '';
    const effectiveNote = reasonNote.trim() || approval?.note || '';

    try {
      if (mode === 'override') {
        if (!itemRowName) {
          throw new Error(t('privileged_actions.missing_line'));
        }
        await overrideLinePrice({
          invoice: invoiceName,
          itemRowName,
          newRate: parsedRate,
          reason: effectiveReason,
          reasonNote: effectiveNote,
          approvalToken: approval?.token,
        });
      } else {
        if (!itemCode) {
          throw new Error(t('privileged_actions.missing_item'));
        }
        await addOpenItem({
          invoice: invoiceName,
          itemCode,
          qty: parsedQty,
          rate: parsedRate,
          reference: reference.trim(),
          reason: effectiveReason,
          reasonNote: effectiveNote,
          approvalToken: approval?.token,
        });
      }
      showToast.success(
        mode === 'override'
          ? t('privileged_actions.price_override_saved')
          : t('privileged_actions.open_item_added'),
      );
      await onSuccess();
      onOpenChange(false);
    } catch (err) {
      if (!approval?.token && isAuthRequiredError(err)) {
        const delta = Math.abs(parsedRate - currentRate);
        const pct = currentRate > 0 ? Math.round((delta / currentRate) * 10000) / 100 : undefined;
        const claim =
          mode === 'override'
            ? {
                action: 'price_override',
                invoice: invoiceName,
                item_row_name: itemRowName || '',
                from_rate: currentRate,
                to_rate: parsedRate,
              }
            : {
                action: 'open_item',
                invoice: invoiceName,
                item_code: itemCode || '',
                qty: parsedQty,
                rate: parsedRate,
                reference: reference.trim(),
              };
        setPendingApproval({ claim, amount: mode === 'override' ? delta : parsedRate, percent: pct });
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
            <DialogTitle>
              {mode === 'override'
                ? t('privileged_actions.price_override_title')
                : t('privileged_actions.open_item')}
            </DialogTitle>
            <DialogDescription>
              {itemName} · {t('privileged_actions.current_price')}: {formatCurrency(currentRate, currency)}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 px-6 pb-2">
            {mode === 'open_item' && (
              <div className="space-y-1.5">
                <label htmlFor="open-item-reference" className="block text-sm font-medium text-gray-900">
                  {t('privileged_actions.open_item_reference')}
                </label>
                <input
                  id="open-item-reference"
                  type="text"
                  value={reference}
                  onChange={(e) => setReference(e.target.value)}
                  disabled={submitting}
                  placeholder={t('privileged_actions.open_item_reference_placeholder')}
                  className="min-h-11 w-full rounded-md border border-border px-3 py-2 text-sm"
                />
              </div>
            )}

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <label htmlFor="price-override-rate" className="block text-sm font-medium text-gray-900">
                  {mode === 'override'
                    ? t('privileged_actions.new_price')
                    : t('privileged_actions.open_item_price')}
                </label>
                <input
                  id="price-override-rate"
                  type="number"
                  inputMode="decimal"
                  min={0}
                  step="any"
                  value={rateInput}
                  onChange={(e) => setRateInput(e.target.value)}
                  disabled={submitting}
                  className="min-h-11 w-full rounded-md border border-border px-3 py-2 text-sm tabular-nums"
                />
              </div>

              {mode === 'open_item' && (
                <div className="space-y-1.5">
                  <label htmlFor="open-item-qty" className="block text-sm font-medium text-gray-900">
                    {t('privileged_actions.quantity')}
                  </label>
                  <input
                    id="open-item-qty"
                    type="number"
                    inputMode="decimal"
                    min={0.01}
                    step="any"
                    value={qtyInput}
                    onChange={(e) => setQtyInput(e.target.value)}
                    disabled={submitting}
                    className="min-h-11 w-full rounded-md border border-border px-3 py-2 text-sm tabular-nums"
                  />
                </div>
              )}
            </div>

            <div className="space-y-1.5">
              <label htmlFor="price-override-reason" className="block text-sm font-medium text-gray-900">
                {t('privileged_actions.reason')}
              </label>
              {reasons.length > 0 ? (
                <select
                  id="price-override-reason"
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
                  id="price-override-reason"
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
                <label htmlFor="price-override-note" className="block text-sm font-medium text-gray-900">
                  {t('privileged_actions.reason_note')}
                </label>
                <Textarea
                  id="price-override-note"
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
              {mode === 'override' ? t('privileged_actions.apply_price') : t('privileged_actions.add_open_item')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {pendingApproval && (
        <ManagerApproval
          open
          posProfile={posProfile}
          privilege={privilege}
          title={
            mode === 'override'
              ? t('privileged_actions.price_override')
              : t('privileged_actions.open_item')
          }
          summaryLines={[
            `${itemName}: ${formatCurrency(currentRate, currency)} → ${formatCurrency(Number(rateInput || 0), currency)}`,
            `${t('privileged_actions.check_ref')}: ${invoiceName}`,
          ]}
          reference={{ doctype: 'POS Invoice', name: invoiceName }}
          claim={pendingApproval.claim}
          amount={pendingApproval.amount}
          percent={pendingApproval.percent}
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
