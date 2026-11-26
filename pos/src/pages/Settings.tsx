import { useEffect, useState } from 'react';
import { call } from '@ury/core';
import { Button, Card, CardContent } from '@ury/ui';
import { t } from '../i18n';
import POSCloseFlow from '../components/POSCloseFlow';
import ApprovalPinDialog, { PinStatusBadges } from '../components/ApprovalPinDialog';
import StaffPinsDialog from '../components/StaffPinsDialog';
import PairTabletDialog from '../components/PairTabletDialog';
import { getMyPinStatus, type MyPinStatus } from '../lib/override-api';
import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';

function SecurityControls({ userName, posProfileName }: { userName: string; posProfileName?: string }) {
  const [dialog, setDialog] = useState<'approval' | 'staff' | 'pair' | null>(null);
  const [status, setStatus] = useState<MyPinStatus | 'error' | null>(null);
  const [canManagePins, setCanManagePins] = useState(false);
  const namedStaff = userName !== 'Administrator';
  const approvalOpen = dialog === 'approval';

  useEffect(() => {
    if (!posProfileName) return;
    let active = true;
    call.get<{ message?: { has_permission?: unknown } }>('frappe.client.has_permission', {
      doctype: 'POS Profile', docname: posProfileName, perm_type: 'write',
    }).then((response) => {
      if (active) setCanManagePins(response?.message?.has_permission === true);
    }).catch(() => { if (active) setCanManagePins(false); });
    return () => { active = false; };
  }, [posProfileName]);

  useEffect(() => {
    if (!namedStaff || approvalOpen) return;
    let active = true;
    getMyPinStatus().then((result) => { if (active) setStatus(result); })
      .catch(() => { if (active) setStatus('error'); });
    return () => { active = false; };
  }, [namedStaff, approvalOpen]);

  const close = (open: boolean) => { if (!open) setDialog(null); };
  return (
    <>
      {namedStaff && (
        <Card>
          <CardContent className="space-y-3 p-4">
            <h3 className="font-semibold">{t('approval_pin.title')}</h3>
            {status === 'error' ? <p role="status">{t('approval_pin.load_failed')}</p>
              : status && <PinStatusBadges {...status} />}
            <p className="text-sm text-muted-foreground">{t('settings.personal_pin_note')}</p>
            <Button onClick={() => setDialog('approval')}>{t('settings.manage_my_pin')}</Button>
          </CardContent>
          <ApprovalPinDialog open={approvalOpen} onOpenChange={close}
            posProfile={posProfileName} canManagePins={canManagePins} />
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
          <StaffPinsDialog open={dialog === 'staff'} onOpenChange={close} />
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
