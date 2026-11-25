import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Trash2, X } from 'lucide-react';
import { Button, Textarea } from '@ury/ui';
import { parseFrappeError } from '@ury/core';
import { t } from '../i18n';
import {
  isAuthRequiredError,
  listReasons,
  overrideActionErrorMessage,
  type OverrideReason,
} from '../lib/override-api';
import {
  formatMoneyAmount,
  reduceSentItemWithWaste,
  type ReduceItemWithWasteResponse,
} from '../lib/void-return-api';
import { extractAuthChallenge } from '../lib/privileged-actions-api';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';

export interface VoidItemDialogProps {
  open: boolean;
  posProfile: string;
  invoiceId: string;
  currency: string;
  item: {
    name: string;
    item_code?: string;
    item_name: string;
    qty: number;
    rate: number;
    reservation_line_key?: string | null;
    custom_is_modifier?: number;
  };
  orderItems?: Array<{
    name: string;
    item_code?: string;
    item_name?: string;
    qty: number;
    rate: number;
    custom_is_modifier?: number;
    custom_parent_line_key?: string | null;
  }>;
  onCompleted: (result: ReduceItemWithWasteResponse) => void;
  onClose: () => void;
}

function isWasteReasonRow(reason: OverrideReason | null): boolean {
  if (!reason) return false;
  return (
    reason.code.toUpperCase().startsWith('WASTE') ||
    reason.description.toLowerCase().includes('waste')
  );
}

