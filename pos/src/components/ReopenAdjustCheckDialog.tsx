import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, FileWarning, RefreshCw, X } from 'lucide-react';
import { Button, Textarea } from '@ury/ui';
import { parseFrappeError } from '@ury/core';
import { t } from '../i18n';
import {
  isAuthRequiredError,
  listReasons,
  overrideErrorMessage,
  type OverrideReason,
} from '../lib/override-api';
import {
  adjustClosedCheck,
  formatMoneyAmount,
  getReturnableCheck,
  reopenClosedCheck,
  type AdjustClosedCheckResponse,
  type ReopenClosedCheckResponse,
  type ReturnableCheckResponse,
} from '../lib/void-return-api';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';

export interface ReopenAdjustBillSummary {
  name: string;
  pos_profile: string;
  currency: string;
  grand_total: number;
  rounded_total: number;
  efris_irn?: string | null;
  efris_status?: string | null;
  items: Array<{ item_name: string; qty: number; amount: number }>;
  payments: Array<{ mode_of_payment: string; amount: number }>;
}

export interface ReopenAdjustCheckDialogProps {
  open: boolean;
  bill: ReopenAdjustBillSummary;
  availableModes?: string[];
  onCompleted: (result: AdjustClosedCheckResponse | ReopenClosedCheckResponse) => void;
  onClose: () => void;
}

