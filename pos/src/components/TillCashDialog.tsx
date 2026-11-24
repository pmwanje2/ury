import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle,
  ArrowDownToLine,
  ArrowUpFromLine,
  Check,
  FileBarChart2,
  KeyRound,
  PlusCircle,
  Receipt,
  RefreshCw,
  RotateCcw,
  Trash2,
} from 'lucide-react';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  Input,
  Select,
  SelectItem,
  Spinner,
  showToast,
} from '@ury/ui';
import { formatCurrency, parseFrappeError } from '@ury/core';
import { t } from '../i18n';
import { usePOSStore } from '../store/pos-store';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';
import { isAuthRequiredError } from '../lib/override-api';
import {
  approveTillCashMovement,
  discardTillCashMovement,
  getTillCashState,
  recordTillCashMovement,
  type CashMovementType,
  type TillCashState,
} from '../lib/till-cash-api';
import {
  getShiftXReport,
  getTillDrawerStatus,
  listClosedTills,
  loadActivePosReasonForClaim,
  logCashPullException,
  recordCashMovementWithControl,
  reopenTill,
  triggerNoSale,
  type ClosedTillRow,
  type ShiftXReport,
  type TillDrawerStatus,
} from '../lib/till-controls-api';

interface TillCashDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

type TillMenuTab = CashMovementType | 'No Sale' | 'Reopen Till' | 'X-Report';

const MENU_TABS: TillMenuTab[] = [
  'Pickup',
  'Loan',
  'Paid Out',
  'Paid In',
  'No Sale',
  'Reopen Till',
  'X-Report',
];

type PendingApprovalKind =
  | {
      kind: 'cash_movement';
      movementType: CashMovementType;
      amount: number;
      reason: string;
      reference: string;
      target: 'expense' | 'tips';
      claimReason: string;
      needsPicker: boolean;
    }
  | { kind: 'threshold_exception'; reason: string; claimReason: string; needsPicker: boolean }
  | { kind: 'no_sale'; reason: string; note: string; claimReason: string; needsPicker: boolean }
  | {
      kind: 'reopen_till';
      closingName: string;
      openingName: string;
      reason: string;
      claimReason: string;
      needsPicker: boolean;
    };

