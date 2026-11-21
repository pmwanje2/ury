import { useEffect, useState } from 'react';
import { call, parseFrappeError } from '@ury/core';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Spinner,
} from '@ury/ui';
import { t } from '../i18n';
import { floorPinError } from '../lib/floor-pin-errors';

interface StaffMember {
  user: string;
  full_name: string;
  has_pin: boolean;
}

interface StaffPinsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const StaffPinsDialog = ({ open, onOpenChange }: StaffPinsDialogProps) => {
  const [staff, setStaff] = useState<StaffMember[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyUser, setBusyUser] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [issued, setIssued] = useState<{ name: string; pin: string } | null>(null);

  useEffect(() => {
    setIssued(null);
    setError(null);
    if (!open) {
      setStaff([]);
      return;
    }

    let cancelled = false;
    setLoading(true);
    call.get('ury.ury_pos.floor_pin.pin_staff')
      .then((response) => {
        if (cancelled) return;
        const message = (response as { message?: unknown }).message;
        setStaff(Array.isArray(message) ? (message as StaffMember[]) : []);
      })
      .catch((reason) => {
        if (!cancelled) setError(floorPinError(parseFrappeError(reason, t('staff_pins.load_failed')), 'staff_pins'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [open]);

  const issuePin = async (member: StaffMember) => {
    setBusyUser(member.user);
    setError(null);
    try {
      const response = await call.post('ury.ury_pos.floor_pin.issue_pin', { user: member.user });
      const pin = (response as { message?: { pin?: unknown } }).message?.pin;
      if (typeof pin !== 'string' || !pin) throw new Error(t('staff_pins.issue_failed'));
      setIssued({ name: member.full_name, pin });
      setStaff((current) => current.map((row) => (row.user === member.user ? { ...row, has_pin: true } : row)));
    } catch (reason) {
      setError(floorPinError(parseFrappeError(reason, t('staff_pins.issue_failed')), 'staff_pins'));
    } finally {
      setBusyUser(null);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent variant="large" onClose={() => onOpenChange(false)}>
        {issued ? (
          <>
            <DialogHeader>
              <DialogTitle>{t('staff_pins.title')}</DialogTitle>
              <DialogDescription>{t('staff_pins.shown_once')}</DialogDescription>
            </DialogHeader>
            <div className="px-6 pb-6 text-center">
              <p className="mb-4 text-sm text-gray-600">{issued.name}</p>
              <p className="text-5xl font-semibold tracking-widest text-gray-900" aria-label={t('staff_pins.pin_value')} dir="ltr">
                {issued.pin}
              </p>
            </div>
            <DialogFooter>
              <Button className="w-full" onClick={() => setIssued(null)}>{t('staff_pins.done')}</Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>{t('staff_pins.title')}</DialogTitle>
              <DialogDescription>{t('staff_pins.description')}</DialogDescription>
            </DialogHeader>
            <div className="space-y-3 px-6 pb-4">
              {loading ? (
                <Spinner message={t('common.loading')} />
              ) : staff.length === 0 && !error ? (
                <p className="py-4 text-sm text-gray-500">{t('staff_pins.no_staff')}</p>
              ) : (
                staff.map((member) => (
                  <div key={member.user} className="flex items-center justify-between gap-4 border-b border-gray-200 py-3">
                    <div>
                      <p className="font-medium text-gray-900">{member.full_name}</p>
                      {!member.has_pin && <p className="text-sm text-amber-700">{t('staff_pins.needs_pin')}</p>}
                    </div>
                    <Button variant="outline" disabled={busyUser !== null} onClick={() => void issuePin(member)}>
                      {member.has_pin ? t('staff_pins.new_pin') : t('staff_pins.give_pin')}
                    </Button>
                  </div>
                ))
              )}
              {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
            </div>
            <DialogFooter>
              <Button variant="outline" className="w-full" onClick={() => onOpenChange(false)}>
                {t('staff_pins.close')}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
};

export default StaffPinsDialog;