export default function ReopenAdjustCheckDialog({
  open,
  bill,
  availableModes,
  onCompleted,
  onClose,
}: ReopenAdjustCheckDialogProps) {
  const [mode, setMode] = useState<'adjust' | 'reopen'>('adjust');
  const [reasons, setReasons] = useState<OverrideReason[]>([]);
  const [selectedReasonCode, setSelectedReasonCode] = useState('');
  const [note, setNote] = useState('');
  const [selectedTender, setSelectedTender] = useState('');
  const [tipInput, setTipInput] = useState('0');
  const [confirmCreditAndRebill, setConfirmCreditAndRebill] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = useState<{
    privilege: 'adjust_closed_check' | 'reopen_check';
    claim: Record<string, unknown>;
    amount: number;
  } | null>(null);

  const [hydratedCheck, setHydratedCheck] = useState<ReturnableCheckResponse | null>(null);

  const isFiscalised = Boolean(
    hydratedCheck?.is_fiscalised ||
      (bill.efris_irn && bill.efris_irn.trim()) ||
      (bill.efris_status && !['', 'Cancelled'].includes(bill.efris_status.trim())),
  );

  const effectiveProfile = hydratedCheck?.pos_profile || bill.pos_profile;
  const effectiveCurrency = hydratedCheck?.currency || bill.currency;
  const effectivePrecision = hydratedCheck?.precision;

  const tenderModes = useMemo(() => {
    const set = new Set<string>(hydratedCheck?.available_modes || availableModes || []);
    for (const p of hydratedCheck?.original_payments || bill.payments || []) {
      if (p.mode_of_payment) set.add(p.mode_of_payment);
    }
    if (set.size === 0) {
      set.add('Cash');
      set.add('Card');
    }
    return Array.from(set);
  }, [availableModes, bill.payments, hydratedCheck]);

  useEffect(() => {
    if (!open) return;
    let active = true;
    setMode('adjust');
    setSelectedReasonCode('');
    setNote('');
    setTipInput('0');
    setConfirmCreditAndRebill(false);
    setError(null);
    setPendingApproval(null);
    setHydratedCheck(null);
    setSelectedTender(bill.payments?.[0]?.mode_of_payment || availableModes?.[0] || 'Cash');

    // Always hydrate authoritative server metadata (is_fiscalised, pos_profile, precision,
    // available_modes, original_payments, charged_tip) even when bill.payments was pre-filled.
    getReturnableCheck(bill.name)
      .then((res) => {
        if (!active || !res || typeof res !== 'object' || !('invoice' in res)) return;
        setHydratedCheck(res);
        const firstMode = res.original_payments?.[0]?.mode_of_payment || res.available_modes?.[0];
        if (firstMode) setSelectedTender(firstMode);
        if (typeof res.charged_tip === 'number' && res.charged_tip > 0) {
          setTipInput(String(res.charged_tip));
        }
      })
      .catch(() => {
        // Best-effort hydration; server re-validates on submit.
      });
    return () => {
      active = false;
    };
  }, [open, bill.name]);

  useEffect(() => {
    if (!open) return;
    let active = true;
    setSelectedReasonCode('');
    const priv = mode === 'adjust' ? 'adjust_closed_check' : 'reopen_check';
    listReasons(priv)
      .then((rows) => {
        if (active) setReasons(rows);
      })
      .catch(() => {
        if (active) setReasons([]);
      });
    return () => {
      active = false;
    };
  }, [open, bill.name, mode]);

  const selectedReason = useMemo(
    () => reasons.find((r) => r.code === selectedReasonCode) ?? null,
    [reasons, selectedReasonCode],
  );
  const requiresNote = Boolean(selectedReason && Number(selectedReason.requires_note) === 1);
  const tipAmount = Math.max(0, Number(tipInput || 0) || 0);
  const checkTotal = Number(hydratedCheck?.grand_total ?? bill.rounded_total ?? bill.grand_total ?? 0);
  const existingTip = Math.max(0, Number(hydratedCheck?.charged_tip ?? 0) || 0);
  const preTipTotal = Math.max(0, checkTotal - existingTip);
  const adjustedTotal = preTipTotal + tipAmount;

  const reasonValid =
    reasons.length > 0
      ? Boolean(selectedReason) && (!requiresNote || note.trim().length >= 4)
      : note.trim().length >= 4;
  const fiscalValid = !isFiscalised || confirmCreditAndRebill;
  const canSubmit = Boolean(reasonValid && fiscalValid && !submitting && (mode === 'reopen' || selectedTender));

  async function handleSubmit(approvalToken?: string) {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    const reasonCode = reasons.length > 0 ? selectedReasonCode : note.trim();
    const trimmedNote = note.trim();
    const creditFlag = isFiscalised && confirmCreditAndRebill;

    try {
      if (mode === 'adjust') {
        const payments = [{ mode_of_payment: selectedTender, amount: adjustedTotal }];
        const res = await adjustClosedCheck({
          invoiceId: bill.name,
          payments,
          reason: reasonCode,
          note: reasons.length > 0 ? trimmedNote : undefined,
          tipAmount,
          creditAndRebill: creditFlag,
          approvalToken,
        });
        onCompleted(res);
      } else {
        const res = await reopenClosedCheck({
          invoiceId: bill.name,
          reason: reasonCode,
          note: reasons.length > 0 ? trimmedNote : undefined,
          creditAndRebill: creditFlag,
          approvalToken,
        });
        onCompleted(res);
      }
    } catch (err) {
      const msg = parseFrappeError(err, '') || (err instanceof Error ? err.message : '');
      if (!approvalToken && isAuthRequiredError(msg || err)) {
        if (mode === 'adjust') {
          setPendingApproval({
            privilege: 'adjust_closed_check',
            amount: adjustedTotal,
            claim: {
              original_invoice: bill.name,
              payments: [{ mode_of_payment: selectedTender, amount: adjustedTotal }],
              tip_amount: tipAmount,
              credit_and_rebill: creditFlag ? 1 : 0,
              reason: selectedReason?.code || reasonCode,
              note: reasons.length > 0 ? trimmedNote : '',
            },
          });
        } else {
          setPendingApproval({
            privilege: 'reopen_check',
            amount: checkTotal,
            claim: {
              original_invoice: bill.name,
              grand_total: checkTotal,
              credit_and_rebill: creditFlag ? 1 : 0,
              reason: selectedReason?.code || reasonCode,
              note: reasons.length > 0 ? trimmedNote : '',
            },
          });
        }
      } else {
        setError(overrideErrorMessage(err) || msg || t('void_return.reopen_failed'));
      }
    } finally {
      setSubmitting(false);
    }
  }

  function handleApproved(result: ManagerApprovalResult) {
    setPendingApproval(null);
    void handleSubmit(result.token);
  }

  if (!open) return null;

  return (
    <>
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-3"
        role="dialog"
        aria-modal="true"
        aria-label={t('void_return.reopen_adjust_title')}
      >
        <div className="w-full max-w-lg rounded-xl bg-white shadow-xl border border-gray-200 overflow-hidden">
          <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3">
            <div className="flex items-center gap-2 min-w-0">
              <RefreshCw className="h-5 w-5 text-blue-700 shrink-0" />
              <h2 className="text-base font-semibold text-gray-900 truncate">
                {t('void_return.reopen_adjust_title')} · {bill.name}
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
            <div className="grid grid-cols-2 gap-2" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={mode === 'adjust'}
                onClick={() => setMode('adjust')}
                className={`min-h-[44px] rounded-lg border px-3 py-2 text-sm font-medium ${
                  mode === 'adjust'
                    ? 'border-blue-600 bg-blue-50 text-blue-900'
                    : 'border-gray-200 bg-white text-gray-700'
                }`}
              >
                {t('void_return.mode_adjust_tender')}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={mode === 'reopen'}
                onClick={() => setMode('reopen')}
                className={`min-h-[44px] rounded-lg border px-3 py-2 text-sm font-medium ${
                  mode === 'reopen'
                    ? 'border-blue-600 bg-blue-50 text-blue-900'
                    : 'border-gray-200 bg-white text-gray-700'
                }`}
              >
                {t('void_return.mode_reopen_check')}
              </button>
            </div>

            {mode === 'adjust' ? (
              <div className="space-y-3 rounded-lg border border-gray-200 bg-gray-50 p-3">
                <p className="text-xs text-gray-600">{t('void_return.adjust_items_locked_hint')}</p>
                <div className="space-y-1.5">
                  <label htmlFor="adjust-tender-select" className="block text-sm font-medium text-gray-700">
                    {t('void_return.replacement_tender_label')}
                  </label>
                  <select
                    id="adjust-tender-select"
                    value={selectedTender}
                    onChange={(e) => setSelectedTender(e.target.value)}
                    disabled={submitting}
                    className="w-full min-h-[44px] rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm"
                  >
                    {tenderModes.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="space-y-1.5">
                  <label htmlFor="adjust-tip-input" className="block text-sm font-medium text-gray-700">
                    {t('void_return.tip_amount_label')}
                  </label>
                  <input
                    id="adjust-tip-input"
                    type="number"
                    min="0"
                    step="any"
                    value={tipInput}
                    onChange={(e) => setTipInput(e.target.value)}
                    disabled={submitting}
                    className="w-full min-h-[44px] rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm"
                  />
                </div>
                <div className="flex justify-between text-sm font-semibold text-gray-900 pt-1 border-t border-gray-200">
                  <span>{t('void_return.adjusted_total_label')}</span>
                  <span>{formatMoneyAmount(adjustedTotal, effectiveCurrency, effectivePrecision)}</span>
                </div>
              </div>
            ) : (
              <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-blue-950">
                {t('void_return.reopen_check_explanation')}
              </div>
            )}

            {isFiscalised && (
              <label className="flex items-start gap-3 rounded-lg border border-amber-300 bg-amber-50 p-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={confirmCreditAndRebill}
                  onChange={(e) => setConfirmCreditAndRebill(e.target.checked)}
                  disabled={submitting}
                  className="mt-1 h-4 w-4 rounded border-amber-500"
                />
                <span className="text-sm text-amber-950">
                  <span className="font-semibold flex items-center gap-1.5">
                    <FileWarning className="h-4 w-4" />
                    {t('void_return.fiscal_reopen_confirm_label')}
                  </span>
                  <span className="text-xs text-amber-800 block mt-0.5">
                    {t('void_return.fiscal_reopen_confirm_hint')}
                  </span>
                </span>
              </label>
            )}

            <div className="space-y-1.5">
              <label htmlFor="reopen-reason-select" className="block text-sm font-medium text-gray-700">
                {t('void_return.reason_label')}
              </label>
              {reasons.length > 0 ? (
                <select
                  id="reopen-reason-select"
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
                <label htmlFor="reopen-reason-note" className="block text-sm font-medium text-gray-700">
                  {requiresNote || reasons.length === 0
                    ? t('void_return.note_required_label')
                    : t('void_return.note_optional_label')}
                </label>
                <Textarea
                  id="reopen-reason-note"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder={t('void_return.note_placeholder')}
                  rows={2}
                  disabled={submitting}
                />
              </div>
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
              onClick={() => void handleSubmit()}
              disabled={!canSubmit}
            >
              {submitting
                ? t('common.loading')
                : mode === 'adjust'
                  ? t('void_return.apply_adjustment')
                  : t('void_return.confirm_reopen')}
            </Button>
          </div>
        </div>
      </div>

      {pendingApproval && (
        <ManagerApproval
          open
          posProfile={effectiveProfile}
          privilege={pendingApproval.privilege}
          title={
            pendingApproval.privilege === 'adjust_closed_check'
              ? t('void_return.mode_adjust_tender')
              : t('void_return.mode_reopen_check')
          }
          summaryLines={[
            `${bill.name} · ${formatMoneyAmount(pendingApproval.amount, effectiveCurrency, effectivePrecision)}`,
            `${t('void_return.reason_label')}: ${selectedReason?.description || selectedReasonCode}`,
          ]}
          reference={{ doctype: 'POS Invoice', name: bill.name }}
          claim={pendingApproval.claim}
          amount={pendingApproval.amount}
          requireReason={false}
          onApproved={handleApproved}
          onCancel={() => setPendingApproval(null)}
        />
      )}
    </>
  );
}
