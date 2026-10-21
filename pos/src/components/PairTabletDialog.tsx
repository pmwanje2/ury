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
  Input,
} from '@ury/ui';
import { t } from '../i18n';

const MAX_LABEL_LENGTH = 80;

interface PairTabletDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  posProfile: string;
}

function floorPinError(error: unknown, fallback: string): string {
  const message = parseFrappeError(error, fallback);
  return message.includes('URY_FLOOR_PIN_NOT_PERMITTED')
    ? t('errors.floor_pin_not_permitted')
    : message;
}

const PairTabletDialog = ({ open, onOpenChange, posProfile }: PairTabletDialogProps) => {
  const [label, setLabel] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setLabel('');
      setError(null);
      setBusy(false);
    }
  }, [open]);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalizedLabel = label.trim();
    if (!normalizedLabel) {
      setError(t('pair_tablet.label_required'));
      return;
    }
    if (normalizedLabel.length > MAX_LABEL_LENGTH) {
      setError(t('pair_tablet.label_too_long'));
      return;
    }

    setBusy(true);
    setError(null);
    try {
      await call.post('ury.ury_pos.floor_pin.pair_device', {
        pos_profile: posProfile,
        label: normalizedLabel,
      });
      onOpenChange(false);
      window.location.reload();
    } catch (reason) {
      setError(floorPinError(reason, t('pair_tablet.failed')));
    } finally {
      setBusy(false);
    }
  };

  const handleOpenChange = (next: boolean) => {
    if (!busy) onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent variant="large" onClose={() => handleOpenChange(false)} closeLabel={t('common.close')}>
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>{t('header.pair_tablet')}</DialogTitle>
            <DialogDescription>{t('pair_tablet.description')}</DialogDescription>
          </DialogHeader>
          <div className="space-y-2 px-6 pb-6">
            <label htmlFor="tablet_label" className="block text-sm font-medium text-gray-700">
              {t('pair_tablet.label')}
            </label>
            <Input
              id="tablet_label"
              name="tablet_label"
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              placeholder={t('pair_tablet.placeholder')}
              maxLength={MAX_LABEL_LENGTH}
              required
              disabled={busy}
            />
            {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => handleOpenChange(false)} disabled={busy}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={busy}>
              {busy ? t('common.loading') : t('pair_tablet.submit')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
};

export default PairTabletDialog;
