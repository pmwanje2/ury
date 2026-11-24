/**
 * Temporary B2a contract component for inline manager PIN approval.
 *
 * Matches the exact ManagerApproval prop contract from pos-override-contract.md
 * and Matrix §2 visual spec (SupervisorSignOff + 48px touch PIN pad).
 * Deleted and replaced by B2a's `ManagerApproval.tsx` once `feat/pos-manager-authorisation` merges.
 */
import React, { useEffect, useState } from 'react';
import { ShieldAlert, Delete } from 'lucide-react';
import { call, parseFrappeError } from '@ury/core';
import { Button } from '@ury/ui';
import { t } from '../i18n';

export interface ManagerApprovalProps {
  open: boolean;
  posProfile: string;
  privilege: string;
  title: string;
  summaryLines: string[];
  reference: { doctype: string; name: string };
  claim: Record<string, unknown>;
  percent?: number;
  amount?: number;
  onApproved: (result: { token: string; approverName: string }) => void;
  onCancel: () => void;
}

interface ApproverOption {
  user: string;
  full_name: string;
}

const OVERRIDE_MOD = 'ury.ury_pos.override';
const STUB_MOD = 'ury.ury_pos._discount_override_stub';

async function callOverrideGet<T>(fn: string, params: Record<string, unknown>): Promise<T> {
  try {
    const res = await call.get<{ message: T }>(`${OVERRIDE_MOD}.${fn}`, params);
    return res.message;
  } catch (err: any) {
    const status = err?.httpStatus ?? err?.status;
    if (status === 404 || /has no attribute|not whitelisted|not found/i.test(String(err?.message || err))) {
      const res = await call.get<{ message: T }>(`${STUB_MOD}.${fn}`, params);
      return res.message;
    }
    throw err;
  }
}

async function callOverridePost<T>(fn: string, body: Record<string, unknown>): Promise<T> {
  try {
    const res = await call.post<{ message: T }>(`${OVERRIDE_MOD}.${fn}`, body);
    return res.message;
  } catch (err: any) {
    const status = err?.httpStatus ?? err?.status;
    if (status === 404 || /has no attribute|not whitelisted|not found/i.test(String(err?.message || err))) {
      const res = await call.post<{ message: T }>(`${STUB_MOD}.${fn}`, body);
      return res.message;
    }
    throw err;
  }
}