export default function VoidItemDialog({
  open,
  posProfile,
  invoiceId,
  currency,
  item,
  orderItems,
  onCompleted,
  onClose,
}: VoidItemDialogProps) {
  const [voidQty, setVoidQty] = useState(1);
  const [reasons, setReasons] = useState<OverrideReason[]>([]);
  const [selectedReasonCode, setSelectedReasonCode] = useState('');
  const [note, setNote] = useState('');
  const [isWaste, setIsWaste] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = useState<{
    privilege: 'void_sent_item' | 'reduce_sent_qty';
    claim: Record<string, unknown>;
    newQty: number;
    amount: number;
    approvalAmount?: number;
  } | null>(null);

  const newQty = Math.max(0, item.qty - voidQty);
  const privilege: 'void_sent_item' | 'reduce_sent_qty' =
    newQty === 0 ? 'void_sent_item' : 'reduce_sent_qty';

  const voidAmount = useMemo(() => {
    let rawAmount = Number(item.rate || 0) * voidQty;
    const parentLineKey = item.reservation_line_key;
    if (parentLineKey && Number(item.custom_is_modifier || 0) !== 1 && orderItems?.length) {
      for (const child of orderItems) {
        if (
          child.name === item.name ||
          Number(child.custom_is_modifier || 0) !== 1 ||
          child.custom_parent_line_key !== parentLineKey ||
          Number(child.qty || 0) <= 0
        ) {
          continue;
        }
        const childQty = Number(child.qty || 0);
        let childNewQty = childQty;
        if (newQty === 0) {
          childNewQty = 0;
        } else if (item.qty > 0) {
          const scaled = (childQty * newQty) / item.qty;
          childNewQty = Math.abs(scaled - Math.round(scaled)) < 1e-6 ? Math.round(scaled) : childQty;
        }
        if (childNewQty < childQty) {
          rawAmount += Number(child.rate || 0) * (childQty - childNewQty);
        }
      }
    }
    return Math.round(rawAmount * 10000) / 10000;
  }, [item, orderItems, voidQty, newQty]);

  useEffect(() => {
    if (!open) return;
    let active = true;
    setVoidQty(1);
    setSelectedReasonCode('');
    setNote('');
    setIsWaste(false);
    setError(null);
    setPendingApproval(null);
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
  }, [open, privilege]);

  const selectedReason = useMemo(
    () => reasons.find((r) => r.code === selectedReasonCode) ?? null,
    [reasons, selectedReasonCode],
  );

  const requiresNote = Boolean(selectedReason && Number(selectedReason.requires_note) === 1);
  const hasConfiguredReasons = reasons.length > 0;
  const reasonValue = hasConfiguredReasons ? selectedReasonCode : note.trim();
  const isValid =
    voidQty >= 1 &&
    voidQty <= item.qty &&
    (hasConfiguredReasons
      ? Boolean(selectedReason) && (!requiresNote || note.trim().length >= 4)
      : note.trim().length >= 4);

  function handleReasonChange(code: string) {
    setSelectedReasonCode(code);
    const found = reasons.find((r) => r.code === code) ?? null;
    setIsWaste(isWasteReasonRow(found));
    setError(null);
  }

  async function submitVoid(approvalToken?: string) {
    if (!isValid || submitting) return;
    setSubmitting(true);
    setError(null);
    const trimmedNote = note.trim();
    const delta = newQty - item.qty;
    try {
      const result = await reduceSentItemWithWaste({
        invoiceId,
        itemRowName: item.name,
        newQty,
        reason: reasonValue,
        note: hasConfiguredReasons ? trimmedNote : undefined,
        isWaste,
        approvalToken,
      });
      onCompleted(result);
    } catch (err) {
      const msg = parseFrappeError(err, '') || (err instanceof Error ? err.message : '');
      if (!approvalToken && isAuthRequiredError(msg || err)) {
        setPendingApproval({
          privilege,
          newQty,
          amount: voidAmount,
          approvalAmount: extractAuthChallenge(err)?.amount ?? undefined,
          claim: {
            invoice: invoiceId,
            item_row_name: item.name,
            item_code: item.item_code || '',
            previous_qty: Number(item.qty),
            new_qty: Number(newQty),
            delta: Number(delta),
            reason: selectedReason?.code || reasonValue,
            note: hasConfiguredReasons ? trimmedNote : '',
            is_waste: isWaste ? 1 : 0,
          },
        });
      } else {
        setError(overrideActionErrorMessage(err) || msg || t('void_return.void_failed'));
      }
    } finally {
      setSubmitting(false);
    }
  }

  function handleApproved(result: ManagerApprovalResult) {
    setPendingApproval(null);
    void submitVoid(result.token);
  }

  if (!open) return null;

  return (
    <>
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-3"
        role="dialog"
        aria-modal="true"
        aria-labelledby="void-item-dialog-title"
      >
        <div className="w-full max-w-md rounded-xl bg-white shadow-xl border border-gray-200 overflow-hidden">
          <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3">
            <div className="flex items-center gap-2">
              <Trash2 className="h-5 w-5 text-red-600" />
              <h2 id="void-item-dialog-title" className="text-base font-semibold text-gray-900">
                {t('void_return.void_item_title')}
              </h2>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label={t('common.close')}
              className="rounded-lg p-2 text-gray-500 hover:bg-gray-100 min-h-[44px] min-w-[44px] flex items-center justify-center"
            >
              <X className="h-5 w-5" />
            </button>
          </div>

          <div className="p-4 space-y-4 max-h-[80vh] overflow-y-auto">
            <div className="rounded-lg bg-gray-50 border border-gray-200 p-3 flex items-center justify-between gap-2">
              <div>
                <div className="font-medium text-gray-900">{item.item_name}</div>
                <div className="text-xs text-gray-600">
                  {t('void_return.sent_qty_summary', { qty: item.qty })} ·{' '}
                  {formatMoneyAmount(voidAmount, currency)}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  aria-label={t('void_return.decrease_qty')}
                  disabled={voidQty <= 1 || submitting}
                  onClick={() => setVoidQty((q) => Math.max(1, q - 1))}
                  className="h-11 w-11 rounded-lg border border-gray-300 bg-white font-semibold text-gray-800 disabled:opacity-40"
                >
                  −
                </button>
                <span className="min-w-[2rem] text-center font-semibold text-gray-900">
                  {voidQty}
                </span>
                <button
                  type="button"
                  aria-label={t('void_return.increase_qty')}
                  disabled={voidQty >= item.qty || submitting}
                  onClick={() => setVoidQty((q) => Math.min(item.qty, q + 1))}
                  className="h-11 w-11 rounded-lg border border-gray-300 bg-white font-semibold text-gray-800 disabled:opacity-40"
                >
                  +
                </button>
              </div>
            </div>

            {hasConfiguredReasons ? (
              <div className="space-y-1.5">
                <label htmlFor="void-item-reason" className="block text-sm font-medium text-gray-700">
                  {t('void_return.reason_label')}
                </label>
                <select
                  id="void-item-reason"
                  value={selectedReasonCode}
                  onChange={(e) => handleReasonChange(e.target.value)}
                  disabled={submitting}
                  className="w-full min-h-[44px] rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900"
                >
                  <option value="">{t('void_return.choose_reason')}</option>
                  {reasons.map((r) => (
                    <option key={r.code} value={r.code}>
                      {r.description}
                    </option>
                  ))}
                </select>
              </div>
            ) : null}

            {(!hasConfiguredReasons || requiresNote || selectedReasonCode) && (
              <div className="space-y-1.5">
                <label htmlFor="void-item-note" className="block text-sm font-medium text-gray-700">
                  {requiresNote || !hasConfiguredReasons
                    ? t('void_return.note_required_label')
                    : t('void_return.note_optional_label')}
                </label>
                <Textarea
                  id="void-item-note"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder={t('void_return.note_placeholder')}
                  rows={2}
                  disabled={submitting}
                />
              </div>
            )}

            <label className="flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 p-3 cursor-pointer">
              <input
                type="checkbox"
                checked={isWaste}
                onChange={(e) => setIsWaste(e.target.checked)}
                disabled={submitting}
                className="mt-1 h-4 w-4 rounded border-amber-400"
              />
              <span className="text-sm text-amber-950">
                <span className="font-medium block">{t('void_return.mark_as_waste')}</span>
                <span className="text-xs text-amber-800">{t('void_return.waste_stock_hint')}</span>
              </span>
            </label>

            {error && (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800"
              >
                <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
                <span>{error}</span>
              </div>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 border-t border-gray-200 px-4 py-3 bg-gray-50">
            <Button type="button" variant="outline" onClick={onClose} disabled={submitting}>
              {t('common.cancel')}
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => void submitVoid()}
              disabled={!isValid || submitting}
            >
              {submitting ? t('common.loading') : t('void_return.confirm_void')}
            </Button>
          </div>
        </div>
      </div>

      {pendingApproval && (
        <ManagerApproval
          open
          posProfile={posProfile}
          privilege={pendingApproval.privilege}
          title={t('void_return.void_approval_title')}
          summaryLines={[
            `${item.item_name}: ${item.qty} → ${pendingApproval.newQty}`,
            formatMoneyAmount(pendingApproval.amount, currency),
            `${t('void_return.reason_label')}: ${selectedReason?.description || reasonValue}`,
          ]}
          reference={{ doctype: 'POS Invoice', name: invoiceId }}
          claim={pendingApproval.claim}
          amount={pendingApproval.approvalAmount}
          requireReason={false}
          onApproved={handleApproved}
          onCancel={() => setPendingApproval(null)}
        />
      )}
    </>
  );
}