const TillCashDialog = ({ open, onOpenChange }: TillCashDialogProps) => {
  const { posProfile } = usePOSStore();
  const posProfileName = posProfile?.name || '';

  const [state, setState] = useState<TillCashState | null>(null);
  const [drawerStatus, setDrawerStatus] = useState<TillDrawerStatus | null>(null);
  const [closedTills, setClosedTills] = useState<ClosedTillRow[]>([]);
  const [selectedClosing, setSelectedClosing] = useState<string>('');
  const [xReport, setXReport] = useState<ShiftXReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [busyRow, setBusyRow] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [activeTab, setActiveTab] = useState<TillMenuTab>('Pickup');
  const [amountInput, setAmountInput] = useState('');
  const [reason, setReason] = useState('');
  const [reference, setReference] = useState('');
  const [paidOutTarget, setPaidOutTarget] = useState<'expense' | 'tips'>('expense');
  const [tipServer, setTipServer] = useState('');

  const [showExceptionForm, setShowExceptionForm] = useState(false);
  const [exceptionReason, setExceptionReason] = useState('');

  const [noSaleReason, setNoSaleReason] = useState('');
  const [noSaleNote, setNoSaleNote] = useState('');

  const [reopenReason, setReopenReason] = useState('');

  const [pendingApproval, setPendingApproval] = useState<PendingApprovalKind | null>(null);

  const loadState = useCallback(async () => {
    if (!posProfileName) return;
    setLoading(true);
    setError(null);
    try {
      const next = await getTillCashState(posProfileName);
      const pullThreshold = Number(
        next.cash_pull_threshold ?? posProfile?.cash_pull_threshold ?? 0,
      );
      const status =
        next.opening_entry && pullThreshold > 0
          ? await getTillDrawerStatus(posProfileName)
          : null;
      setState(next);
      setDrawerStatus(status);
      if (
        next.paid_out_targets.length > 0 &&
        !next.paid_out_targets.some((item) => item.kind === paidOutTarget)
      ) {
        setPaidOutTarget(next.paid_out_targets[0].kind);
      }
    } catch (err) {
      setError(parseFrappeError(err, t('till_cash.load_failed')));
    } finally {
      setLoading(false);
    }
  }, [posProfileName, paidOutTarget]);

  const loadClosedTills = useCallback(async () => {
    if (!posProfileName) return;
    const rows = await listClosedTills(posProfileName);
    setClosedTills(rows);
    if (rows.length > 0 && !rows.some((r) => r.name === selectedClosing)) {
      setSelectedClosing(rows[0].name);
    }
  }, [posProfileName, selectedClosing]);

  const loadXReport = useCallback(async () => {
    if (!posProfileName) return;
    const report = await getShiftXReport(posProfileName, state?.opening_entry);
    setXReport(report);
  }, [posProfileName, state?.opening_entry]);

  useEffect(() => {
    if (!open) return;
    void loadState();
  }, [open, loadState]);

  useEffect(() => {
    if (!open || !posProfileName) return;
    if (activeTab === 'Reopen Till') {
      void loadClosedTills();
    } else if (activeTab === 'X-Report') {
      void loadXReport();
    }
  }, [open, posProfileName, activeTab, loadClosedTills, loadXReport]);

  const isMovementTab =
    activeTab === 'Pickup' ||
    activeTab === 'Loan' ||
    activeTab === 'Paid Out' ||
    activeTab === 'Paid In';
  const movementType: CashMovementType = isMovementTab ? activeTab : 'Pickup';
  const parsedAmount = parseFloat(amountInput);
  const amountValid = Number.isFinite(parsedAmount) && parsedAmount > 0;
  const isPaidOut = activeTab === 'Paid Out';
  const isPaidIn = activeTab === 'Paid In';
  const paidOutThreshold: number | null =
    state !== null
      ? state.paid_out_threshold
      : (drawerStatus?.paid_out_threshold ?? posProfile?.paid_out_approval_threshold ?? null);
  const paidInThreshold = Number(
    state?.paid_in_threshold ?? drawerStatus?.paid_in_threshold ?? posProfile?.paid_in_approval_threshold ?? 0,
  );
  const needsSecondPerson =
    isPaidOut && amountValid && paidOutThreshold !== null && parsedAmount > paidOutThreshold;
  const paidInAboveThreshold =
    isPaidIn && amountValid && state !== null && parsedAmount > paidInThreshold;
  const requiresInPlacePin =
    ((needsSecondPerson || paidInAboveThreshold) && !state?.can_approve);
  const typeAvailable = isMovementTab
    ? state
      ? Boolean(state.available_types[movementType] ?? (movementType === 'Paid In' ? posProfile?.paid_in_enabled : false))
      : true
    : true;
  const needsReasonField = isPaidOut || isPaidIn;
  const canSubmitMovement =
    Boolean(state?.enabled && state?.opening_entry) &&
    typeAvailable &&
    amountValid &&
    (!needsReasonField || reason.trim().length > 0) &&
    (!requiresInPlacePin || reference.trim().length > 0) &&
    !submitting;

  const openCashMovementApproval = async (
    mType: CashMovementType,
    amt: number,
    rawReason: string,
    rawRef: string,
    target: 'expense' | 'tips',
  ) => {
    const privilege =
      mType === 'Paid Out' || mType === 'Paid In'
        ? 'paid_out'
        : mType === 'Loan'
          ? 'cash_loan'
          : 'cash_pickup';
    const resolved = await loadActivePosReasonForClaim(privilege, rawReason);
    setPendingApproval({
      kind: 'cash_movement',
      movementType: mType,
      amount: amt,
      reason: rawReason,
      reference: rawRef,
      target,
      claimReason: resolved.claimReason,
      needsPicker: resolved.needsPicker,
    });
  };

  const submitCashMovement = async (approvalToken?: string, approvedReason?: string) => {
    if (!posProfileName || !state?.opening_entry) return;
    setSubmitting(true);
    setError(null);
    const effectiveReason = (approvedReason ?? reason).trim();
    try {
      if (approvalToken || isPaidIn) {
        const created = await recordCashMovementWithControl({
          pos_profile: posProfileName,
          movement_type: movementType,
          amount: parsedAmount,
          reason: effectiveReason || undefined,
          reference: reference.trim() || undefined,
          ...(isPaidOut
            ? {
                paid_out_target: paidOutTarget,
                ...(paidOutTarget === 'tips' && tipServer ? { tip_server: tipServer } : {}),
              }
            : {}),
          pos_opening_entry: state.opening_entry,
          approval_token: approvalToken,
        });
        setAmountInput('');
        setReason('');
        setReference('');
        setTipServer('');
        if (created.docstatus === 0) {
          showToast.success(t('till_cash.saved_pending'));
        } else {
          showToast.success(t('till_cash.recorded_success', { type: movementType }));
        }
      } else {
        const created = await recordTillCashMovement({
          pos_profile: posProfileName,
          movement_type: movementType,
          amount: parsedAmount,
          ...(isPaidOut
            ? {
                reason: effectiveReason,
                paid_out_target: paidOutTarget,
                ...(paidOutTarget === 'tips' && tipServer ? { tip_server: tipServer } : {}),
              }
            : {}),
          ...(reference.trim() ? { reference: reference.trim() } : {}),
        });
        setAmountInput('');
        setReason('');
        setReference('');
        setTipServer('');
        if (created.docstatus === 0) {
          showToast.success(t('till_cash.saved_pending'));
        } else {
          showToast.success(t('till_cash.recorded_success', { type: movementType }));
        }
      }
      await loadState();
    } catch (err) {
      if (!approvalToken && isAuthRequiredError(err)) {
        await openCashMovementApproval(
          movementType,
          parsedAmount,
          reason.trim(),
          reference.trim(),
          paidOutTarget,
        );
      } else {
        setError(parseFrappeError(err, t('till_cash.record_failed')));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleRecord = () => {
    if (!posProfileName || !canSubmitMovement) return;
    if (requiresInPlacePin) {
      void openCashMovementApproval(
        movementType,
        parsedAmount,
        reason.trim(),
        reference.trim(),
        paidOutTarget,
      );
      return;
    }
    void submitCashMovement();
  };

  const submitNoSale = async (approvalToken?: string, approvedReason?: string) => {
    const effectiveReason = (approvedReason ?? noSaleReason).trim();
    if (!posProfileName || !effectiveReason) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await triggerNoSale({
        pos_profile: posProfileName,
        reason: effectiveReason,
        note: noSaleNote.trim() || undefined,
        pos_opening_entry: state?.opening_entry || undefined,
        approval_token: approvalToken,
      });
      if (typeof window !== 'undefined' && res.drawer_kick_hex) {
        window.dispatchEvent(
          new CustomEvent('pos:drawer-kick', { detail: { hex: res.drawer_kick_hex } }),
        );
      }
      setNoSaleReason('');
      setNoSaleNote('');
      showToast.success(
        t('till_cash.no_sale_success', { count: res.no_sale_count_for_shift }),
      );
      await loadState();
    } catch (err) {
      if (!approvalToken && isAuthRequiredError(err)) {
        const resolved = await loadActivePosReasonForClaim('no_sale', noSaleReason.trim());
        setPendingApproval({
          kind: 'no_sale',
          reason: noSaleReason.trim(),
          note: noSaleNote.trim(),
          claimReason: resolved.claimReason,
          needsPicker: resolved.needsPicker,
        });
      } else {
        setError(parseFrappeError(err, t('till_cash.record_failed')));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleTriggerNoSale = () => {
    if (!noSaleReason.trim() || !state?.opening_entry) return;
    if (!state.can_approve) {
      void loadActivePosReasonForClaim('no_sale', noSaleReason.trim()).then((resolved) => {
        setPendingApproval({
          kind: 'no_sale',
          reason: noSaleReason.trim(),
          note: noSaleNote.trim(),
          claimReason: resolved.claimReason,
          needsPicker: resolved.needsPicker,
        });
      });
      return;
    }
    void submitNoSale();
  };

  const handleRequestExceptionApproval = () => {
    if (!exceptionReason.trim() || !state?.opening_entry) return;
    void loadActivePosReasonForClaim('cash_pickup', exceptionReason.trim()).then((resolved) => {
      setPendingApproval({
        kind: 'threshold_exception',
        reason: exceptionReason.trim(),
        claimReason: resolved.claimReason,
        needsPicker: resolved.needsPicker,
      });
    });
  };

  const handleRequestReopenApproval = () => {
    const row = closedTills.find((c) => c.name === selectedClosing);
    if (!row || !reopenReason.trim()) return;
    void loadActivePosReasonForClaim('reopen_till', reopenReason.trim()).then((resolved) => {
      setPendingApproval({
        kind: 'reopen_till',
        closingName: row.name,
        openingName: row.pos_opening_entry,
        reason: reopenReason.trim(),
        claimReason: resolved.claimReason,
        needsPicker: resolved.needsPicker,
      });
    });
  };

  const handleManagerApproved = async (result: ManagerApprovalResult) => {
    const action = pendingApproval;
    setPendingApproval(null);
    if (!action || !posProfileName) return;

    const effectiveReason = result.reason || action.claimReason || action.reason;
    if (action.kind === 'cash_movement') {
      await submitCashMovement(result.token, effectiveReason);
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      if (action.kind === 'threshold_exception') {
        await logCashPullException({
          pos_profile: posProfileName,
          reason: effectiveReason,
          pos_opening_entry: state?.opening_entry || undefined,
          approval_token: result.token,
        });
        setExceptionReason('');
        setShowExceptionForm(false);
        showToast.success(t('till_cash.exception_logged'));
        await loadState();
      } else if (action.kind === 'no_sale') {
        await submitNoSale(result.token, effectiveReason);
      } else if (action.kind === 'reopen_till') {
        await reopenTill({
          pos_closing_entry: action.closingName,
          reason: effectiveReason,
          approval_token: result.token,
        });
        setReopenReason('');
        showToast.success(t('till_cash.reopen_success', { closing: action.closingName }));
        await Promise.all([loadState(), loadClosedTills()]);
      }
    } catch (err) {
      setError(parseFrappeError(err, t('till_cash.record_failed')));
    } finally {
      setSubmitting(false);
    }
  };

  const handleApprove = async (name: string) => {
    setBusyRow(name);
    setError(null);
    try {
      await approveTillCashMovement(name);
      showToast.success(t('till_cash.approved_success'));
      await loadState();
    } catch (err) {
      setError(parseFrappeError(err, t('till_cash.approve_failed')));
    } finally {
      setBusyRow(null);
    }
  };

  const handleDiscard = async (name: string) => {
    setBusyRow(name);
    setError(null);
    try {
      await discardTillCashMovement(name);
      showToast.success(t('till_cash.discarded_success'));
      await loadState();
    } catch (err) {
      setError(parseFrappeError(err, t('till_cash.discard_failed')));
    } finally {
      setBusyRow(null);
    }
  };

  const recordButtonLabel =
    movementType === 'Pickup'
      ? t('till_cash.record_pickup')
      : movementType === 'Loan'
        ? t('till_cash.record_loan')
        : movementType === 'Paid In'
          ? requiresInPlacePin
            ? t('till_cash.approve_record_paid_in')
            : t('till_cash.record_paid_in')
          : requiresInPlacePin
            ? t('till_cash.approve_record_paid_out')
            : t('till_cash.record_paid_out');

  const approvalConfig = (() => {
    if (!pendingApproval || !posProfileName) return null;
    if (pendingApproval.kind === 'cash_movement') {
      const actionKey =
        pendingApproval.movementType === 'Paid Out'
          ? 'paid_out'
          : pendingApproval.movementType === 'Paid In'
            ? 'paid_in'
            : pendingApproval.movementType === 'Loan'
              ? 'cash_loan'
              : 'cash_pickup';
      const privilege =
        pendingApproval.movementType === 'Paid Out' || pendingApproval.movementType === 'Paid In'
          ? 'paid_out'
          : pendingApproval.movementType === 'Loan'
            ? 'cash_loan'
            : 'cash_pickup';
      return {
        privilege,
        reference: { doctype: 'POS Opening Entry', name: state?.opening_entry || '' },
        amount: pendingApproval.amount,
        requireReason: pendingApproval.needsPicker ? true : undefined,
        summaryLines: [
          `${pendingApproval.movementType}: ${formatCurrency(pendingApproval.amount, state?.currency || '')}`,
          ...(pendingApproval.claimReason || pendingApproval.reason
            ? [`${t('till_cash.reason')}: ${pendingApproval.claimReason || pendingApproval.reason}`]
            : []),
          ...(pendingApproval.reference ? [`${t('till_cash.reference')}: ${pendingApproval.reference}`] : []),
        ],
        claim: {
          action: actionKey,
          movement_type: pendingApproval.movementType,
          amount: pendingApproval.amount,
          reason: pendingApproval.claimReason,
          reference: pendingApproval.reference,
          paid_out_target: pendingApproval.target,
          pos_opening_entry: state?.opening_entry || '',
        },
      };
    }
    if (pendingApproval.kind === 'threshold_exception') {
      return {
        privilege: 'cash_pickup',
        reference: { doctype: 'POS Opening Entry', name: state?.opening_entry || '' },
        requireReason: pendingApproval.needsPicker ? true : undefined,
        summaryLines: [
          t('till_cash.log_exception'),
          `${t('till_cash.exception_reason')}: ${pendingApproval.claimReason || pendingApproval.reason}`,
        ],
        claim: {
          action: 'threshold_exception',
          pos_profile: posProfileName,
          pos_opening_entry: state?.opening_entry || '',
          threshold_amount: Number(drawerStatus?.cash_pull_threshold ?? state?.cash_pull_threshold ?? 0),
          reason: pendingApproval.claimReason,
        },
      };
    }
    if (pendingApproval.kind === 'no_sale') {
      return {
        privilege: 'no_sale',
        reference: { doctype: 'POS Opening Entry', name: state?.opening_entry || '' },
        requireReason: pendingApproval.needsPicker ? true : undefined,
        summaryLines: [
          t('till_cash.no_sale_heading'),
          `${t('till_cash.no_sale_reason')}: ${pendingApproval.claimReason || pendingApproval.reason}`,
        ],
        claim: {
          action: 'no_sale',
          pos_profile: posProfileName,
          pos_opening_entry: state?.opening_entry || '',
          reason: pendingApproval.claimReason,
          note: pendingApproval.note,
        },
      };
    }
    return {
      privilege: 'reopen_till',
      reference: { doctype: 'POS Closing Entry', name: pendingApproval.closingName },
      requireReason: pendingApproval.needsPicker ? true : undefined,
      summaryLines: [
        `${t('till_cash.reopen_till')}: ${pendingApproval.closingName}`,
        `${t('till_cash.reopen_reason')}: ${pendingApproval.claimReason || pendingApproval.reason}`,
      ],
      claim: {
        action: 'reopen_till',
        pos_profile: posProfileName,
        pos_closing_entry: pendingApproval.closingName,
        pos_opening_entry: pendingApproval.openingName,
        reason: pendingApproval.claimReason,
      },
    };
  })();

  const blindDrawer = Boolean(drawerStatus?.blind_close || Number(posProfile?.custom_blind_cash_count) === 1);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        variant="xlarge"
        size="4xl"
        onClose={() => onOpenChange(false)}
        className="max-h-[92vh] overflow-y-auto"
      >
        <DialogHeader>
          <DialogTitle>{t('till_cash.title')}</DialogTitle>
          <DialogDescription>{t('till_cash.description')}</DialogDescription>
        </DialogHeader>

        <div className="px-4 sm:px-6 pb-6 space-y-5">
          {error && (
            <div className="p-3 bg-red-50 border border-red-200 rounded-lg">
              <p className="text-sm text-red-700">{error}</p>
            </div>
          )}

          {loading && !state ? (
            <div className="flex items-center justify-center py-10">
              <Spinner message={t('till_cash.loading')} />
            </div>
          ) : state && !state.enabled ? (
            <p className="py-6 text-center text-sm text-gray-600">{t('till_cash.disabled')}</p>
          ) : state ? (
            <>
              {drawerStatus?.threshold_exceeded && (
                <div
                  role="alert"
                  className="rounded-lg border border-amber-300 bg-amber-50 p-3.5 space-y-3"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="flex items-start gap-2.5 text-amber-950">
                      <AlertTriangle className="w-5 h-5 shrink-0 text-amber-700 mt-0.5" />
                      <p className="text-sm font-medium">
                        {blindDrawer || drawerStatus.expected_drawer_cash === null
                          ? t('till_cash.cash_pull_alert_blind')
                          : t('till_cash.cash_pull_alert', {
                              expected: formatCurrency(drawerStatus.expected_drawer_cash, drawerStatus.currency),
                              suggested: formatCurrency(
                                drawerStatus.suggested_pickup_amount ?? 0,
                                drawerStatus.currency,
                              ),
                            })}
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {!blindDrawer &&
                        drawerStatus.suggested_pickup_amount !== null &&
                        drawerStatus.suggested_pickup_amount > 0 && (
                          <Button
                            type="button"
                            size="sm"
                            variant="default"
                            className="min-h-11"
                            onClick={() => {
                              setActiveTab('Pickup');
                              setAmountInput(String(drawerStatus.suggested_pickup_amount));
                              setShowExceptionForm(false);
                            }}
                          >
                            {t('till_cash.prefill_pickup')}
                          </Button>
                        )}
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        className="min-h-11"
                        onClick={() => setShowExceptionForm((prev) => !prev)}
                      >
                        {t('till_cash.log_exception')}
                      </Button>
                    </div>
                  </div>

                  {showExceptionForm && (
                    <div className="pt-2 border-t border-amber-200 flex flex-col sm:flex-row gap-2 sm:items-end">
                      <div className="flex-1 space-y-1">
                        <label
                          htmlFor="till-exception-reason"
                          className="text-xs font-medium text-amber-950"
                        >
                          {t('till_cash.exception_reason')}
                        </label>
                        <Input
                          id="till-exception-reason"
                          type="text"
                          value={exceptionReason}
                          onChange={(e) => setExceptionReason(e.target.value)}
                          placeholder={t('till_cash.exception_reason_placeholder')}
                          disabled={submitting}
                        />
                      </div>
                      <Button
                        type="button"
                        size="sm"
                        className="min-h-11"
                        disabled={!exceptionReason.trim() || submitting}
                        onClick={handleRequestExceptionApproval}
                      >
                        {t('till_cash.approve_exception')}
                      </Button>
                    </div>
                  )}
                </div>
              )}

              {/* Responsive Till Menu keys (phone 2-col, tablet 4-col, desktop 7-col) */}
              <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-2">
                {MENU_TABS.map((tab) => {
                  const selected = activeTab === tab;
                  const available =
                    tab === 'Pickup' || tab === 'Loan' || tab === 'Paid Out'
                      ? Boolean(state.available_types[tab]) && Boolean(state.opening_entry)
                      : tab === 'Paid In'
                        ? Boolean(state.available_types['Paid In'] ?? posProfile?.paid_in_enabled) &&
                          Boolean(state.opening_entry)
                        : tab === 'No Sale' || tab === 'X-Report'
                          ? Boolean(state.opening_entry)
                          : true;
                  const Icon =
                    tab === 'Pickup'
                      ? ArrowUpFromLine
                      : tab === 'Loan'
                        ? ArrowDownToLine
                        : tab === 'Paid Out'
                          ? Receipt
                          : tab === 'Paid In'
                            ? PlusCircle
                            : tab === 'No Sale'
                              ? KeyRound
                              : tab === 'Reopen Till'
                                ? RotateCcw
                                : FileBarChart2;
                  const label =
                    tab === 'Pickup'
                      ? t('till_cash.pickup')
                      : tab === 'Loan'
                        ? t('till_cash.loan')
                        : tab === 'Paid Out'
                          ? t('till_cash.paid_out')
                          : tab === 'Paid In'
                            ? t('till_cash.paid_in')
                            : tab === 'No Sale'
                              ? t('till_cash.no_sale')
                              : tab === 'Reopen Till'
                                ? t('till_cash.reopen_till')
                                : t('till_cash.x_report');
                  return (
                    <Button
                      key={tab}
                      type="button"
                      variant={selected ? 'default' : 'outline'}
                      disabled={!available || submitting}
                      onClick={() => {
                        setActiveTab(tab);
                        setError(null);
                      }}
                      className="min-h-14 h-auto py-2 px-2 flex flex-col items-center justify-center gap-1 text-center whitespace-normal"
                    >
                      <Icon className="w-4 h-4 shrink-0" />
                      <span className="text-xs sm:text-sm font-semibold leading-tight">{label}</span>
                    </Button>
                  );
                })}
              </div>

              {!state.opening_entry && activeTab !== 'Reopen Till' ? (
                <p className="py-6 text-center text-sm text-gray-600">
                  {t('till_cash.no_open_shift')}
                </p>
              ) : isMovementTab ? (
                <div className="rounded-lg border border-border p-4 space-y-4">
                  <div className="space-y-1.5">
                    <label htmlFor="till-cash-amount" className="text-sm font-medium text-gray-800">
                      {t('till_cash.amount')}
                    </label>
                    <Input
                      id="till-cash-amount"
                      type="number"
                      min="0"
                      step="0.01"
                      value={amountInput}
                      onChange={(e) => setAmountInput(e.target.value)}
                      placeholder={t('till_cash.amount_placeholder')}
                      disabled={submitting}
                    />
                  </div>

                  {isPaidOut && state.paid_out_targets.length > 0 && (
                    <div className="space-y-1.5">
                      <span className="text-sm font-medium text-gray-800">
                        {t('till_cash.paid_out_for')}
                      </span>
                      <div className="flex flex-wrap gap-2">
                        {state.paid_out_targets.map((target) => (
                          <Button
                            key={target.kind}
                            type="button"
                            size="sm"
                            className="min-h-11"
                            variant={paidOutTarget === target.kind ? 'default' : 'outline'}
                            disabled={submitting}
                            onClick={() => setPaidOutTarget(target.kind)}
                          >
                            {target.label}
                          </Button>
                        ))}
                      </div>
                    </div>
                  )}

                  {isPaidOut && paidOutTarget === 'tips' && (
                    <div className="space-y-1.5">
                      <label htmlFor="till-cash-tip-server" className="text-sm font-medium text-gray-800">
                        {t('till_cash.tip_server')}
                      </label>
                      <Select
                        id="till-cash-tip-server"
                        value={tipServer}
                        onChange={(e) => setTipServer(e.target.value)}
                        disabled={submitting}
                      >
                        <SelectItem value="">{t('till_cash.tip_server_placeholder')}</SelectItem>
                        {(state.servers || []).map((srv) => (
                          <SelectItem key={srv.user} value={srv.user}>
                            {srv.full_name ? `${srv.full_name} (${srv.user})` : srv.user}
                          </SelectItem>
                        ))}
                      </Select>
                    </div>
                  )}

                  {needsReasonField && (
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      <div className="space-y-1.5">
                        <label htmlFor="till-cash-reason" className="text-sm font-medium text-gray-800">
                          {t('till_cash.reason')}
                        </label>
                        <Input
                          id="till-cash-reason"
                          type="text"
                          value={reason}
                          onChange={(e) => setReason(e.target.value)}
                          placeholder={t('till_cash.reason_placeholder')}
                          disabled={submitting}
                        />
                      </div>
                      <div className="space-y-1.5">
                        <label htmlFor="till-cash-reference" className="text-sm font-medium text-gray-800">
                          {t('till_cash.reference')}
                        </label>
                        <Input
                          id="till-cash-reference"
                          type="text"
                          value={reference}
                          onChange={(e) => setReference(e.target.value)}
                          placeholder={t('till_cash.reference_placeholder')}
                          disabled={submitting}
                        />
                      </div>
                    </div>
                  )}

                  {needsSecondPerson && paidOutThreshold !== null && (
                    <p className="text-sm font-medium text-amber-700 bg-amber-50 border border-amber-200 rounded-md px-3 py-2">
                      {paidOutThreshold === 0
                        ? t('till_cash.requires_second_person_all')
                        : t('till_cash.requires_second_person', {
                            threshold: formatCurrency(paidOutThreshold, state.currency),
                          })}
                    </p>
                  )}

                  {paidInAboveThreshold && (
                    <p className="text-sm font-medium text-amber-700 bg-amber-50 border border-amber-200 rounded-md px-3 py-2">
                      {t('till_cash.paid_in_requires_approval', {
                        threshold: formatCurrency(paidInThreshold, state.currency),
                      })}
                    </p>
                  )}

                  <div className="flex justify-end">
                    <Button
                      type="button"
                      className="min-h-11 w-full sm:w-auto"
                      disabled={!canSubmitMovement}
                      onClick={handleRecord}
                    >
                      {submitting ? t('till_cash.recording') : recordButtonLabel}
                    </Button>
                  </div>
                </div>
              ) : activeTab === 'No Sale' ? (
                <div className="rounded-lg border border-border p-4 space-y-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <h3 className="text-base font-semibold text-gray-900">
                        {t('till_cash.no_sale_heading')}
                      </h3>
                      <p className="text-xs text-gray-600">{t('till_cash.no_sale_description')}</p>
                    </div>
                    {drawerStatus && (
                      <span className="text-xs font-medium px-2.5 py-1 rounded-full bg-gray-100 text-gray-800">
                        {t('till_cash.no_sale_count_label')}: {drawerStatus.no_sale_count}
                      </span>
                    )}
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div className="space-y-1.5">
                      <label htmlFor="till-no-sale-reason" className="text-sm font-medium text-gray-800">
                        {t('till_cash.no_sale_reason')}
                      </label>
                      <Input
                        id="till-no-sale-reason"
                        type="text"
                        value={noSaleReason}
                        onChange={(e) => setNoSaleReason(e.target.value)}
                        placeholder={t('till_cash.no_sale_reason_placeholder')}
                        disabled={submitting}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <label htmlFor="till-no-sale-note" className="text-sm font-medium text-gray-800">
                        {t('till_cash.no_sale_note')}
                      </label>
                      <Input
                        id="till-no-sale-note"
                        type="text"
                        value={noSaleNote}
                        onChange={(e) => setNoSaleNote(e.target.value)}
                        placeholder={t('till_cash.no_sale_note_placeholder')}
                        disabled={submitting}
                      />
                    </div>
                  </div>
                  <div className="flex justify-end">
                    <Button
                      type="button"
                      className="min-h-11 w-full sm:w-auto"
                      disabled={!noSaleReason.trim() || submitting}
                      onClick={handleTriggerNoSale}
                    >
                      {t('till_cash.open_cash_drawer')}
                    </Button>
                  </div>
                </div>
              ) : activeTab === 'Reopen Till' ? (
                <div className="rounded-lg border border-border p-4 space-y-4">
                  <div>
                    <h3 className="text-base font-semibold text-gray-900">
                      {t('till_cash.reopen_heading')}
                    </h3>
                    <p className="text-xs text-gray-600">{t('till_cash.reopen_description')}</p>
                  </div>

                  {closedTills.length === 0 ? (
                    <p className="text-sm text-gray-500 py-4 text-center border border-dashed border-border rounded-lg">
                      {t('till_cash.reopen_empty')}
                    </p>
                  ) : (
                    <>
                      <div className="space-y-2">
                        {closedTills.map((row) => (
                          <button
                            key={row.name}
                            type="button"
                            onClick={() => setSelectedClosing(row.name)}
                            className={`w-full text-start rounded-lg border p-3 transition-colors min-h-11 ${
                              selectedClosing === row.name
                                ? 'border-blue-600 bg-blue-50'
                                : 'border-border hover:bg-gray-50'
                            }`}
                          >
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <span className="font-semibold text-sm text-gray-900">{row.name}</span>
                              <span className="text-xs text-gray-600">
                                {row.period_end_date || row.posting_date}
                              </span>
                            </div>
                            <p className="text-xs text-gray-600 mt-0.5">
                              {row.user_name || row.user} · {row.pos_opening_entry}
                            </p>
                          </button>
                        ))}
                      </div>

                      <div className="space-y-1.5">
                        <label htmlFor="till-reopen-reason" className="text-sm font-medium text-gray-800">
                          {t('till_cash.reopen_reason')}
                        </label>
                        <Input
                          id="till-reopen-reason"
                          type="text"
                          value={reopenReason}
                          onChange={(e) => setReopenReason(e.target.value)}
                          placeholder={t('till_cash.reopen_reason_placeholder')}
                          disabled={submitting}
                        />
                      </div>

                      <div className="flex justify-end">
                        <Button
                          type="button"
                          className="min-h-11 w-full sm:w-auto"
                          disabled={!selectedClosing || !reopenReason.trim() || submitting}
                          onClick={handleRequestReopenApproval}
                        >
                          {t('till_cash.reopen_shift')}
                        </Button>
                      </div>
                    </>
                  )}
                </div>
              ) : (
                <div className="rounded-lg border border-border p-4 space-y-4">
                  <div className="flex items-center justify-between">
                    <h3 className="text-base font-semibold text-gray-900">
                      {t('till_cash.x_report_heading')}
                    </h3>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => void loadXReport()}
                    >
                      <RefreshCw className="w-4 h-4 me-1" />
                      {t('till_cash.refresh')}
                    </Button>
                  </div>

                  {xReport ? (
                    <>
                      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                        <div className="rounded-lg border border-border p-3">
                          <p className="text-xs text-gray-500">{t('till_cash.x_report_no_sales')}</p>
                          <p
                            data-testid="x-report-no-sale-count"
                            className="text-lg font-semibold text-gray-900"
                          >
                            {xReport.no_sale_count}
                          </p>
                        </div>
                        <div className="rounded-lg border border-border p-3">
                          <p className="text-xs text-gray-500">
                            {t('till_cash.x_report_exceptions')}
                          </p>
                          <p
                            data-testid="x-report-threshold-exceptions"
                            className="text-lg font-semibold text-gray-900"
                          >
                            {xReport.threshold_exception_count}
                          </p>
                        </div>
                        <div className="rounded-lg border border-border p-3">
                          <p className="text-xs text-gray-500">
                            {t('till_cash.net_drawer_movement')}
                          </p>
                          <p className="text-lg font-semibold text-gray-900">
                            {formatCurrency(xReport.net_drawer_movement, xReport.currency)}
                          </p>
                        </div>
                        <div className="rounded-lg border border-border p-3">
                          <p className="text-xs text-gray-500">
                            {t('till_cash.x_report_open_checks')}
                          </p>
                          <p className="text-lg font-semibold text-gray-900">
                            {xReport.open_checks_count}
                          </p>
                        </div>
                      </div>

                      {xReport.blind_close || xReport.expected_drawer_cash === null ? (
                        <p className="text-xs text-gray-600">{t('till_cash.x_report_blind_hidden')}</p>
                      ) : (
                        <div className="rounded-lg border border-border bg-gray-50 p-3 flex items-center justify-between">
                          <span className="text-sm font-medium text-gray-700">
                            {t('till_cash.x_report_expected_cash')}
                          </span>
                          <span className="text-base font-semibold text-gray-900">
                            {formatCurrency(xReport.expected_drawer_cash, xReport.currency)}
                          </span>
                        </div>
                      )}

                      {xReport.no_sale_events.length > 0 && (
                        <div className="space-y-1.5">
                          <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                            {t('till_cash.x_report_recent_no_sales')}
                          </h4>
                          <div className="divide-y divide-border rounded-lg border border-border text-xs">
                            {xReport.no_sale_events.map((ev) => (
                              <div key={ev.name} className="p-2.5 flex justify-between gap-2">
                                <span>{ev.reason || ev.name}</span>
                                <span className="text-gray-500">{ev.event_time}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </>
                  ) : (
                    <Spinner message={t('till_cash.loading')} />
                  )}
                </div>
              )}

              {state.opening_entry && (
                <div className="space-y-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <h3 className="text-base font-semibold text-gray-900">
                        {t('till_cash.shift_movements')}
                      </h3>
                      <p className="text-xs text-gray-500">
                        {t('till_cash.net_drawer_movement')}:{' '}
                        <span className="font-semibold text-gray-800">
                          {formatCurrency(state.net_drawer_movement, state.currency)}
                        </span>
                        {state.pending_count > 0 && (
                          <span className="ms-2 text-amber-700 font-medium">
                            · {t('till_cash.pending_badge', { count: state.pending_count })}
                          </span>
                        )}
                      </p>
                    </div>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={loading}
                      onClick={() => void loadState()}
                    >
                      <RefreshCw className="w-4 h-4 me-1" />
                      {t('till_cash.refresh')}
                    </Button>
                  </div>

                  {state.movements.length === 0 ? (
                    <p className="text-sm text-gray-500 py-4 text-center border border-dashed border-border rounded-lg">
                      {t('till_cash.empty')}
                    </p>
                  ) : (
                    <div className="divide-y divide-border rounded-lg border border-border">
                      {state.movements.map((row) => (
                        <div
                          key={row.name}
                          className="flex flex-wrap items-center justify-between gap-3 p-3 text-sm"
                        >
                          <div className="space-y-0.5">
                            <div className="flex items-center gap-2">
                              <span className="font-semibold text-gray-900">{row.movement_type}</span>
                              <span
                                className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                                  row.docstatus === 1
                                    ? 'bg-green-100 text-green-800'
                                    : 'bg-amber-100 text-amber-800'
                                }`}
                              >
                                {row.status}
                              </span>
                              <span className="text-xs text-gray-500">{row.name}</span>
                            </div>
                            {row.reason && <p className="text-gray-700">{row.reason}</p>}
                            <p className="text-xs text-gray-500">
                              {t('till_cash.recorded_by')}: {row.owner_name || row.owner}
                              {row.tip_server && (
                                <>
                                  {' '}
                                  · {t('till_cash.tip_server')}: {row.tip_server_name || row.tip_server}
                                </>
                              )}
                              {row.approver && (
                                <>
                                  {' '}
                                  · {t('till_cash.approved_by')}: {row.approver_name || row.approver}
                                </>
                              )}
                            </p>
                          </div>

                          <div className="flex items-center gap-2">
                            <span
                              className={`font-semibold ${
                                row.drawer_change >= 0 ? 'text-green-700' : 'text-gray-900'
                              }`}
                            >
                              {row.drawer_change >= 0 ? '+' : ''}
                              {formatCurrency(row.drawer_change, state.currency)}
                            </span>
                            {row.can_approve && (
                              <Button
                                type="button"
                                size="sm"
                                disabled={busyRow === row.name}
                                onClick={() => void handleApprove(row.name)}
                              >
                                <Check className="w-3.5 h-3.5 me-1" />
                                {t('till_cash.approve')}
                              </Button>
                            )}
                            {row.can_discard && (
                              <Button
                                type="button"
                                size="sm"
                                variant="outline"
                                disabled={busyRow === row.name}
                                onClick={() => void handleDiscard(row.name)}
                              >
                                <Trash2 className="w-3.5 h-3.5 me-1" />
                                {t('till_cash.discard')}
                              </Button>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </>
          ) : null}
        </div>
      </DialogContent>

      {approvalConfig && (
        <ManagerApproval
          open={Boolean(pendingApproval)}
          posProfile={posProfileName}
          privilege={approvalConfig.privilege}
          summaryLines={approvalConfig.summaryLines}
          reference={approvalConfig.reference}
          claim={approvalConfig.claim}
          amount={'amount' in approvalConfig ? approvalConfig.amount : undefined}
          requireReason={approvalConfig.requireReason}
          onApproved={(res) => void handleManagerApproved(res)}
          onCancel={() => setPendingApproval(null)}
        />
      )}
    </Dialog>
  );
};

export default TillCashDialog;
