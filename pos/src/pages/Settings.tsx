import { useState } from 'react';
import { Badge, Button, Card, CardContent } from '@ury/ui';
import { t } from '../i18n';
import POSCloseFlow from '../components/POSCloseFlow';
import { isPinManagedExternally } from '../lib/use-my-pin-status';
import { PinStatusBadges } from '../components/ApprovalPinDialog';
import StaffPinsDialog from '../components/StaffPinsDialog';
import PairTabletDialog from '../components/PairTabletDialog';
import { useApprovalPin } from '../components/ApprovalPinProvider';
import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';

function SecurityControls({ userName, posProfileName }: { userName: string; posProfileName?: string }) {
  const [dialog, setDialog] = useState<'staff' | 'pair' | null>(null);
  const { status, loadFailed, canManagePins, openDialog } = useApprovalPin();
  const external = status && isPinManagedExternally(status);
  const namedStaff = userName !== 'Administrator';
  const close = (open: boolean) => { if (!open) setDialog(null); };
  return (
    <>
      {namedStaff && (
        <Card>
          <CardContent className="space-y-3 p-4">
            <h3 className="font-semibold">{t('approval_pin.title')}</h3>
            {loadFailed ? <p role="status">{t('approval_pin.load_failed')}</p>
              : external ? <Badge variant="outline">{t('approval_pin.status_external')}</Badge>
              : status && <PinStatusBadges {...status} />}
            {status && <p className="text-sm text-muted-foreground">
              {t(status.reauth?.mode === 'unavailable' ? 'approval_pin.unavailable' : external ? 'approval_pin.external_managed' : 'settings.personal_pin_note')}
            </p>}
            <Button onClick={openDialog}>{t('settings.manage_my_pin')}</Button>
          </CardContent>
        </Card>
      )}
      {canManagePins && posProfileName && (
        <Card>
          <CardContent className="space-y-3 p-4">
            <h3 className="font-semibold">{t('settings.floor_devices')}</h3>
            <p className="text-sm text-muted-foreground">{t('settings.waiter_pin_note')}</p>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" onClick={() => setDialog('staff')}>{t('header.staff_pins')}</Button>
              <Button variant="outline" onClick={() => setDialog('pair')}>{t('header.pair_tablet')}</Button>
            </div>
          </CardContent>
          {dialog === 'staff' && <StaffPinsDialog key={posProfileName} posProfile={posProfileName} open onOpenChange={close} />}
          <PairTabletDialog open={dialog === 'pair'} onOpenChange={close} posProfile={posProfileName} />
        </Card>
      )}
    </>
  );
}

export default function Settings() {
  const userName = useRootStore((state) => state.user?.name);
  const posProfileName = usePOSStore((state) => state.posProfile?.name);
  return (
    <div className="h-full overflow-y-auto bg-background p-6 text-foreground">
      <div className="max-w-md mx-auto space-y-6">
        <h1 className="text-xl font-semibold">{t('settings.title')}</h1>
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-2">
            {t('settings.end_of_day')}
          </h2>
          <POSCloseFlow />
        </section>
        <section className="space-y-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {t('settings.security')}
          </h2>
          {userName && userName !== 'Guest' && (
            <SecurityControls key={JSON.stringify([userName, posProfileName])}
              userName={userName} posProfileName={posProfileName} />
          )}
        </section>
      </div>
    </div>
  );
}
