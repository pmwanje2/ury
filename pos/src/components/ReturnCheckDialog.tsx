import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, BedDouble, FileWarning, RotateCcw, X } from 'lucide-react';
import { Button, Textarea } from '@ury/ui';
import { parseFrappeError } from '@ury/core';
import { t } from '../i18n';
import { isAuthRequiredError, overrideErrorMessage } from '../lib/override-api';
import {
  createPosReturn,
  formatMoneyAmount,
  getReturnableCheck,
  previewPosReturn,
  type CreatePosReturnResponse,
  type RefundPaymentEntry,
  type ReturnableCheckResponse,
  type ReturnPreviewResponse,
} from '../lib/void-return-api';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';

export interface ReturnCheckDialogProps {
  open: boolean;
  invoiceId: string;
  onCompleted: (result: CreatePosReturnResponse) => void;
  onClose: () => void;
}

export default function ReturnCheckDialog({
  open,
  invoiceId,
  onCompleted,
  onClose,
}: ReturnCheckDialogProps) {
  const [loading, setLoading] = useState(false);
  const [checkData, setCheckData] = useState<ReturnableCheckResponse | null>(null);
  const [qtyByRow, setQtyByRow] = useState<Record<string, number>>({});
  const [preview, setPreview] = useState<ReturnPreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [selectedReasonCode, setSelectedReasonCode] = useState('');
  const [note, setNote] = useState('');
  const [allowDifferentTender, setAllowDifferentTender] = useState(false);
  const [differentMode, setDifferentMode] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = useState<{
    claim: Record<string, unknown>;
    refundTotal: number;
    refundPayments: RefundPaymentEntry[];
  } | null>(null);

  useEffect(() => {
    if (!open || !invoiceId) return;
    let active = true;
    setLoading(true);
    setError(null);
    setCheckData(null);
    setQtyByRow({});
    setPreview(null);
    setSelectedReasonCode('');
    setNote('');
    setAllowDifferentTender(false);
    setDifferentMode('');
    setPendingApproval(null);

    getReturnableCheck(invoiceId)
      .then((res) => {
        if (!active) return;
        setCheckData(res);
        const initial: Record<string, number> = {};
        for (const item of res.items) {
          initial[item.item_row_name] = 0;
        }
        setQtyByRow(initial);
        const firstNonOrig = res.available_modes.find(
          (m) => !res.original_payments.some((p) => p.mode_of_payment === m),
        );
        setDifferentMode(firstNonOrig || res.available_modes[0] || '');
      })
      .catch((err) => {
        if (active) {
          setError(parseFrappeError(err, t('void_return.load_check_failed')));
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
    };
  }, [open, invoiceId]);

  const selectedItems = useMemo(
    () =>
      Object.entries(qtyByRow)
        .filter(([, qty]) => qty > 0)
        .map(([item_row_name, qty]) => ({ item_row_name, qty })),
    [qtyByRow],
  );

  useEffect(() => {
    if (!open || !checkData || selectedItems.length === 0) {
      setPreview(null);
      return;
    }
    let active = true;
    setPreviewLoading(true);
    previewPosReturn({
      originalInvoice: checkData.invoice,
      items: selectedItems,
      allowDifferentTender: allowDifferentTender && !checkData.is_room_charge,
    })
      .then((res) => {
        if (active) {
          setPreview(res);
          setError(null);
        }
      })
      .catch((err) => {
        if (active) {
          setError(parseFrappeError(err, t('void_return.preview_failed')));
        }
      })
      .finally(() => {
        if (active) setPreviewLoading(false);
      });
    return () => {
      active = false;
    };
  }, [open, checkData, selectedItems, allowDifferentTender]);

  const reasons = checkData?.reasons || [];
  const selectedReason = useMemo(
    () => reasons.find((r) => r.code === selectedReasonCode) ?? null,
    [reasons, selectedReasonCode],
  );
  const requiresNote = Boolean(selectedReason && Number(selectedReason.requires_note) === 1);
  const effectiveRefundPayments: RefundPaymentEntry[] = useMemo(() => {
    if (!preview) return [];
    if (allowDifferentTender && !checkData?.is_room_charge && differentMode && preview.max_cash_refundable > 0) {
      return [{ mode_of_payment: differentMode, amount: preview.max_cash_refundable }];
    }
    return preview.default_refund_payments;
  }, [preview, allowDifferentTender, checkData, differentMode]);

  const isReasonValid =
    reasons.length > 0
      ? Boolean(selectedReason) && (!requiresNote || note.trim().length >= 4)
      : note.trim().length >= 4;
  const canSubmit =
    Boolean(checkData && preview && selectedItems.length > 0 && isReasonValid && !submitting && !previewLoading);

  function selectAllRemaining() {
    if (!checkData) return;
    const next: Record<string, number> = {};
    for (const row of checkData.items) {
      next[row.item_row_name] = row.remaining_qty;
    }
    setQtyByRow(next);
  }

  async function submitReturn(approvalToken?: string) {
    if (!checkData || !preview || !canSubmit) return;
    setSubmitting(true);
    setError(null);
    const reasonCode = reasons.length > 0 ? selectedReasonCode : note.trim();
    const trimmedNote = note.trim();
    const useDifferent = Boolean(allowDifferentTender && !checkData.is_room_charge);
    try {
      const result = await createPosReturn({
        originalInvoice: checkData.invoice,
        items: selectedItems,
        reason: reasonCode,
        note: reasons.length > 0 ? trimmedNote : undefined,
        refundPayments: useDifferent ? effectiveRefundPayments : undefined,
        allowDifferentTender: useDifferent,
        approvalToken,
      });
      onCompleted(result);
    } catch (err) {
      const msg = parseFrappeError(err, '') || (err instanceof Error ? err.message : '');
      if (!approvalToken && isAuthRequiredError(msg || err)) {
        const sortedItems = selectedItems
          .map((sel) => {
            const meta = checkData.items.find((i) => i.item_row_name === sel.item_row_name);
            return {
              item_row_name: sel.item_row_name,
              item_code: meta?.item_code || '',
              qty: Number(sel.qty),
            };
          })
          .sort((a, b) =>
            a.item_row_name.localeCompare(b.item_row_name) || a.item_code.localeCompare(b.item_code),
          );
        const sortedPayments = [...effectiveRefundPayments]
          .map((p) => ({ mode_of_payment: p.mode_of_payment, amount: Number(p.amount) }))
          .sort((a, b) => a.mode_of_payment.localeCompare(b.mode_of_payment));
        setPendingApproval({
          refundTotal: preview.refund_total,
          refundPayments: sortedPayments,
          claim: {
            original_invoice: checkData.invoice,
            items: sortedItems,
            refund_total: Number(preview.refund_total),
            currency: checkData.currency,
            refund_payments: sortedPayments,
            allow_different_tender: useDifferent ? 1 : 0,
            reason: selectedReason?.code || reasonCode,
            note: reasons.length > 0 ? trimmedNote : '',
          },
        });
      } else {
        setError(overrideErrorMessage(err) || msg || t('void_return.return_failed'));
      }
    } finally {
      setSubmitting(false);
    }
  }

  function handleApproved(result: ManagerApprovalResult) {
    setPendingApproval(null);
    void submitReturn(result.token);
  }

  if (!open) return null;

  return (
    <>
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-3"
        role="dialog"
        aria-modal="true"
        aria-label={t('void_return.return_title')}
      >
        <div className="w-full max-w-xl rounded-xl bg-white shadow-xl border border-gray-200 overflow-hidden">
          <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3">
            <div className="flex items-center gap-2 min-w-0">
              <RotateCcw className="h-5 w-5 text-blue-700 shrink-0" />
              <h2 className="text-base font-semibold text-gray-900 truncate">
                {t('void_return.return_title')} · {invoiceId}
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

          <div className="p-4 space-y-4 max-h-[75vh] overflow-y-auto">
            {loading && <p className="text-sm text-gray-600">{t('common.loading')}</p>}

            {checkData && (
              <>
                <div className="flex flex-wrap gap-2">
                  {checkData.is_room_charge && (
                    <div className="inline-flex items-center gap-1.5 rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-xs font-medium text-blue-900">
                      <BedDouble className="h-4 w-4" />
                      <span>
                        {t('void_return.room_charge_folio_only', {
                          room: checkData.room_number || '—',
                        })}
                      </span>
                    </div>
                  )}
                  {checkData.is_fiscalised && (
                    <div className="inline-flex items-center gap-1.5 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-950">
                      <FileWarning className="h-4 w-4" />
                      <span>{t('void_return.fiscal_credit_note_notice')}</span>
                    </div>
                  )}
                </div>

                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium text-gray-800">
                    {t('void_return.select_return_items')}
                  </span>
                  <button
                    type="button"
                    onClick={selectAllRemaining}
                    className="text-xs font-medium text-blue-700 hover:underline min-h-[36px] px-2"
                  >
                    {t('void_return.select_all_remaining')}
                  </button>
                </div>

                <div className="divide-y divide-gray-200 rounded-lg border border-gray-200">
                  {checkData.items.map((row) => {
                    const chosen = qtyByRow[row.item_row_name] || 0;
                    return (
                      <div
                        key={row.item_row_name}
                        className="flex items-center justify-between gap-3 p-3"
                      >
                        <div className="min-w-0">
                          <div className="font-medium text-gray-900 truncate">{row.item_name}</div>
                          <div className="text-xs text-gray-600">
                            {formatMoneyAmount(row.rate, checkData.currency, checkData.precision)} ·{' '}
                            {t('void_return.remaining_qty_label', {
                              remaining: row.remaining_qty,
                              sold: row.sold_qty,
                            })}
                          </div>
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                          <button
                            type="button"
                            aria-label={`Decrease return quantity for ${row.item_name}`}
                            disabled={chosen <= 0 || submitting}
                            onClick={() =>
                              setQtyByRow((prev) => ({
                                ...prev,
                                [row.item_row_name]: Math.max(0, (prev[row.item_row_name] || 0) - 1),
                              }))
                            }
                            className="h-11 w-11 rounded-lg border border-gray-300 bg-white font-semibold disabled:opacity-40"
                          >
                            −
                          </button>
                          <span className="min-w-[2rem] text-center font-semibold text-gray-900">
                            {chosen}
                          </span>
                          <button
                            type="button"
                            aria-label={`Increase return quantity for ${row.item_name}`}
                            disabled={chosen >= row.remaining_qty || submitting}
                            onClick={() =>
                              setQtyByRow((prev) => ({
                                ...prev,
                                [row.item_row_name]: Math.min(
                                  row.remaining_qty,
                                  (prev[row.item_row_name] || 0) + 1,
                                ),
                              }))
                            }
                            className="h-11 w-11 rounded-lg border border-gray-300 bg-white font-semibold disabled:opacity-40"
                          >
                            +
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>

                {preview && (
                  <div className="rounded-lg bg-gray-50 border border-gray-200 p-3 space-y-1.5 text-sm">
                    <div className="flex justify-between text-gray-700">
                      <span>{t('void_return.return_subtotal')}</span>
                      <span>
                        {formatMoneyAmount(preview.subtotal, preview.currency, preview.precision)}
                      </span>
                    </div>
                    {preview.discount_amount > 0 && (
                      <div className="flex justify-between text-gray-700">
                        <span>{t('void_return.proportional_discount')}</span>
                        <span>
                          −{formatMoneyAmount(preview.discount_amount, preview.currency, preview.precision)}
                        </span>
                      </div>
                    )}
                    {preview.tax_amount > 0 && (
                      <div className="flex justify-between text-gray-700">
                        <span>{t('void_return.proportional_tax')}</span>
                        <span>
                          {formatMoneyAmount(preview.tax_amount, preview.currency, preview.precision)}
                        </span>
                      </div>
                    )}
                    <div className="flex justify-between font-semibold text-gray-900 pt-1 border-t border-gray-200">
                      <span>{t('void_return.refund_total')}</span>
                      <span>
                        {formatMoneyAmount(preview.refund_total, preview.currency, preview.precision)}
                      </span>
                    </div>
                    {preview.unpaid_credit_reduction > 0 && (
                      <div className="text-xs text-amber-800 pt-1">
                        {t('void_return.unpaid_balance_credit_note', {
                          paid: formatMoneyAmount(
                            preview.max_cash_refundable,
                            preview.currency,
                            preview.precision,
                          ),
                          unpaid: formatMoneyAmount(
                            preview.unpaid_credit_reduction,
                            preview.currency,
                            preview.precision,
                          ),
                        })}
                      </div>
                    )}
                    <div className="text-xs text-gray-600 pt-1">
                      {t('void_return.refund_tender_summary')}:{' '}
                      {effectiveRefundPayments
                        .map(
                          (p) =>
                            `${p.mode_of_payment} (${formatMoneyAmount(
                              p.amount,
                              preview.currency,
                              preview.precision,
                            )})`,
                        )
                        .join(', ') || t('void_return.no_cash_tender_refund')}
                    </div>
                  </div>
                )}

                {!checkData.is_room_charge && checkData.available_modes.length > 1 && (
                  <div className="space-y-2 rounded-lg border border-gray-200 p-3">
                    <label className="flex items-center gap-2 text-sm text-gray-800 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={allowDifferentTender}
                        onChange={(e) => setAllowDifferentTender(e.target.checked)}
                        className="h-4 w-4 rounded border-gray-300"
                      />
                      <span>{t('void_return.use_different_tender')}</span>
                    </label>
                    {allowDifferentTender && (
                      <select
                        aria-label={t('void_return.refund_tender_label')}
                        value={differentMode}
                        onChange={(e) => setDifferentMode(e.target.value)}
                        className="w-full min-h-[44px] rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm"
                      >
                        {checkData.available_modes.map((m) => (
                          <option key={m} value={m}>
                            {m}
                          </option>
                        ))}
                      </select>
                    )}
                  </div>
                )}

                <div className="space-y-1.5">
                  <label htmlFor="return-reason-select" className="block text-sm font-medium text-gray-700">
                    {t('void_return.return_reason_label')}
                  </label>
                  {reasons.length > 0 ? (
                    <select
                      id="return-reason-select"
                      value={selectedReasonCode}
                      onChange={(e) => setSelectedReasonCode(e.target.value)}
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
                  ) : null}
                </div>

                {(reasons.length === 0 || requiresNote || selectedReasonCode) && (
                  <div className="space-y-1.5">
                    <label htmlFor="return-reason-note" className="block text-sm font-medium text-gray-700">
                      {requiresNote || reasons.length === 0
                        ? t('void_return.note_required_label')
                        : t('void_return.note_optional_label')}
                    </label>
                    <Textarea
                      id="return-reason-note"
                      value={note}
                      onChange={(e) => setNote(e.target.value)}
                      placeholder={t('void_return.note_placeholder')}
                      rows={2}
                      disabled={submitting}
                    />
                  </div>
                )}
              </>
            )}

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
              onClick={() => void submitReturn()}
              disabled={!canSubmit}
            >
              {submitting ? t('common.loading') : t('void_return.process_return')}
            </Button>
          </div>
        </div>
      </div>

      {pendingApproval && checkData && (
        <ManagerApproval
          open
          posProfile={checkData.pos_profile}
          privilege="return"
          title={t('void_return.return_approval_title')}
          summaryLines={[
            `${t('void_return.return_title')}: ${checkData.invoice}`,
            `${t('void_return.refund_total')}: ${formatMoneyAmount(
              pendingApproval.refundTotal,
              checkData.currency,
              checkData.precision,
            )}`,
            `${t('void_return.reason_label')}: ${selectedReason?.description || selectedReasonCode}`,
          ]}
          reference={{ doctype: 'POS Invoice', name: checkData.invoice }}
          claim={pendingApproval.claim}
          amount={pendingApproval.refundTotal}
          requireReason={false}
          onApproved={handleApproved}
          onCancel={() => setPendingApproval(null)}
        />
      )}
    </>
  );
}
