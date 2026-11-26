import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { useLocation } from 'react-router-dom';
import { call } from '@ury/core';
import ApprovalPinDialog from './ApprovalPinDialog';
import { useMyPinStatus } from '../lib/use-my-pin-status';
import type { MyPinStatus } from '../lib/override-api';
import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';

interface ApprovalPinAccess {
  openDialog: () => void;
  status: MyPinStatus | null;
  loadFailed: boolean;
  canManagePins: boolean;
}

const ApprovalPinContext = createContext<ApprovalPinAccess>({
  openDialog: () => {}, status: null, loadFailed: false, canManagePins: false,
});
export const useApprovalPin = () => useContext(ApprovalPinContext);

function PinSession({ children, profile, namedStaff }: {
  children: ReactNode; profile?: string; namedStaff: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [canManagePins, setCanManagePins] = useState(false);
  const { pathname } = useLocation();
  const pin = useMyPinStatus(namedStaff && (open || pathname.replace(/\/$/, '') === '/settings'));

  useEffect(() => {
    if (!profile) return;
    let active = true;
    call.get<{ message?: { has_permission?: unknown } }>('frappe.client.has_permission', {
      doctype: 'POS Profile', docname: profile, perm_type: 'write',
    }).then(response => {
      if (active) setCanManagePins(response?.message?.has_permission === true);
    }).catch(() => { if (active) setCanManagePins(false); });
    return () => { active = false; };
  }, [profile]);

  const openDialog = () => {
    if (pin.loadFailed) void pin.refresh();
    setOpen(true);
  };

  return (
    <ApprovalPinContext.Provider value={{
      openDialog, status: pin.status, loadFailed: pin.loadFailed, canManagePins,
    }}>
      {children}
      {namedStaff && <ApprovalPinDialog open={open} onOpenChange={setOpen} pinStatus={pin}
        posProfile={profile} canManagePins={canManagePins} />}
    </ApprovalPinContext.Provider>
  );
}

export function ApprovalPinProvider({ children }: { children: ReactNode }) {
  const name = useRootStore(state => state.user?.name);
  const profile = usePOSStore(state => state.posProfile?.name);
  return <PinSession key={JSON.stringify([name, profile])} profile={profile}
    namedStaff={Boolean(name && name !== 'Guest' && name !== 'Administrator')}>{children}</PinSession>;
}