const DiscountManagerApproval: React.FC<ManagerApprovalProps> = ({
  open,
  posProfile,
  privilege,
  title,
  summaryLines,
  reference,
  claim,
  percent,
  amount,
  onApproved,
  onCancel,
}) => {
  const [approvers, setApprovers] = useState<ApproverOption[]>([]);
  const [loadingApprovers, setLoadingApprovers] = useState(false);
  const [selectedApprover, setSelectedApprover] = useState('');
  const [pin, setPin] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !posProfile) return;
    let active = true;
    setLoadingApprovers(true);
    setError(null);
    const params: Record<string, unknown> = {
      pos_profile: posProfile,
      privilege,
      reference_name: reference.name,
    };
    if (percent !== undefined) params.percent = percent;
    if (amount !== undefined) params.amount = amount;

    callOverrideGet<ApproverOption[]>('list_approvers', params)
      .then((list) => {
        if (!active) return;
        const rows = Array.isArray(list) ? list : [];
        setApprovers(rows);
        if (rows.length === 1) {
          setSelectedApprover(rows[0].user);
        } else if (!rows.some((r) => r.user === selectedApprover)) {
          setSelectedApprover('');
        }
      })
      .catch((err) => {
        if (!active) return;
        setApprovers([]);
        setError(parseFrappeError(err, t('discount.no_approvers')));
      })
      .finally(() => {
        if (active) setLoadingApprovers(false);
      });

    return () => {
      active = false;
    };
  }, [open, posProfile, privilege, reference.name, percent, amount]);

  if (!open) return null;

  const appendDigit = (digit: string) => {
    setError(null);
    setPin((prev) => (prev.length < 8 ? `${prev}${digit}` : prev));
  };

  const backspace = () => {
    setError(null);
    setPin((prev) => prev.slice(0, -1));
  };

  const clearPin = () => {
    setError(null);
    setPin('');
  };

  const handleAuthorise = async () => {
    if (!selectedApprover || pin.length < 4 || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await callOverridePost<{
        token: string;
        approver: string;
        approver_name: string;
      }>('authorise', {
        pos_profile: posProfile,
        privilege,
        approver: selectedApprover,
        pin,
        reference_doctype: reference.doctype,
        reference_name: reference.name,
        claim,
      });
      setPin('');
      onApproved({
        token: result.token,
        approverName: result.approver_name || result.approver,
      });
    } catch (err) {
      setPin('');
      setError(parseFrappeError(err, t('errors.payment_failed')));
    } finally {
      setSubmitting(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleAuthorise();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      setPin('');
      onCancel();
    }
  };

  return (
    <div
      data-testid="manager-approval-card"
      className="space-y-3 rounded-xl border border-amber-200 bg-amber-50 p-4"
      onKeyDown={handleKeyDown}
    >
      <div className="flex items-start gap-2">
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-700" />
        <div className="min-w-0 flex-1 space-y-1">
          <p className="text-sm font-semibold text-amber-900">{title}</p>
          {summaryLines.map((line, idx) => (
            <p key={idx} className="text-xs text-amber-800">
              {line}
            </p>
          ))}
        </div>
      </div>

      {loadingApprovers ? (
        <p className="text-xs text-amber-700">{t('common.loading')}</p>
      ) : approvers.length === 0 ? (
        <p role="alert" className="text-xs font-medium text-rose-700">
          {t('discount.no_approvers')}
        </p>
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <select
              aria-label={t('discount.manager_label')}
              value={selectedApprover}
              onChange={(e) => {
                setError(null);
                setSelectedApprover(e.target.value);
              }}
              disabled={submitting}
              className="min-h-11 w-full rounded-lg border border-amber-300 bg-white px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-amber-500"
            >
              <option value="">{t('discount.manager_placeholder')}</option>
              {approvers.map((person) => (
                <option key={person.user} value={person.user}>
                  {person.full_name}
                </option>
              ))}
            </select>

            <input
              type="password"
              inputMode="numeric"
              autoComplete="off"
              aria-label={t('discount.pin_label')}
              placeholder={t('discount.pin_placeholder')}
              value={pin}
              onChange={(e) => {
                setError(null);
                setPin(e.target.value.replace(/\D/g, '').slice(0, 8));
              }}
              disabled={submitting}
              className="min-h-11 w-full rounded-lg border border-amber-300 bg-white px-3 py-2 text-center text-sm tracking-widest text-gray-900 focus:outline-none focus:ring-2 focus:ring-amber-500"
            />
          </div>

          {/* Touch-friendly 48px PIN keypad */}
          <div className="grid grid-cols-3 gap-1.5" data-testid="manager-pin-keypad">
            {['1', '2', '3', '4', '5', '6', '7', '8', '9'].map((digit) => (
              <button
                key={digit}
                type="button"
                disabled={submitting}
                onClick={() => appendDigit(digit)}
                className="min-h-12 rounded-lg border border-amber-200 bg-white text-base font-semibold text-gray-900 hover:bg-amber-100 active:bg-amber-200 disabled:opacity-50"
              >
                {digit}
              </button>
            ))}
            <button
              type="button"
              disabled={submitting}
              onClick={clearPin}
              className="min-h-12 rounded-lg border border-amber-200 bg-white text-xs font-medium text-gray-700 hover:bg-amber-100 disabled:opacity-50"
            >
              {t('common.clear')}
            </button>
            <button
              type="button"
              disabled={submitting}
              onClick={() => appendDigit('0')}
              className="min-h-12 rounded-lg border border-amber-200 bg-white text-base font-semibold text-gray-900 hover:bg-amber-100 active:bg-amber-200 disabled:opacity-50"
            >
              0
            </button>
            <button
              type="button"
              disabled={submitting}
              aria-label="Backspace"
              onClick={backspace}
              className="flex min-h-12 items-center justify-center rounded-lg border border-amber-200 bg-white text-gray-700 hover:bg-amber-100 disabled:opacity-50"
            >
              <Delete className="h-4 w-4" />
            </button>
          </div>

          {error && (
            <p role="alert" className="text-xs font-medium text-rose-700">
              {error}
            </p>
          )}

          <div className="flex items-center justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="min-h-11"
              onClick={() => {
                setPin('');
                onCancel();
              }}
              disabled={submitting}
            >
              {t('common.cancel')}
            </Button>
            <Button
              type="button"
              size="sm"
              className="min-h-11"
              onClick={handleAuthorise}
              disabled={submitting || !selectedApprover || pin.length < 4}
            >
              {submitting ? t('common.loading') : t('discount.authorise')}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
};

export default DiscountManagerApproval;
