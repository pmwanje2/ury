import { useCallback, useEffect, useState } from 'react';
import {
  ArrowDownToLine,
  ArrowUpFromLine,
  Check,
  Receipt,
  RefreshCw,
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
  Spinner,
  showToast,
} from '@ury/ui';
import { formatCurrency, parseFrappeError } from '@ury/core';
import { t } from '../i18n';
import { usePOSStore } from '../store/pos-store';
import {
  approveTillCashMovement,
  discardTillCashMovement,
  getTillCashState,
  recordTillCashMovement,
  type CashMovementType,
  type TillCashState,
} from '../lib/till-cash-api';

interface TillCashDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const MOVEMENT_TYPES: CashMovementType[] = ['Pickup', 'Loan', 'Paid Out'];

const TillCashDialog = ({ open, onOpenChange }: TillCashDialogProps) => {
  const { posProfile } = usePOSStore();
  const posProfileName = posProfile?.name || '';

  const [state, setState] = useState<TillCashState | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [busyRow, setBusyRow] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [movementType, setMovementType] = useState<CashMovementType>('Pickup');
  const [amountInput, setAmountInput] = useState('');
  const [reason, setReason] = useState('');
  const [paidOutTarget, setPaidOutTarget] = useState<'expense' | 'tips'>('expense');

  const loadState = useCallback(async () => {
    if (!posProfileName) return;
    setLoading(true);
    setError(null);
    try {
      const next = await getTillCashState(posProfileName);
      setState(next);
      if (next.paid_out_targets.length > 0 && !next.paid_out_targets.some((item) => item.kind === paidOutTarget)) {
        setPaidOutTarget(next.paid_out_targets[0].kind);
      }
    } catch (err) {
      setError(parseFrappeError(err, t('till_cash.load_failed')));
    } finally {
      setLoading(false);
    }
  }, [posProfileName, paidOutTarget]);

  useEffect(() => {
    if (!open) return;
    void loadState();
  }, [open, loadState]);

  const parsedAmount = parseFloat(amountInput);
  const amountValid = Number.isFinite(parsedAmount) && parsedAmount > 0;
  const isPaidOut = movementType === 'Paid Out';
  const needsSecondPerson =
    isPaidOut && amountValid && state !== null && parsedAmount > state.paid_out_threshold;
  const typeAvailable = state ? Boolean(state.available_types[movementType]) : true;
  const canSubmit =
    Boolean(state?.enabled && state?.opening_entry) &&
    typeAvailable &&
    amountValid &&
    (!isPaidOut || reason.trim().length > 0) &&
    !submitting;

  const handleRecord = async () => {
    if (!posProfileName || !canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const created = await recordTillCashMovement({
        pos_profile: posProfileName,
        movement_type: movementType,
        amount: parsedAmount,
        ...(isPaidOut ? { reason: reason.trim(), paid_out_target: paidOutTarget } : {}),
      });
      setAmountInput('');
      setReason('');
      if (created.docstatus === 0) {
        showToast.success(t('till_cash.saved_pending'));
      } else {
        showToast.success(t('till_cash.recorded_success', { type: movementType }));
      }
      await loadState();
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
        : t('till_cash.record_paid_out');

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        variant="xlarge"
        size="4xl"
        onClose={() => onOpenChange(false)}
        className="max-h-[90vh] overflow-y-auto"
      >
        <DialogHeader>
          <DialogTitle>{t('till_cash.title')}</DialogTitle>
          <DialogDescription>{t('till_cash.description')}</DialogDescription>
        </DialogHeader>

        <div className="px-6 pb-6 space-y-6">
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
          ) : state && !state.opening_entry ? (
            <p className="py-6 text-center text-sm text-gray-600">{t('till_cash.no_open_shift')}</p>
          ) : state ? (
            <>
              <div className="grid grid-cols-3 gap-3">
                {MOVEMENT_TYPES.map((type) => {
                  const selected = movementType === type;
                  const available = Boolean(state.available_types[type]);
                  const Icon =
                    type === 'Pickup'
                      ? ArrowUpFromLine
                      : type === 'Loan'
                        ? ArrowDownToLine
                        : Receipt;
                  const label =
                    type === 'Pickup'
                      ? t('till_cash.pickup')
                      : type === 'Loan'
                        ? t('till_cash.loan')
                        : t('till_cash.paid_out');
                  return (
                    <Button
                      key={type}
                      type="button"
                      variant={selected ? 'default' : 'outline'}
                      disabled={!available || submitting}
                      onClick={() => {
                        setMovementType(type);
                        setError(null);
                      }}
                      className="h-14 flex flex-col items-center justify-center gap-1"
                    >
                      <Icon className="w-4 h-4" />
                      <span className="text-sm font-semibold">{label}</span>
                    </Button>
                  );
                })}
              </div>

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

                {isPaidOut && (
                  <>
                    {state.paid_out_targets.length > 0 && (
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

                    {needsSecondPerson && (
                      <p className="text-sm font-medium text-amber-700 bg-amber-50 border border-amber-200 rounded-md px-3 py-2">
                        {t('till_cash.requires_second_person', {
                          threshold: formatCurrency(state.paid_out_threshold, state.currency),
                        })}
                      </p>
                    )}
                  </>
                )}

                <div className="flex justify-end">
                  <Button type="button" disabled={!canSubmit} onClick={handleRecord}>
                    {submitting ? t('till_cash.recording') : recordButtonLabel}
                  </Button>
                </div>
              </div>

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
            </>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
};

export default TillCashDialog;
