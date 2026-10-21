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

interface StaffMember {
  user: string;
  full_name: string;
  has_pin: boolean;
}

interface StaffPinsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function floorPinError(error: unknown, fallback: string): string {
  const message = parseFrappeError(error, fallback);
  return message.includes('URY_FLOOR_PIN_NOT_PERMITTED')
    ? t('errors.floor_pin_not_permitted')
    : message;
}

const StaffPinsDialog = ({ open, onOpenChange }: StaffPinsDialogProps) => {
  const [staff, setStaff] = useState<StaffMember[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyUser, setBusyUser] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [issuedFor, setIssuedFor] = useState<string | null>(null);
  const [issuedPin, setIssuedPin] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setStaff([]);
      setError(null);
      setIssuedFor(null);
      setIssuedPin(null);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    setIssuedFor(null);
    setIssuedPin(null);

    call.get('ury.ury_pos.floor_pin.pin_staff')
      .then((response) => {
        if (cancelled) return;
        const message = (response as { message?: unknown }).message;
        setStaff(Array.isArray(message) ? message as StaffMember[] : []);
      })
      .catch((reason) => {
        if (!cancelled) {
          setError(floorPinError(reason, t('staff_pins.load_failed')));
        }
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
      if (typeof pin !== 'string' || !pin) {
        throw new Error(t('staff_pins.issue_failed'));
      }
      setIssuedFor(member.full_name);
      setIssuedPin(pin);
    } catch (reason) {
      setError(floorPinError(reason, t('staff_pins.issue_failed')));
    } finally {
      setBusyUser(null);
    }
  };

  const closeIssuedPin = () => {
    setIssuedFor(null);
    setIssuedPin(null);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent variant="large" onClose={() => onOpenChange(false)} closeLabel={t('common.close')}>
        {issuedPin && issuedFor ? (
          <>
            <DialogHeader>
              <DialogTitle>{t('staff_pins.title')}</DialogTitle>
              <DialogDescription>{t('staff_pins.shown_once')}</DialogDescription>
            </DialogHeader>
            <div className="px-6 pb-6 text-center">
              <p className="mb-4 text-sm text-gray-600">{issuedFor}</p>
              <p className="text-5xl font-semibold tracking-widest text-gray-900" aria-label={t('staff_pins.pin_value')}>
                {issuedPin}
              </p>
            </div>
            <DialogFooter>
              <Button className="w-full" onClick={closeIssuedPin}>{t('staff_pins.done')}</Button>
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
                    <Button
                      variant="outline"
                      disabled={busyUser !== null}
                      onClick={() => void issuePin(member)}
                    >
                      {member.has_pin ? t('staff_pins.new_pin') : t('staff_pins.give_pin')}
                    </Button>
                  </div>
                ))
              )}
              {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
            </div>
            <DialogFooter>
              <Button variant="outline" className="w-full" onClick={() => onOpenChange(false)}>
                {t('common.close')}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
};

export default StaffPinsDialog;
