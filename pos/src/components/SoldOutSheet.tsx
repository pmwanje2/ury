import { useEffect, useState } from 'react';
import { AvailabilityActionMenu, Dialog, DialogContent, DialogHeader, DialogTitle, type AvailabilityActions } from '@ury/ui';
import { getAvailabilityOverrides, type AvailabilityOverride, type MenuAvailability } from '../lib/availability-api';
import { availabilityActionLabels } from '../lib/availability-labels';
import { t } from '../i18n';

export function SoldOutSheet({ branch, company, availability, actions }: {
  branch: string; company: string; availability: MenuAvailability; actions: (item: string) => AvailabilityActions;
}) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<AvailabilityOverride[]>();
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!open) return;
    let active = true;
    setFailed(false);
    setLoading(true);
    void getAvailabilityOverrides(branch, company).then(result => { if (active) setRows(result); })
      .catch(() => { if (active) setFailed(true); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [branch, company, availability, open]);
  const soldOut = Object.values(availability).filter(row => row.manual_status === 'Sold Out' || row.reason_code === 'COUNT_EXHAUSTED');
  return <>
    <button type="button" className="min-h-11 rounded-full border px-3 text-sm" onClick={() => setOpen(true)}>
      {t('menu.sold_out_count', { count: soldOut.length })}
    </button>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent onClose={() => setOpen(false)}>
        <DialogHeader><DialogTitle>{t('menu.availability.sold_out')}</DialogTitle></DialogHeader>
        <div className="max-h-[65vh] overflow-auto">
          {failed && <p role="alert">{t('menu.availability_load_failed')}</p>}
          {loading && !rows && <p>{t('common.loading')}</p>}
          {rows?.length === 0 && <p>{t('menu.no_availability_overrides')}</p>}
          {rows?.map(row => <div key={row.item} className="flex items-start justify-between gap-3 border-b p-2">
            <div>
              <p className="font-medium">{row.item_name}</p>
              {row.status === 'Sold Out' && <p>{t('menu.availability.sold_out')}</p>}
              {!!row.countdown_enabled && <p>{t('menu.qty_left', { qty: row.remaining_qty })}</p>}
            </div>
            <AvailabilityActionMenu actions={actions(row.item)} labels={availabilityActionLabels()}
              manualSoldOut={row.status === 'Sold Out'} dailyLimit={row.countdown_enabled ? row.daily_limit : null}
              autoReset={!!row.auto_reset_on_open} />
          </div>)}
        </div>
      </DialogContent>
    </Dialog>
  </>;
}
