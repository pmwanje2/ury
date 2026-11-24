import { useCallback, useEffect, useState } from 'react';
import { Clock, AlertTriangle } from 'lucide-react';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  showToast,
} from '@ury/ui';
import {
  getMyTimeclockStatus,
  punchTimeclock,
  type TimeclockStatus,
} from '../lib/timeclock-api';
import { t } from '../i18n';

interface TimeclockButtonProps {
  posProfile?: string | null;
  disabled?: boolean;
  onStatusChange?: (status: TimeclockStatus) => void;
}

function formatClockTime(timestamp?: string | null): string {
  if (!timestamp) return '';
  const match = String(timestamp).match(/(\d{2}:\d{2})/);
  if (match) return match[1];
  const parsed = new Date(String(timestamp).replace(' ', 'T'));
  if (!Number.isNaN(parsed.getTime())) {
    return `${String(parsed.getHours()).padStart(2, '0')}:${String(parsed.getMinutes()).padStart(2, '0')}`;
  }
  return '';
}

function formatElapsed(minutes?: number): string {
  const total = Math.max(0, Math.floor(Number(minutes) || 0));
  const hrs = Math.floor(total / 60);
  const mins = total % 60;
  if (hrs <= 0) return `${mins}m`;
  return `${hrs}h ${String(mins).padStart(2, '0')}m`;
}

const TimeclockButton = ({ posProfile, disabled = false, onStatusChange }: TimeclockButtonProps) => {
  const [status, setStatus] = useState<TimeclockStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    if (!posProfile) {
      setStatus(null);
      return;
    }
    const next = await getMyTimeclockStatus(posProfile);
    setStatus(next);
  }, [posProfile]);

  useEffect(() => {
    let cancelled = false;
    if (!posProfile) {
      setStatus(null);
      return () => {
        cancelled = true;
      };
    }
    getMyTimeclockStatus(posProfile).then((res) => {
      if (!cancelled) setStatus(res);
    });
    return () => {
      cancelled = true;
    };
  }, [posProfile]);

  if (!status?.available) {
    return null;
  }

  const isClockedIn = Boolean(status.is_clocked_in);
  const sinceTime = formatClockTime(status.clocked_in_since || status.last_punch_time);
  const hasOpenWork = Boolean(
    status.block_clock_out_with_open_work &&
      ((status.open_checks_count ?? 0) > 0 || status.has_open_shift),
  );

  const handleOpenDialog = () => {
    setError(null);
    setOpen(true);
    void loadStatus();
  };

  const handlePunch = async () => {
    if (!posProfile || submitting) return;
    const action: 'IN' | 'OUT' = isClockedIn ? 'OUT' : 'IN';
    setSubmitting(true);
    setError(null);
    try {
      const updated = await punchTimeclock(action, posProfile);
      setStatus(updated);
      onStatusChange?.(updated);
      showToast.success(
        action === 'IN' ? t('timeclock.clocked_in_toast') : t('timeclock.clocked_out_toast'),
      );
      setOpen(false);
    } catch (err) {
      const msg = err instanceof Error ? err.message : t('timeclock.punch_failed');
      setError(msg);
      showToast.error(msg);
    } finally {
      setSubmitting(false);
    }
  };

  const pillLabel = isClockedIn
    ? t('timeclock.clock_out_pill', { time: sinceTime || '--:--' })
    : t('timeclock.clock_in');

  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={disabled}
        onClick={handleOpenDialog}
        data-testid="timeclock-button"
        aria-label={pillLabel}
        className={
          isClockedIn
            ? 'flex items-center gap-1.5 h-7 px-2 border-emerald-300 bg-emerald-50 text-emerald-800 hover:bg-emerald-100'
            : 'flex items-center gap-1.5 h-7 px-2 border-amber-300 bg-amber-50 text-amber-900 hover:bg-amber-100'
        }
      >
        <Clock className="w-3.5 h-3.5 shrink-0" />
        <span className="text-xs font-medium truncate max-w-[9rem] sm:max-w-none">
          {pillLabel}
        </span>
      </Button>

      <Dialog open={open} onOpenChange={(next) => !submitting && setOpen(next)}>
        <DialogContent
          size="md"
          onClose={() => !submitting && setOpen(false)}
          className="max-h-[90vh] overflow-y-auto"
        >
          <DialogHeader>
            <DialogTitle>
              {isClockedIn ? t('timeclock.clock_out_title') : t('timeclock.clock_in_title')}
            </DialogTitle>
            <DialogDescription>
              {isClockedIn
                ? t('timeclock.clock_out_description')
                : t('timeclock.clock_in_description')}
            </DialogDescription>
          </DialogHeader>

          <div className="px-6 pb-4 space-y-3 text-sm">
            {status.employee_name && (
              <div className="flex items-center justify-between rounded-md border border-border bg-gray-50 px-3 py-2">
                <span className="text-gray-500">{t('timeclock.staff_label')}</span>
                <span className="font-medium text-gray-900">{status.employee_name}</span>
              </div>
            )}

            {status.shift && (
              <div className="flex items-center justify-between rounded-md border border-border bg-gray-50 px-3 py-2">
                <span className="text-gray-500">{t('timeclock.shift_label')}</span>
                <span className="font-medium text-gray-900">{status.shift}</span>
              </div>
            )}

            {isClockedIn && sinceTime && (
              <div className="flex items-center justify-between rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2">
                <span className="text-emerald-800">{t('timeclock.clocked_in_since')}</span>
                <span className="font-semibold text-emerald-900">
                  {sinceTime} ({formatElapsed(status.elapsed_minutes)})
                </span>
              </div>
            )}

            {!status.has_employee && (
              <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-amber-800">
                <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
                <span>{t('timeclock.no_employee_notice')}</span>
              </div>
            )}

            {isClockedIn && hasOpenWork && (
              <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-amber-900">
                <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
                <div>
                  <p className="font-medium">{t('timeclock.open_work_warning')}</p>
                  <p className="text-xs text-amber-800 mt-1">
                    {status.open_checks_count
                      ? t('timeclock.open_checks_count', { count: status.open_checks_count })
                      : null}
                    {status.open_checks_count && status.has_open_shift ? ' · ' : ''}
                    {status.has_open_shift ? t('timeclock.open_shift_active') : null}
                  </p>
                </div>
              </div>
            )}

            {error && (
              <p className="text-sm font-medium text-red-600 break-words" role="alert">
                {error}
              </p>
            )}
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={submitting}
              onClick={() => setOpen(false)}
            >
              {t('common.cancel')}
            </Button>
            <Button
              type="button"
              disabled={submitting || !status.has_employee}
              onClick={handlePunch}
            >
              {submitting
                ? t('timeclock.saving')
                : isClockedIn
                  ? t('timeclock.confirm_clock_out')
                  : t('timeclock.confirm_clock_in')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
};

export default TimeclockButton;
