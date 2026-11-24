import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Delete, ShieldCheck, X } from 'lucide-react';
import { Button, Spinner, Textarea, cn } from '@ury/ui';
import { t } from '../i18n';
import {
  authoriseOverride,
  extractAuthErrorCode,
  getPrivilegePolicy,
  listApprovers,
  listReasons,
  overrideErrorMessage,
  type OverrideApprover,
  type OverrideReason,
} from '../lib/override-api';

const MIN_PIN_LENGTH = 4;
const MAX_PIN_LENGTH = 8;
const MIN_NOTE_LENGTH = 4;
const DEFAULT_IDLE_TIMEOUT_MS = 60_000;
const FOCUSABLE_SELECTOR =
  'button:not([disabled]), select:not([disabled]), textarea:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])';

export interface ManagerApprovalResult {
  token: string;
  approverName: string;
  approver?: string;
  reason?: string;
  note?: string;
}

export interface ManagerApprovalProps {
  open: boolean;
  posProfile: string;
  privilege: string;
  title?: string;
  summaryLines: string[];
  reference: { doctype: string; name: string } | string;
  claim: Record<string, unknown>;
  percent?: number;
  amount?: number;
  privilegeGroup?: number;
  requireReason?: boolean;
  idleTimeoutMs?: number;
  onApproved: (result: ManagerApprovalResult) => void;
  onCancel: () => void;
}

function normalizeReference(ref: ManagerApprovalProps['reference']): { doctype: string; name: string } {
  if (typeof ref === 'string') {
    return { doctype: 'POS Invoice', name: ref };
  }
  return {
    doctype: ref?.doctype || 'POS Invoice',
    name: ref?.name || '',
  };
}

