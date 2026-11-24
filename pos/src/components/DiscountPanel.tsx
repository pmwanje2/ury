import React, { useEffect, useMemo, useRef, useState } from 'react';
import { X, Percent, Tag, CheckCircle2, Trash2, Delete } from 'lucide-react';
import { Button, Dialog, DialogContent, showToast } from '@ury/ui';
import { parseFrappeError } from '@ury/core';
import { t } from '../i18n';
import type { POSInvoiceItem } from '../lib/invoice-api';
import {
  applyCheckDiscount,
  currencyPrecisionFor,
  formatCurrencyPrecision,
  listDiscountOptions,
  previewCheckDiscount,
  removeCheckDiscount,
  type CheckDiscountSummary,
  type DiscountOptionsResponse,
  type DiscountPreviewResponse,
  type LineDiscountInput,
} from '../lib/discount-api';
import DiscountManagerApproval from './DiscountManagerApproval';

export interface DiscountPanelProps {
  open: boolean;
  invoice: string;
  posProfile: string;
  items: POSInvoiceItem[];
  currency?: string | null;
  existingDiscounts: CheckDiscountSummary | null;
  onClose: () => void;
  onApplied: (summary: DiscountPreviewResponse) => void | Promise<void>;
}

type ScopeTab = 'check' | 'line';
type EntryMode = 'percent' | 'amount';

interface LineDraft {
  mode: EntryMode;
  value: string;
}

const QUICK_PERCENTS = [5, 10, 15, 20, 100];

