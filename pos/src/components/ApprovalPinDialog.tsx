import { useEffect, useState } from 'react';
import {
  Badge,
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Spinner,
} from '@ury/ui';
import { t } from '../i18n';
import {
  clearUserPin,
  getMyPinStatus,
  listPinUsers,
  overrideErrorMessage,
  setMyPin,
  type MyPinStatus,
  type PinUserStatus,
} from '../lib/override-api';

const PIN_RE = /^[0-9]{4,8}$/;

export interface ApprovalPinDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  posProfile?: string | null;
  canManagePins?: boolean;
}

export default function ApprovalPinDialog({
  open,
  onOpenChange,
  posProfile,
  canManagePins = false,
}: ApprovalPinDialogProps) {
  const [tab, setTab] = useState<'self' | 'staff'>('self');
  const [status, setStatus] = useState<MyPinStatus>({ has_pin: false, external_verifier: false });
  const [loading, setLoading] = useState(false);
  const [password, setPassword] = useState('');
  const [pin, setPin] = useState('');
  const [confirmPin, setConfirmPin] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const [staff, setStaff] = useState<PinUserStatus[]>([]);
  const [staffLoading, setStaffLoading] = useState(false);
  const [clearingUser, setClearingUser] = useState<string | null>(null);

  const showStaffTab = Boolean(canManagePins && posProfile);

  useEffect(() => {
    if (!open) {
      setPassword('');
      setPin('');
      setConfirmPin('');
      setError(null);
      setSuccess(null);
      setTab('self');
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    setSuccess(null);
    getMyPinStatus()
      .then((res) => {
        if (!cancelled) setStatus(res);
      })
      .catch((err) => {
        if (!cancelled) setError(overrideErrorMessage(err, 'approval_pin.load_failed'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [open]);

  useEffect(() => {
    if (!open || tab !== 'staff' || !posProfile || !canManagePins) return;
    let cancelled = false;
    setStaffLoading(true);
    setError(null);
    listPinUsers(posProfile)
      .then((rows) => {
        if (!cancelled) setStaff(rows);
      })
      .catch((err) => {
        if (!cancelled) setError(overrideErrorMessage(err, 'approval_pin.load_failed'));
      })
      .finally(() => {
        if (!cancelled) setStaffLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, tab, posProfile, canManagePins]);

  const handleSavePin = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setSuccess(null);

    if (!password) {
      setError(t('approval_pin.password_required'));
      return;
    }
    if (!PIN_RE.test(pin)) {
      setError(t('approval_pin.pin_format'));
      return;
    }
    if (pin !== confirmPin) {
      setError(t('approval_pin.pin_mismatch'));
      return;
    }

    setBusy(true);
    try {
      await setMyPin(password, pin);
      setStatus((prev) => ({ ...prev, has_pin: true }));
      setPassword('');
      setPin('');
      setConfirmPin('');
      setSuccess(t('approval_pin.saved'));
    } catch (err) {
      setError(overrideErrorMessage(err, 'approval_pin.save_failed'));
    } finally {
      setBusy(false);
    }
  };

  const handleClearStaffPin = async (member: PinUserStatus) => {
    if (!posProfile) return;
    setClearingUser(member.user);
    setError(null);
    setSuccess(null);
    try {
      await clearUserPin(member.user, posProfile);
      setStaff((prev) => prev.map((row) => (row.user === member.user ? { ...row, has_pin: false } : row)));
      setSuccess(t('approval_pin.cleared', { name: member.full_name }));
    } catch (err) {
      setError(overrideErrorMessage(err, 'approval_pin.clear_failed'));
    } finally {
      setClearingUser(null);
    }
  };

  const handleOpenChange = (next: boolean) => {
    if (!busy && !clearingUser) onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent onClose={() => handleOpenChange(false)}>
        <DialogHeader>
          <DialogTitle>{t('approval_pin.title')}</DialogTitle>
          <DialogDescription>{t('approval_pin.description')}</DialogDescription>
        </DialogHeader>

        {showStaffTab && (
          <div className="flex gap-2 px-6 pb-2">
            <Button
              type="button"
              size="sm"
              variant={tab === 'self' ? 'default' : 'outline'}
              onClick={() => {
                setTab('self');
                setError(null);
                setSuccess(null);
              }}
            >
              {t('approval_pin.tab_my_pin')}
            </Button>
            <Button
              type="button"
              size="sm"
              variant={tab === 'staff' ? 'default' : 'outline'}
              onClick={() => {
                setTab('staff');
                setError(null);
                setSuccess(null);
              }}
            >
              {t('approval_pin.tab_clear_staff')}
            </Button>
          </div>
        )}

        {tab === 'self' ? (
          <form onSubmit={handleSavePin}>
            <div className="space-y-4 px-6 pb-5">
              {loading ? (
                <Spinner message={t('common.loading')} />
              ) : status.external_verifier ? (
                <p className="rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900">
                  {t('approval_pin.external_managed')}
                </p>
              ) : (
                <>
                  <div className="flex items-center justify-between">
                    <span className="text-sm text-gray-600">{t('approval_pin.status_label')}</span>
                    <Badge variant={status.has_pin ? 'default' : 'outline'}>
                      {status.has_pin ? t('approval_pin.status_set') : t('approval_pin.status_not_set')}
                    </Badge>
                  </div>

                  <div className="space-y-1">
                    <label htmlFor="approval-pin-password" className="block text-sm font-medium text-gray-700">
                      {t('approval_pin.current_password')}
                    </label>
                    <Input
                      id="approval-pin-password"
                      type="password"
                      autoComplete="current-password"
                      value={password}
                      disabled={busy}
                      onChange={(e) => setPassword(e.target.value)}
                      required
                    />
                  </div>

                  <div className="space-y-1">
                    <label htmlFor="approval-pin-new" className="block text-sm font-medium text-gray-700">
                      {t('approval_pin.new_pin')}
                    </label>
                    <Input
                      id="approval-pin-new"
                      type="password"
                      inputMode="numeric"
                      autoComplete="new-password"
                      maxLength={8}
                      value={pin}
                      disabled={busy}
                      onChange={(e) => setPin(e.target.value.replace(/\D/g, '').slice(0, 8))}
                      placeholder={t('approval_pin.pin_placeholder')}
                      required
                    />
                  </div>

                  <div className="space-y-1">
                    <label htmlFor="approval-pin-confirm" className="block text-sm font-medium text-gray-700">
                      {t('approval_pin.confirm_pin')}
                    </label>
                    <Input
                      id="approval-pin-confirm"
                      type="password"
                      inputMode="numeric"
                      autoComplete="new-password"
                      maxLength={8}
                      value={confirmPin}
                      disabled={busy}
                      onChange={(e) => setConfirmPin(e.target.value.replace(/\D/g, '').slice(0, 8))}
                      placeholder={t('approval_pin.pin_placeholder')}
                      required
                    />
                  </div>
                </>
              )}

              {error && (
                <p role="alert" className="text-sm text-rose-700">
                  {error}
                </p>
              )}
              {success && (
                <p role="status" className="text-sm text-emerald-700">
                  {success}
                </p>
              )}
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => handleOpenChange(false)} disabled={busy}>
                {t('common.cancel')}
              </Button>
              {!status.external_verifier && (
                <Button type="submit" disabled={busy || loading}>
                  {busy ? t('common.loading') : t('approval_pin.save_button')}
                </Button>
              )}
            </DialogFooter>
          </form>
        ) : (
          <>
            <div className="space-y-3 px-6 pb-5">
              <p className="text-xs text-gray-600">{t('approval_pin.clear_policy_note')}</p>
              {staffLoading ? (
                <Spinner message={t('common.loading')} />
              ) : staff.length === 0 ? (
                <p className="py-4 text-sm text-gray-500">{t('staff_pins.no_staff')}</p>
              ) : (
                <div className="max-h-72 space-y-2 overflow-y-auto">
                  {staff.map((member) => (
                    <div
                      key={member.user}
                      className="flex items-center justify-between gap-3 border-b border-gray-100 py-2.5"
                    >
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium text-gray-900">{member.full_name}</p>
                        <p className="text-xs text-gray-500">
                          {member.has_pin ? t('approval_pin.status_set') : t('approval_pin.status_not_set')}
                        </p>
                      </div>
                      {member.has_pin && (
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={clearingUser !== null}
                          onClick={() => void handleClearStaffPin(member)}
                        >
                          {clearingUser === member.user ? t('common.loading') : t('approval_pin.clear_pin')}
                        </Button>
                      )}
                    </div>
                  ))}
                </div>
              )}
              {error && (
                <p role="alert" className="text-sm text-rose-700">
                  {error}
                </p>
              )}
              {success && (
                <p role="status" className="text-sm text-emerald-700">
                  {success}
                </p>
              )}
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                className="w-full"
                onClick={() => handleOpenChange(false)}
                disabled={clearingUser !== null}
              >
                {t('staff_pins.close')}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