export default function ManagerApproval({
  open,
  posProfile,
  privilege,
  title,
  summaryLines,
  reference,
  claim,
  percent,
  amount,
  privilegeGroup,
  requireReason,
  idleTimeoutMs = DEFAULT_IDLE_TIMEOUT_MS,
  onApproved,
  onCancel,
}: ManagerApprovalProps) {
  const refObj = useMemo(() => normalizeReference(reference), [reference]);
  const presetReason = typeof claim?.reason === 'string' ? claim.reason.trim() : '';

  const [approvers, setApprovers] = useState<OverrideApprover[]>([]);
  const [selectedApprover, setSelectedApprover] = useState('');
  const [policyRequiresReason, setPolicyRequiresReason] = useState(false);
  const [reasons, setReasons] = useState<OverrideReason[]>([]);
  const [selectedReasonCode, setSelectedReasonCode] = useState('');
  const [reasonNote, setReasonNote] = useState('');
  const [pin, setPin] = useState('');
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shake, setShake] = useState(false);

  const onCancelRef = useRef(onCancel);
  onCancelRef.current = onCancel;

  const dialogRef = useRef<HTMLDivElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const idleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showReasonPicker = useMemo(() => {
    if (requireReason === true) return true;
    if (requireReason === false) return false;
    return policyRequiresReason && !presetReason;
  }, [requireReason, policyRequiresReason, presetReason]);

  const selectedReasonObj = useMemo(
    () => reasons.find((r) => r.code === selectedReasonCode) ?? null,
    [reasons, selectedReasonCode],
  );

  const needsNote = Boolean(selectedReasonObj && Number(selectedReasonObj.requires_note) === 1);

  const resolvedReasonText = useMemo(() => {
    if (!showReasonPicker) return presetReason;
    if (!selectedReasonObj) return '';
    const trimmedNote = reasonNote.trim();
    if (needsNote) {
      if (selectedReasonObj.code === 'OTHER') {
        return trimmedNote ? `Other: ${trimmedNote}` : '';
      }
      return trimmedNote ? `${selectedReasonObj.description}: ${trimmedNote}` : '';
    }
    return selectedReasonObj.description || selectedReasonObj.code;
  }, [showReasonPicker, presetReason, selectedReasonObj, needsNote, reasonNote]);

  const isReasonValid = useMemo(() => {
    if (!showReasonPicker) return true;
    if (!selectedReasonObj) return false;
    if (needsNote) return reasonNote.trim().length >= MIN_NOTE_LENGTH;
    return true;
  }, [showReasonPicker, selectedReasonObj, needsNote, reasonNote]);

  const handleClose = useCallback(() => {
    setPin('');
    setError(null);
    onCancelRef.current();
  }, []);

  const resetIdleTimer = useCallback(() => {
    if (idleTimerRef.current) {
      clearTimeout(idleTimerRef.current);
      idleTimerRef.current = null;
    }
    if (!open || idleTimeoutMs <= 0) return;
    idleTimerRef.current = setTimeout(() => {
      setPin('');
      setError(null);
      onCancelRef.current();
    }, idleTimeoutMs);
  }, [open, idleTimeoutMs]);

  // Capture & restore focus around dialog open/close
  useEffect(() => {
    if (!open) return;
    previouslyFocusedRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    return () => {
      const prev = previouslyFocusedRef.current;
      previouslyFocusedRef.current = null;
      if (prev && typeof prev.focus === 'function' && document.contains(prev)) {
        prev.focus();
      }
    };
  }, [open]);

  // Focus first control inside dialog once approvers finish loading
  useEffect(() => {
    if (!open || loading) return;
    const container = dialogRef.current;
    if (!container) return;
    if (!container.contains(document.activeElement)) {
      const firstControl =
        container.querySelector<HTMLElement>('#manager-approval-approver') ??
        container.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
      firstControl?.focus();
    }
  }, [open, loading]);

  // Reset state & load eligible approvers + policy/reasons on open
  useEffect(() => {
    if (!open) {
      setPin('');
      setError(null);
      setSubmitting(false);
      setShake(false);
      if (idleTimerRef.current) {
        clearTimeout(idleTimerRef.current);
        idleTimerRef.current = null;
      }
      return;
    }

    let cancelled = false;
    setPin('');
    setError(null);
    setSubmitting(false);
    setSelectedApprover('');
    setSelectedReasonCode('');
    setReasonNote('');
    setApprovers([]);
    setReasons([]);
    setPolicyRequiresReason(false);
    setLoading(true);

    Promise.all([
      listApprovers({
        posProfile,
        privilege,
        referenceName: refObj.name || undefined,
        percent,
        amount,
        privilegeGroup,
      }),
      getPrivilegePolicy({
        posProfile,
        privilege,
        percent,
        amount,
        privilegeGroup,
      }).catch(() => null),
    ])
      .then(async ([approverRows, policy]) => {
        if (cancelled) return;
        setApprovers(approverRows);
        if (approverRows.length === 1) {
          setSelectedApprover(approverRows[0].user);
        }
        const mustAskReason =
          requireReason === true || (requireReason !== false && Boolean(policy?.requires_reason) && !presetReason);
        setPolicyRequiresReason(Boolean(policy?.requires_reason));
        if (mustAskReason) {
          const reasonRows = await listReasons(privilege).catch(() => []);
          if (!cancelled) setReasons(reasonRows);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(overrideErrorMessage(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [open, posProfile, privilege, refObj.name, percent, amount, privilegeGroup, requireReason, presetReason]);

  // Idle timer lifecycle
  useEffect(() => {
    if (!open) return;
    resetIdleTimer();
    return () => {
      if (idleTimerRef.current) {
        clearTimeout(idleTimerRef.current);
        idleTimerRef.current = null;
      }
    };
  }, [open, resetIdleTimer]);

  const canSubmit =
    !loading &&
    !submitting &&
    approvers.length > 0 &&
    Boolean(selectedApprover) &&
    pin.length >= MIN_PIN_LENGTH &&
    pin.length <= MAX_PIN_LENGTH &&
    isReasonValid;

  const handleSubmit = useCallback(async () => {
    if (!canSubmit) return;
    resetIdleTimer();
    setSubmitting(true);
    setError(null);
    setShake(false);

    const submittedPin = pin;
    const finalClaim: Record<string, unknown> = { ...claim };
    if (showReasonPicker && resolvedReasonText) {
      finalClaim.reason = resolvedReasonText;
    }

    try {
      const result = await authoriseOverride({
        posProfile,
        privilege,
        approver: selectedApprover,
        pin: submittedPin,
        referenceDoctype: refObj.doctype,
        referenceName: refObj.name,
        claim: finalClaim,
      });
      setPin('');
      onApproved({
        token: result.token,
        approverName: result.approver_name,
        approver: result.approver,
        reason: resolvedReasonText || undefined,
        note: needsNote ? reasonNote.trim() : undefined,
      });
    } catch (err) {
      const code = extractAuthErrorCode(err);
      setError(overrideErrorMessage(err));
      if (code === 'POS-AUTH-PIN' || code === 'POS-AUTH-LOCKED' || code === 'POS-AUTH-NO-PIN') {
        setPin('');
        setShake(true);
        setTimeout(() => setShake(false), 350);
      }
    } finally {
      setSubmitting(false);
    }
  }, [
    canSubmit,
    resetIdleTimer,
    pin,
    claim,
    showReasonPicker,
    resolvedReasonText,
    posProfile,
    privilege,
    selectedApprover,
    refObj.doctype,
    refObj.name,
    onApproved,
    needsNote,
    reasonNote,
  ]);

  const pushDigit = useCallback(
    (digit: string) => {
      resetIdleTimer();
      setError(null);
      setPin((prev) => (prev.length < MAX_PIN_LENGTH ? prev + digit : prev));
    },
    [resetIdleTimer],
  );

  const handleBackspace = useCallback(() => {
    resetIdleTimer();
    setPin((prev) => prev.slice(0, -1));
  }, [resetIdleTimer]);

  const handleClear = useCallback(() => {
    resetIdleTimer();
    setError(null);
    setPin('');
  }, [resetIdleTimer]);

  // Hardware keyboard support (0-9, Backspace, Enter, Escape) + accessible Tab focus trap
  useEffect(() => {
    if (!open) return;

    const onKeyDown = (event: KeyboardEvent) => {
      resetIdleTimer();
      if (event.key === 'Tab') {
        const container = dialogRef.current;
        if (container) {
          const focusables = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
          if (focusables.length > 0) {
            const first = focusables[0];
            const last = focusables[focusables.length - 1];
            const active = document.activeElement as HTMLElement | null;
            if (event.shiftKey) {
              if (!active || active === first || !container.contains(active)) {
                event.preventDefault();
                last.focus();
              }
            } else if (!active || active === last || !container.contains(active)) {
              event.preventDefault();
              first.focus();
            }
          }
        }
        return;
      }

      if (event.key === 'Escape') {
        event.preventDefault();
        if (!submitting) handleClose();
        return;
      }

      const target = event.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (tag === 'textarea' || tag === 'input') {
        return;
      }

      if (/^[0-9]$/.test(event.key)) {
        event.preventDefault();
        pushDigit(event.key);
      } else if (event.key === 'Backspace') {
        event.preventDefault();
        handleBackspace();
      } else if (event.key === 'Enter' && tag !== 'button') {
        event.preventDefault();
        void handleSubmit();
      }
    };

    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, submitting, resetIdleTimer, handleClose, pushDigit, handleBackspace, handleSubmit]);

  if (!open) return null;

  const dots = '•'.repeat(pin.length);
  const keypadDisabled = loading || submitting || approvers.length === 0;
  const pinLiveLabel = t('manager_approval.pin_entered_count', { count: pin.length });

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby="manager-approval-title"
      className="fixed inset-0 z-50 flex items-end justify-center sm:items-center"
      onPointerDown={resetIdleTimer}
    >
      {/* Scrim */}
      <div
        className="fixed inset-0 bg-black/50 backdrop-blur-sm"
        onClick={() => {
          if (!submitting) handleClose();
        }}
        aria-hidden="true"
      />

      {/* Responsive surface: bottom sheet (<640px), centered max-w-md (tablet portrait 768x1024),
          wide two-column lg:max-w-2xl on landscape tills (1280x720) so all 4 keypad rows fit without scrolling */}
      <div
        className="relative z-10 flex max-h-[94vh] w-full flex-col overflow-hidden rounded-t-2xl bg-white text-gray-900 shadow-xl sm:mx-4 sm:max-w-md sm:rounded-xl lg:max-w-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Sticky header */}
        <div className="flex items-start justify-between gap-3 border-b border-gray-200 px-5 py-3">
          <div className="flex min-w-0 items-start gap-2.5">
            <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-amber-100 text-amber-800">
              <ShieldCheck className="h-4 w-4" aria-hidden="true" />
            </div>
            <div className="min-w-0">
              <h2 id="manager-approval-title" className="text-base font-semibold leading-tight text-gray-900">
                {title || t('manager_approval.title')}
              </h2>
              <p className="mt-0.5 text-xs text-gray-600">{t('manager_approval.subtitle')}</p>
            </div>
          </div>
          <button
            type="button"
            onClick={handleClose}
            disabled={submitting}
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-gray-500 hover:bg-gray-100 hover:text-gray-800 disabled:opacity-50"
          >
            <span className="sr-only">{t('common.dismiss')}</span>
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        {/* Scrollable body (fits without scroll at 768x1024 and 1280x720) */}
        <div className="flex-1 overflow-y-auto px-5 py-3">
          {loading ? (
            <div className="space-y-3">
              {summaryLines.length > 0 && (
                <div className="space-y-0.5 rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-start">
                  {summaryLines.map((line, index) => (
                    <p
                      key={`${index}-${line}`}
                      className={cn(
                        index === 0 ? 'text-sm font-semibold text-amber-950' : 'text-xs text-amber-900',
                      )}
                    >
                      {line}
                    </p>
                  ))}
                </div>
              )}
              <div className="py-6">
                <Spinner message={t('common.loading')} />
              </div>
            </div>
          ) : approvers.length === 0 ? (
            <div className="space-y-3">
              {summaryLines.length > 0 && (
                <div className="space-y-0.5 rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-start">
                  {summaryLines.map((line, index) => (
                    <p
                      key={`${index}-${line}`}
                      className={cn(
                        index === 0 ? 'text-sm font-semibold text-amber-950' : 'text-xs text-amber-900',
                      )}
                    >
                      {line}
                    </p>
                  ))}
                </div>
              )}
              <p
                role="alert"
                className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2.5 text-sm text-rose-700"
              >
                {t('manager_approval.no_approvers')}
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2 lg:items-start lg:gap-5">
              {/* Left column: action context, manager selector, reason & error banner */}
              <div className="space-y-3">
                {summaryLines.length > 0 && (
                  <div className="space-y-0.5 rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-start">
                    {summaryLines.map((line, index) => (
                      <p
                        key={`${index}-${line}`}
                        className={cn(
                          index === 0 ? 'text-sm font-semibold text-amber-950' : 'text-xs text-amber-900',
                        )}
                      >
                        {line}
                      </p>
                    ))}
                  </div>
                )}

                {/* Approver selector */}
                <div className="space-y-1.5">
                  <label
                    htmlFor="manager-approval-approver"
                    className="block text-xs font-medium text-gray-700"
                  >
                    {t('manager_approval.manager')}
                  </label>
                  <select
                    id="manager-approval-approver"
                    aria-label={t('manager_approval.manager')}
                    value={selectedApprover}
                    disabled={submitting}
                    onChange={(e) => {
                      resetIdleTimer();
                      setSelectedApprover(e.target.value);
                      setError(null);
                    }}
                    className="min-h-10 w-full rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
                  >
                    <option value="">{t('manager_approval.select_manager')}</option>
                    {approvers.map((mgr) => (
                      <option key={mgr.user} value={mgr.user}>
                        {mgr.full_name}
                      </option>
                    ))}
                  </select>
                  {approvers.length >= 2 && approvers.length <= 4 && (
                    <div className="flex flex-wrap gap-1.5">
                      {approvers.map((mgr) => {
                        const isSelected = selectedApprover === mgr.user;
                        return (
                          <button
                            key={mgr.user}
                            type="button"
                            aria-pressed={isSelected}
                            disabled={submitting}
                            onClick={() => {
                              resetIdleTimer();
                              setSelectedApprover(mgr.user);
                              setError(null);
                            }}
                            className={cn(
                              'min-h-9 rounded-full border px-3 py-1 text-xs font-medium transition-colors',
                              isSelected
                                ? 'border-blue-600 bg-blue-50 text-blue-800'
                                : 'border-gray-300 bg-white text-gray-700 hover:bg-gray-50',
                            )}
                          >
                            {mgr.full_name}
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>

                {/* Reason picker (when privilege requires a reason) */}
                {showReasonPicker && (
                  <div className="space-y-1.5">
                    <label
                      htmlFor="manager-approval-reason"
                      className="block text-xs font-medium text-gray-700"
                    >
                      {t('manager_approval.reason')}
                    </label>
                    <select
                      id="manager-approval-reason"
                      aria-label={t('manager_approval.reason')}
                      value={selectedReasonCode}
                      disabled={submitting}
                      onChange={(e) => {
                        resetIdleTimer();
                        setSelectedReasonCode(e.target.value);
                        setReasonNote('');
                        setError(null);
                      }}
                      className="min-h-10 w-full rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
                    >
                      <option value="">{t('manager_approval.select_reason')}</option>
                      {reasons.map((r) => (
                        <option key={r.code} value={r.code}>
                          {r.description}
                        </option>
                      ))}
                    </select>
                    {needsNote && (
                      <div className="space-y-1">
                        <label
                          htmlFor="manager-approval-note"
                          className="block text-xs font-medium text-gray-700"
                        >
                          {t('manager_approval.note')}
                        </label>
                        <Textarea
                          id="manager-approval-note"
                          aria-label={t('manager_approval.note')}
                          value={reasonNote}
                          disabled={submitting}
                          placeholder={t('manager_approval.note_placeholder')}
                          onChange={(e) => {
                            resetIdleTimer();
                            setReasonNote(e.target.value);
                          }}
                          rows={2}
                        />
                      </div>
                    )}
                  </div>
                )}

                {/* Error banner */}
                {error && (
                  <p
                    role="alert"
                    className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs font-medium text-rose-700"
                  >
                    {error}
                  </p>
                )}
              </div>

              {/* Right column: masked PIN display & 3x4 touch PIN keypad (>=48px keys) */}
              <div className="space-y-2.5">
                <div className="space-y-1">
                  <div className="flex items-center justify-between text-xs text-gray-600">
                    <span>{t('manager_approval.pin_label')}</span>
                    <span>{t('manager_approval.pin_hint')}</span>
                  </div>
                  <div
                    data-testid="manager-pin-display"
                    role="status"
                    aria-live="polite"
                    aria-label={pinLiveLabel}
                    dir="ltr"
                    className={cn(
                      'flex min-h-12 items-center justify-center rounded-lg border border-gray-300 bg-gray-50 px-4 text-2xl tracking-[0.45em] text-gray-900',
                      shake && 'animate-pulse border-rose-500 bg-rose-50',
                    )}
                  >
                    {dots}
                  </div>
                </div>

                {/* 3x4 touch PIN keypad (min-h-12 = 48px, h-[52px] on sm+) */}
                <div dir="ltr" className="grid grid-cols-3 gap-2">
                  {['1', '2', '3', '4', '5', '6', '7', '8', '9'].map((digit) => (
                    <button
                      key={digit}
                      type="button"
                      disabled={keypadDisabled}
                      onClick={() => pushDigit(digit)}
                      className="min-h-12 h-12 rounded-lg border border-gray-200 bg-gray-50 text-lg font-semibold text-gray-900 transition-colors hover:bg-gray-100 active:bg-gray-200 disabled:opacity-50 sm:h-[52px]"
                    >
                      {digit}
                    </button>
                  ))}
                  <button
                    type="button"
                    disabled={keypadDisabled}
                    onClick={handleClear}
                    className="min-h-12 h-12 rounded-lg border border-gray-200 bg-gray-100 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-200 active:bg-gray-300 disabled:opacity-50 sm:h-[52px]"
                  >
                    {t('floor_pin.clear')}
                  </button>
                  <button
                    type="button"
                    disabled={keypadDisabled}
                    onClick={() => pushDigit('0')}
                    className="min-h-12 h-12 rounded-lg border border-gray-200 bg-gray-50 text-lg font-semibold text-gray-900 transition-colors hover:bg-gray-100 active:bg-gray-200 disabled:opacity-50 sm:h-[52px]"
                  >
                    0
                  </button>
                  <button
                    type="button"
                    disabled={keypadDisabled}
                    onClick={handleBackspace}
                    className="flex min-h-12 h-12 items-center justify-center rounded-lg border border-gray-200 bg-gray-100 text-gray-700 transition-colors hover:bg-gray-200 active:bg-gray-300 disabled:opacity-50 sm:h-[52px]"
                  >
                    <span className="sr-only">{t('floor_pin.delete')}</span>
                    <Delete className="h-5 w-5" aria-hidden="true" />
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Footer actions */}
        <div className="flex flex-col-reverse gap-2 border-t border-gray-200 bg-gray-50 px-5 py-2.5 sm:flex-row sm:justify-end">
          <Button
            type="button"
            variant="outline"
            className="min-h-11 sm:w-auto"
            onClick={handleClose}
            disabled={submitting}
          >
            {t('common.cancel')}
          </Button>
          <Button
            type="button"
            className="min-h-11 sm:w-auto"
            onClick={() => void handleSubmit()}
            disabled={!canSubmit}
          >
            {submitting ? t('manager_approval.approving') : t('manager_approval.approve')}
          </Button>
        </div>
      </div>
    </div>
  );
}