const DiscountPanel: React.FC<DiscountPanelProps> = ({
  open,
  invoice,
  posProfile,
  items,
  currency: propCurrency,
  existingDiscounts,
  onClose,
  onApplied,
}) => {
  const [options, setOptions] = useState<DiscountOptionsResponse | null>(null);
  const [loadingOptions, setLoadingOptions] = useState(false);
  const [tab, setTab] = useState<ScopeTab>('check');

  // Check-level draft
  const [selectedPreset, setSelectedPreset] = useState<string | null>(null);
  const [checkMode, setCheckMode] = useState<EntryMode>('percent');
  const [checkValue, setCheckValue] = useState('');

  // Line-level drafts (row name -> {mode, value})
  const [selectedRow, setSelectedRow] = useState<string | null>(null);
  const [lineDrafts, setLineDrafts] = useState<Record<string, LineDraft>>({});

  // Shared mandatory reason & conditional note
  const [reason, setReason] = useState('');
  const [note, setNote] = useState('');

  // Live server preview & errors
  const [preview, setPreview] = useState<DiscountPreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Approval state (cleared immediately whenever ANY discount field changes!)
  const [approved, setApproved] = useState<{ token: string; approverName: string } | null>(null);
  const [removeApprovalOpen, setRemoveApprovalOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [removing, setRemoving] = useState(false);

  const previewSeq = useRef(0);

  const activeItems = useMemo(
    () => items.filter((item) => !item.is_disposable && item.qty > 0),
    [items],
  );

  // Initialize from existing saved discounts when opening
  useEffect(() => {
    if (!open || !posProfile || !invoice) return;
    let active = true;

    setLoadingOptions(true);
    setError(null);
    setApproved(null);
    setRemoveApprovalOpen(false);
    setPreview(null);

    if (existingDiscounts?.coupon_code) {
      setSelectedPreset(existingDiscounts.coupon_code);
      setCheckValue('');
      setCheckMode('percent');
    } else if (existingDiscounts && existingDiscounts.additional_discount_percentage > 0) {
      setSelectedPreset(null);
      setCheckMode('percent');
      setCheckValue(String(existingDiscounts.additional_discount_percentage));
    } else if (existingDiscounts && existingDiscounts.discount_amount > 0) {
      setSelectedPreset(null);
      setCheckMode('amount');
      setCheckValue(String(existingDiscounts.discount_amount));
    } else {
      setSelectedPreset(null);
      setCheckMode('percent');
      setCheckValue('');
    }

    const initialLines: Record<string, LineDraft> = {};
    for (const ld of existingDiscounts?.line_discounts || []) {
      if (ld.automatic) continue;
      if (ld.discount_percentage > 0) {
        initialLines[ld.row] = { mode: 'percent', value: String(ld.discount_percentage) };
      } else if (ld.discount_amount > 0) {
        initialLines[ld.row] = { mode: 'amount', value: String(ld.discount_amount) };
      }
    }
    setLineDrafts(initialLines);
    setSelectedRow(activeItems[0]?.name ?? null);
    setReason(existingDiscounts?.reason || '');
    setNote(existingDiscounts?.note || '');
    setTab('check');

    listDiscountOptions(posProfile, invoice)
      .then((res) => {
        if (!active) return;
        setOptions(res);
      })
      .catch((err) => {
        if (!active) return;
        setError(parseFrappeError(err, t('errors.invalid_discount')));
      })
      .finally(() => {
        if (active) setLoadingOptions(false);
      });

    return () => {
      active = false;
    };
  }, [open, invoice, posProfile]);

  const currency = options?.currency || existingDiscounts?.currency || propCurrency || '';
  const precision = currencyPrecisionFor(currency, options?.precision ?? existingDiscounts?.precision);
  const allowOpen = options?.allow_discount_change ?? false;
  const formatMoney = (n: number) => formatCurrencyPrecision(n, currency, precision);

  const selectedReasonObj = useMemo(
    () => (options?.reasons || []).find((r) => r.code === reason || r.name === reason) || null,
    [options?.reasons, reason],
  );

  // Build normalized request arguments from current UI draft
  const parsedDraft = useMemo(() => {
    let discountPercent: number | null = null;
    let discountAmount: number | null = null;
    const couponCode: string | null = selectedPreset;

    if (!couponCode && allowOpen) {
      const trimmed = checkValue.trim();
      if (trimmed !== '') {
        const parsed = Number(trimmed);
        if (Number.isFinite(parsed) && parsed > 0) {
          if (checkMode === 'percent') discountPercent = parsed;
          else discountAmount = parsed;
        }
      }
    }

    const lineDiscounts: LineDiscountInput[] = [];
    if (allowOpen) {
      for (const [rowId, entry] of Object.entries(lineDrafts)) {
        const trimmed = entry.value.trim();
        if (!trimmed) continue;
        const parsed = Number(trimmed);
        if (!Number.isFinite(parsed) || parsed <= 0) continue;
        lineDiscounts.push(
          entry.mode === 'percent'
            ? { row: rowId, percent: parsed, amount: null }
            : { row: rowId, percent: null, amount: parsed },
        );
      }
    }

    const hasAnyPositive = Boolean(
      couponCode || discountPercent !== null || discountAmount !== null || lineDiscounts.length > 0,
    );

    return {
      discountPercent,
      discountAmount,
      couponCode,
      lineDiscounts,
      hasAnyPositive,
    };
  }, [selectedPreset, allowOpen, checkMode, checkValue, lineDrafts]);

  // Clear stored approval token immediately whenever ANY draft field changes
  const invalidateApproval = () => {
    setApproved(null);
    setRemoveApprovalOpen(false);
  };

  // Debounced live server preview
  useEffect(() => {
    if (!open || !options) return;
    if (!parsedDraft.hasAnyPositive) {
      setPreview(null);
      setError(null);
      return;
    }

    const seq = ++previewSeq.current;
    const timer = window.setTimeout(async () => {
      setPreviewLoading(true);
      setError(null);
      try {
        const res = await previewCheckDiscount({
          invoice,
          pos_profile: posProfile,
          discount_percent: parsedDraft.discountPercent,
          discount_amount: parsedDraft.discountAmount,
          coupon_code: parsedDraft.couponCode,
          line_discounts: parsedDraft.lineDiscounts.length > 0 ? parsedDraft.lineDiscounts : undefined,
          discount_reason: reason || undefined,
          discount_note: note.trim() || undefined,
        });
        if (seq !== previewSeq.current) return;
        setPreview(res);
      } catch (err) {
        if (seq !== previewSeq.current) return;
        setPreview(null);
        setError(parseFrappeError(err, t('errors.invalid_discount')));
      } finally {
        if (seq === previewSeq.current) setPreviewLoading(false);
      }
    }, 100);

    return () => window.clearTimeout(timer);
  }, [
    open,
    options,
    invoice,
    posProfile,
    parsedDraft.discountPercent,
    parsedDraft.discountAmount,
    parsedDraft.couponCode,
    parsedDraft.lineDiscounts,
    parsedDraft.hasAnyPositive,
    reason,
    note,
  ]);

  // Active keypad target (Check open value OR selected Line item value)
  const activeMode: EntryMode =
    tab === 'check'
      ? checkMode
      : (selectedRow && lineDrafts[selectedRow]?.mode) || 'percent';
  const activeValue: string =
    tab === 'check'
      ? checkValue
      : (selectedRow && lineDrafts[selectedRow]?.value) || '';

  const setActiveValue = (next: string) => {
    invalidateApproval();
    // Sanitize numeric string
    const cleaned = next.replace(/[^\d.]/g, '');
    const parts = cleaned.split('.');
    let normalized = parts.length > 2 ? `${parts[0]}.${parts.slice(1).join('')}` : cleaned;
    if (activeMode === 'amount' && precision === 0) {
      normalized = normalized.replace(/\./g, '');
    }
    if (tab === 'check') {
      setSelectedPreset(null);
      setCheckValue(normalized);
    } else if (selectedRow) {
      setLineDrafts((prev) => ({
        ...prev,
        [selectedRow]: { mode: activeMode, value: normalized },
      }));
    }
  };

  const setActiveMode = (mode: EntryMode) => {
    invalidateApproval();
    if (tab === 'check') {
      setSelectedPreset(null);
      setCheckMode(mode);
      setCheckValue('');
    } else if (selectedRow) {
      setLineDrafts((prev) => ({
        ...prev,
        [selectedRow]: { mode, value: '' },
      }));
    }
  };

  const handleKeypadPress = (key: string) => {
    if (key === 'C') {
      setActiveValue('');
      return;
    }
    if (key === 'BACK') {
      setActiveValue(activeValue.slice(0, -1));
      return;
    }
    if (key === '.') {
      if (activeMode === 'amount' && precision === 0) return;
      if (activeValue.includes('.')) return;
      setActiveValue(activeValue === '' ? '0.' : `${activeValue}.`);
      return;
    }
    setActiveValue(`${activeValue}${key}`);
  };

  const handleSelectPreset = (presetName: string) => {
    invalidateApproval();
    if (selectedPreset === presetName) {
      setSelectedPreset(null);
      return;
    }
    const preset = (options?.presets || []).find((p) => p.name === presetName);
    setSelectedPreset(presetName);
    setCheckValue('');
    if (preset?.discount_reason) {
      setReason(preset.discount_reason);
    }
  };

  const hasExistingDiscount = Boolean(
    existingDiscounts &&
      (existingDiscounts.discount_amount > 0 ||
        existingDiscounts.additional_discount_percentage > 0 ||
        existingDiscounts.coupon_code ||
        existingDiscounts.line_discounts.some((d) => !d.automatic)),
  );

  const noteRequired = Boolean(selectedReasonObj?.requires_note === 1 || preview?.note_required);
  const noteMissing = noteRequired && !note.trim();
  const reasonMissing = !reason;
  const needsApproval = Boolean(preview?.needs_approval);
  const canApply =
    !loadingOptions &&
    !previewLoading &&
    !submitting &&
    parsedDraft.hasAnyPositive &&
    !error &&
    Boolean(preview) &&
    !reasonMissing &&
    !noteMissing &&
    (!needsApproval || Boolean(approved?.token));

  const approvalSummaryLines = useMemo(() => {
    if (!preview) return [];
    const lines: string[] = [];
    if (preview.is_comp || preview.approval_reasons.includes('comp')) {
      lines.push(t('discount.needs_approval_comp'));
    }
    if (preview.approval_reasons.includes('over_limit')) {
      lines.push(
        t('discount.needs_approval_over_limit', {
          limit: preview.limit_percent,
          effective: preview.effective_percent,
        }),
      );
    }
    if (preview.approval_reasons.includes('reason_requires_supervisor')) {
      lines.push(t('discount.needs_approval_reason'));
    }
    lines.push(
      `${t('discount.before')}: ${formatMoney(preview.before_total)} → ${t('discount.after')}: ${formatMoney(preview.after_total)}`,
    );
    return lines;
  }, [preview, currency, precision]);

  const handleApply = async () => {
    if (!canApply) return;
    setSubmitting(true);
    setError(null);
    try {
      const saved = await applyCheckDiscount({
        invoice,
        pos_profile: posProfile,
        discount_percent: parsedDraft.discountPercent,
        discount_amount: parsedDraft.discountAmount,
        coupon_code: parsedDraft.couponCode,
        line_discounts: parsedDraft.lineDiscounts.length > 0 ? parsedDraft.lineDiscounts : undefined,
        discount_reason: reason,
        discount_note: note.trim() || undefined,
        approval: approved?.token ?? undefined,
      });
      showToast.success(t('discount.applied_toast'));
      await onApplied(saved);
      onClose();
    } catch (err) {
      setApproved(null);
      setError(parseFrappeError(err, t('errors.invalid_discount')));
    } finally {
      setSubmitting(false);
    }
  };

  const executeRemove = async (approvalToken?: string) => {
    setRemoving(true);
    setError(null);
    try {
      const cleared = await removeCheckDiscount({
        invoice,
        pos_profile: posProfile,
        approval: approvalToken ?? undefined,
      });
      showToast.success(t('discount.removed_toast'));
      setRemoveApprovalOpen(false);
      await onApplied(cleared);
      onClose();
    } catch (err) {
      setError(parseFrappeError(err, t('errors.invalid_discount')));
    } finally {
      setRemoving(false);
    }
  };

  const handleRemoveClick = () => {
    const canRemoveAlone = options?.can_remove_discount ?? existingDiscounts?.can_remove_discount ?? true;
    if (!canRemoveAlone) {
      setRemoveApprovalOpen(true);
      return;
    }
    executeRemove();
  };

  const showDecimalKey = activeMode === 'percent' || precision > 0;

  if (!open) return null;

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent
        variant="xlarge"
        showCloseButton={false}
        className="fixed bottom-0 start-0 end-0 sm:static bg-white w-full max-w-4xl max-h-[92vh] rounded-t-2xl sm:rounded-xl flex flex-col p-0 overflow-hidden"
      >
        {/* Top Header */}
        <div className="flex items-center justify-between gap-3 border-b border-gray-200 px-5 py-4">
          <div className="flex flex-wrap items-center gap-2 min-w-0">
            <Percent className="h-5 w-5 text-gray-700 shrink-0" />
            <h2 className="text-lg font-bold text-gray-900">{t('discount.title')}</h2>
            <span className="rounded-md bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600">
              {invoice}
            </span>
            {options && (
              <span className="rounded-md bg-amber-50 border border-amber-200 px-2 py-0.5 text-xs font-medium text-amber-800">
                {options.limit_percent > 0
                  ? t('discount.your_limit', { limit: options.limit_percent })
                  : t('discount.manager_required_zero')}
              </span>
            )}
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={onClose}
            aria-label={t('common.cancel')}
            className="min-h-11 min-w-11"
          >
            <X className="h-5 w-5" />
          </Button>
        </div>

        {/* Body: 1-col on phone, 2-col on tablet/desktop */}
        <div className="flex-1 overflow-y-auto grid grid-cols-1 md:grid-cols-2 divide-y md:divide-y-0 md:divide-x divide-gray-200">
          {/* Start Column: Tabs, Presets / Line Picker, Open Keypad */}
          <div className="p-5 space-y-4">
            {/* Check vs Line Tabs */}
            <div role="tablist" className="grid grid-cols-2 gap-1 rounded-lg bg-gray-100 p-1">
              <button
                type="button"
                role="tab"
                aria-selected={tab === 'check'}
                onClick={() => setTab('check')}
                className={`min-h-11 rounded-md px-3 text-sm font-semibold transition-colors ${
                  tab === 'check' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-600 hover:text-gray-900'
                }`}
              >
                {t('discount.tab_check')}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === 'line'}
                onClick={() => setTab('line')}
                className={`min-h-11 rounded-md px-3 text-sm font-semibold transition-colors ${
                  tab === 'line' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-600 hover:text-gray-900'
                }`}
              >
                {t('discount.tab_line')}
                {parsedDraft.lineDiscounts.length > 0 && (
                  <span className="ms-1.5 inline-flex items-center justify-center rounded-full bg-blue-600 px-1.5 py-0.5 text-[11px] font-bold text-white">
                    {parsedDraft.lineDiscounts.length}
                  </span>
                )}
              </button>
            </div>

            {loadingOptions ? (
              <p className="py-6 text-center text-sm text-gray-500">{t('common.loading')}</p>
            ) : tab === 'check' ? (
              <div className="space-y-4">
                {/* Coupon Presets */}
                {options && options.presets.length > 0 && (
                  <div className="space-y-2">
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 flex items-center gap-1.5">
                      <Tag className="h-3.5 w-3.5" />
                      {t('discount.presets')}
                    </h3>
                    <div className="grid grid-cols-2 gap-2">
                      {options.presets.map((preset) => {
                        const isSelected = selectedPreset === preset.name;
                        const badgeText =
                          preset.discount_percentage > 0
                            ? `${preset.discount_percentage}%`
                            : formatMoney(preset.discount_amount);
                        return (
                          <button
                            key={preset.name}
                            type="button"
                            aria-pressed={isSelected}
                            onClick={() => handleSelectPreset(preset.name)}
                            className={`min-h-12 flex flex-col items-start justify-center rounded-lg border px-3 py-2 text-start transition-colors ${
                              isSelected
                                ? 'border-blue-600 bg-blue-50 text-blue-900 ring-1 ring-blue-600'
                                : 'border-gray-200 bg-white text-gray-900 hover:bg-gray-50'
                            }`}
                          >
                            <span className="text-sm font-semibold leading-tight">{preset.title}</span>
                            <span className="text-xs text-gray-500 tabular-nums">
                              {badgeText} · {preset.coupon_code}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}

                {/* Open Amount / Percent Keypad — ONLY when POS Profile allow_discount_change is on */}
                {allowOpen ? (
                  <div className="space-y-3">
                    <div className="flex items-center justify-between gap-2">
                      <label htmlFor="discount-open-input" className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                        {t('discount.open_entry')}
                      </label>
                      <div className="inline-flex rounded-lg bg-gray-100 p-0.5">
                        <button
                          type="button"
                          aria-pressed={checkMode === 'percent'}
                          onClick={() => setActiveMode('percent')}
                          className={`min-h-9 rounded-md px-3 text-xs font-semibold ${
                            checkMode === 'percent' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-600'
                          }`}
                        >
                          {t('discount.percent_off')}
                        </button>
                        <button
                          type="button"
                          aria-pressed={checkMode === 'amount'}
                          onClick={() => setActiveMode('amount')}
                          className={`min-h-9 rounded-md px-3 text-xs font-semibold ${
                            checkMode === 'amount' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-600'
                          }`}
                        >
                          {t('discount.amount_off', { currency: currency || '' })}
                        </button>
                      </div>
                    </div>

                    <input
                      id="discount-open-input"
                      aria-label={t('discount.open_entry')}
                      type="text"
                      inputMode={showDecimalKey ? 'decimal' : 'numeric'}
                      value={checkValue}
                      onChange={(e) => setActiveValue(e.target.value)}
                      placeholder={checkMode === 'percent' ? '0%' : formatMoney(0)}
                      className="min-h-12 w-full rounded-lg border border-gray-300 bg-white px-4 text-end text-xl font-bold tabular-nums text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />

                    {checkMode === 'percent' && (
                      <div className="flex flex-wrap gap-1.5">
                        {QUICK_PERCENTS.map((pct) => (
                          <button
                            key={pct}
                            type="button"
                            onClick={() => setActiveValue(String(pct))}
                            className={`min-h-10 rounded-lg border px-3 text-xs font-semibold tabular-nums ${
                              checkValue === String(pct) && !selectedPreset
                                ? 'border-blue-600 bg-blue-50 text-blue-900'
                                : 'border-gray-200 bg-gray-50 text-gray-700 hover:bg-gray-100'
                            }`}
                          >
                            {pct === 100 ? t('discount.comp_100') : `${pct}%`}
                          </button>
                        ))}
                      </div>
                    )}

                    {/* 48px Touch Keypad */}
                    <div className="grid grid-cols-3 gap-2" data-testid="discount-open-keypad">
                      {['1', '2', '3', '4', '5', '6', '7', '8', '9'].map((digit) => (
                        <button
                          key={digit}
                          type="button"
                          onClick={() => handleKeypadPress(digit)}
                          className="min-h-12 rounded-lg border border-gray-200 bg-white text-lg font-semibold text-gray-900 hover:bg-gray-50 active:bg-gray-100"
                        >
                          {digit}
                        </button>
                      ))}
                      {showDecimalKey ? (
                        <button
                          type="button"
                          onClick={() => handleKeypadPress('.')}
                          className="min-h-12 rounded-lg border border-gray-200 bg-white text-lg font-semibold text-gray-900 hover:bg-gray-50"
                        >
                          .
                        </button>
                      ) : (
                        <button
                          type="button"
                          onClick={() => handleKeypadPress('C')}
                          className="min-h-12 rounded-lg border border-gray-200 bg-gray-50 text-sm font-semibold text-gray-700 hover:bg-gray-100"
                        >
                          {t('common.clear')}
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => handleKeypadPress('0')}
                        className="min-h-12 rounded-lg border border-gray-200 bg-white text-lg font-semibold text-gray-900 hover:bg-gray-50 active:bg-gray-100"
                      >
                        0
                      </button>
                      <button
                        type="button"
                        aria-label="Backspace"
                        onClick={() => handleKeypadPress('BACK')}
                        className="flex min-h-12 items-center justify-center rounded-lg border border-gray-200 bg-gray-50 text-gray-700 hover:bg-gray-100"
                      >
                        <Delete className="h-5 w-5" />
                      </button>
                    </div>
                  </div>
                ) : (
                  <p className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2.5 text-xs text-gray-600">
                    {t('discount.presets_only_notice')}
                  </p>
                )}
              </div>
            ) : (
              /* Line Tab */
              <div className="space-y-3">
                {!allowOpen ? (
                  <p className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2.5 text-xs text-gray-600">
                    {t('discount.line_open_disabled')}
                  </p>
                ) : (
                  <>
                    <div className="max-h-44 overflow-y-auto space-y-1.5 pe-1">
                      {activeItems.map((item) => {
                        const isSel = selectedRow === item.name;
                        const draft = lineDrafts[item.name];
                        const hasDraft = Boolean(draft?.value && Number(draft.value) > 0);
                        return (
                          <button
                            key={item.name}
                            type="button"
                            onClick={() => setSelectedRow(item.name)}
                            className={`w-full min-h-12 flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-start ${
                              isSel
                                ? 'border-blue-600 bg-blue-50 ring-1 ring-blue-600'
                                : 'border-gray-200 bg-white hover:bg-gray-50'
                            }`}
                          >
                            <div className="min-w-0 flex-1">
                              <div className="flex items-center gap-1.5">
                                <span className="truncate text-sm font-medium text-gray-900">{item.item_name}</span>
                                {item.max_discount && item.max_discount > 0 ? (
                                  <span className="shrink-0 rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-800">
                                    {t('discount.item_max_cap', { max: item.max_discount })}
                                  </span>
                                ) : null}
                              </div>
                              <p className="text-xs text-gray-500 tabular-nums">
                                {item.qty} × {formatMoney(item.price_list_rate || item.rate)}
                              </p>
                            </div>
                            <div className="shrink-0 text-end">
                              {hasDraft && (
                                <span className="rounded-md bg-emerald-100 px-2 py-0.5 text-xs font-semibold text-emerald-800 tabular-nums">
                                  -{draft.mode === 'percent' ? `${draft.value}%` : formatMoney(Number(draft.value))}
                                </span>
                              )}
                            </div>
                          </button>
                        );
                      })}
                    </div>

                    {selectedRow && (
                      <div className="space-y-3 border-t border-gray-200 pt-3">
                        <div className="flex items-center justify-between gap-2">
                          <label htmlFor="line-discount-input" className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                            {t('discount.open_entry')}
                          </label>
                          <div className="inline-flex rounded-lg bg-gray-100 p-0.5">
                            <button
                              type="button"
                              aria-pressed={activeMode === 'percent'}
                              onClick={() => setActiveMode('percent')}
                              className={`min-h-9 rounded-md px-3 text-xs font-semibold ${
                                activeMode === 'percent' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-600'
                              }`}
                            >
                              {t('discount.percent_off')}
                            </button>
                            <button
                              type="button"
                              aria-pressed={activeMode === 'amount'}
                              onClick={() => setActiveMode('amount')}
                              className={`min-h-9 rounded-md px-3 text-xs font-semibold ${
                                activeMode === 'amount' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-600'
                              }`}
                            >
                              {t('discount.amount_off', { currency: currency || '' })}
                            </button>
                          </div>
                        </div>

                        <input
                          id="line-discount-input"
                          aria-label={t('discount.open_entry')}
                          type="text"
                          inputMode={showDecimalKey ? 'decimal' : 'numeric'}
                          value={activeValue}
                          onChange={(e) => setActiveValue(e.target.value)}
                          placeholder={activeMode === 'percent' ? '0%' : formatMoney(0)}
                          className="min-h-12 w-full rounded-lg border border-gray-300 bg-white px-4 text-end text-xl font-bold tabular-nums text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500"
                        />

                        <div className="grid grid-cols-3 gap-2" data-testid="discount-open-keypad">
                          {['1', '2', '3', '4', '5', '6', '7', '8', '9'].map((digit) => (
                            <button
                              key={digit}
                              type="button"
                              onClick={() => handleKeypadPress(digit)}
                              className="min-h-12 rounded-lg border border-gray-200 bg-white text-lg font-semibold text-gray-900 hover:bg-gray-50 active:bg-gray-100"
                            >
                              {digit}
                            </button>
                          ))}
                          {showDecimalKey ? (
                            <button
                              type="button"
                              onClick={() => handleKeypadPress('.')}
                              className="min-h-12 rounded-lg border border-gray-200 bg-white text-lg font-semibold text-gray-900 hover:bg-gray-50"
                            >
                              .
                            </button>
                          ) : (
                            <button
                              type="button"
                              onClick={() => handleKeypadPress('C')}
                              className="min-h-12 rounded-lg border border-gray-200 bg-gray-50 text-sm font-semibold text-gray-700 hover:bg-gray-100"
                            >
                              {t('common.clear')}
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() => handleKeypadPress('0')}
                            className="min-h-12 rounded-lg border border-gray-200 bg-white text-lg font-semibold text-gray-900 hover:bg-gray-50 active:bg-gray-100"
                          >
                            0
                          </button>
                          <button
                            type="button"
                            aria-label="Backspace"
                            onClick={() => handleKeypadPress('BACK')}
                            className="flex min-h-12 items-center justify-center rounded-lg border border-gray-200 bg-gray-50 text-gray-700 hover:bg-gray-100"
                          >
                            <Delete className="h-5 w-5" />
                          </button>
                        </div>
                      </div>
                    )}
                  </>
                )}
              </div>
            )}
          </div>

          {/* End Column: Reason, Note, Live Preview, Inline ManagerApproval, Apply / Remove */}
          <div className="p-5 flex flex-col justify-between gap-4 bg-gray-50/40">
            <div className="space-y-4">
              {/* Reason & Note */}
              <div className="space-y-3">
                <div className="space-y-1">
                  <label htmlFor="discount-reason-select" className="block text-xs font-semibold uppercase tracking-wide text-gray-600">
                    {t('discount.reason_label')}
                  </label>
                  <select
                    id="discount-reason-select"
                    aria-label={t('discount.reason_label')}
                    required
                    value={reason}
                    onChange={(e) => {
                      invalidateApproval();
                      setReason(e.target.value);
                    }}
                    className="min-h-11 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500"
                  >
                    <option value="">{t('discount.reason_placeholder')}</option>
                    {(options?.reasons || []).map((r) => (
                      <option key={r.code} value={r.code}>
                        {r.description || r.code}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="space-y-1">
                  <label htmlFor="discount-note-input" className="block text-xs font-semibold uppercase tracking-wide text-gray-600">
                    {t('discount.note_label')}
                  </label>
                  <input
                    id="discount-note-input"
                    aria-label={t('discount.note_label')}
                    type="text"
                    maxLength={140}
                    required={noteRequired}
                    value={note}
                    onChange={(e) => {
                      invalidateApproval();
                      setNote(e.target.value);
                    }}
                    placeholder={
                      noteRequired
                        ? t('discount.note_placeholder_required')
                        : t('discount.note_placeholder_optional')
                    }
                    className="min-h-11 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                </div>
              </div>

              {/* Error alert */}
              {error && (
                <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
                  {error}
                </div>
              )}

              {/* Live Server Preview Card */}
              <div className="rounded-xl border border-gray-200 bg-white p-4 space-y-2">
                <div className="flex items-center justify-between">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                    {t('discount.preview_title')}
                  </h3>
                  {previewLoading && (
                    <span className="text-xs text-gray-400">{t('common.loading')}</span>
                  )}
                </div>
                {preview ? (
                  <div className="space-y-1.5 text-sm tabular-nums">
                    <div className="flex justify-between text-gray-600">
                      <span>{t('discount.before')}</span>
                      <span data-testid="preview-before">{formatMoney(preview.before_total)}</span>
                    </div>
                    <div className="flex justify-between font-medium text-emerald-700">
                      <span>
                        {t('discount.discount_line')}
                        {preview.effective_percent > 0 ? ` (${preview.effective_percent}%)` : ''}
                      </span>
                      <span data-testid="preview-discount">−{formatMoney(preview.discount_total)}</span>
                    </div>
                    <div className="flex justify-between text-gray-600">
                      <span>{t('discount.tax')}</span>
                      <span data-testid="preview-tax">{formatMoney(preview.tax_total)}</span>
                    </div>
                    <div className="border-t border-gray-200 pt-2 flex justify-between text-base font-bold text-gray-900">
                      <span>{t('discount.after')}</span>
                      <span data-testid="preview-after">{formatMoney(preview.after_total)}</span>
                    </div>
                  </div>
                ) : (
                  <p className="text-xs text-gray-400">
                    {t('discount.preview_empty')}
                  </p>
                )}
              </div>

              {/* Inline ManagerApproval ONLY when preview says approval is needed */}
              {needsApproval && preview && !approved && !removeApprovalOpen && (
                <DiscountManagerApproval
                  open={true}
                  posProfile={posProfile}
                  privilege={preview.privilege}
                  title={t('discount.manager_approval_title')}
                  summaryLines={approvalSummaryLines}
                  reference={{ doctype: 'POS Invoice', name: invoice }}
                  claim={preview.claim as unknown as Record<string, unknown>}
                  percent={preview.effective_percent}
                  amount={preview.discount_total}
                  onApproved={(res) => setApproved(res)}
                  onCancel={() => setApproved(null)}
                />
              )}

              {approved && (
                <div className="flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm font-medium text-emerald-800">
                  <CheckCircle2 className="h-4 w-4 shrink-0" />
                  <span>{t('discount.approved_by', { name: approved.approverName })}</span>
                </div>
              )}

              {/* Inline ManagerApproval for discount_remove when required */}
              {removeApprovalOpen && existingDiscounts && (
                <DiscountManagerApproval
                  open={true}
                  posProfile={posProfile}
                  privilege="discount_remove"
                  title={t('discount.remove_approval_title')}
                  summaryLines={[
                    `${t('discount.discount_line')}: −${formatMoney(existingDiscounts.discount_amount)}`,
                  ]}
                  reference={{ doctype: 'POS Invoice', name: invoice }}
                  claim={existingDiscounts.remove_claim || { action: 'remove', row: null }}
                  onApproved={({ token }) => executeRemove(token)}
                  onCancel={() => setRemoveApprovalOpen(false)}
                />
              )}
            </div>

            {/* Footer Action Buttons */}
            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-gray-200 pt-4">
              {hasExistingDiscount ? (
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-11 gap-1.5 border-rose-200 text-rose-700 hover:bg-rose-50"
                  onClick={handleRemoveClick}
                  disabled={removing || submitting}
                >
                  <Trash2 className="h-4 w-4" />
                  {removing ? t('discount.removing') : t('discount.remove')}
                </Button>
              ) : (
                <div />
              )}

              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-11"
                  onClick={onClose}
                  disabled={submitting || removing}
                >
                  {t('common.cancel')}
                </Button>
                <Button
                  type="button"
                  className="min-h-11 min-w-36"
                  onClick={handleApply}
                  disabled={!canApply}
                >
                  {submitting ? t('discount.applying') : t('discount.apply')}
                </Button>
              </div>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
};

export default DiscountPanel;
