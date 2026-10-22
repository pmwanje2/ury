import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { LogOut } from 'lucide-react';
import { Button, showToast } from '@ury/ui';
import { clearCashierStorage } from '../offline/cashier';
import { isLinkDown, getUserSessionStorageKey } from '@ury/core';
import { offlineCaptureEnabled } from '../pwa/runtime-caching';
import {
  checkPOSOpening,
  parseFrappeError,
  validatePOSClose,
  POSOpeningEntryRef,
} from '../lib/pos-opening-api';
import { getChecklist } from '../lib/checklist-api';
import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';
import { User } from '../store/slices/auth-slice';
import POSOpeningDialog from './POSOpeningDialog';
import POSOpeningScreen from './POSOpeningScreen';
import ChecklistGateDialog from './ChecklistGateDialog';
import POSClosingDialog from './POSClosingDialog';
import { t } from '../i18n';
import { isFloorOrderPath } from '../lib/home-route';

interface POSOpeningProviderProps {
  children: React.ReactNode;
}

export type OpeningBlockingState =
  | 'permissionDenied'
  | 'dailyClosePending'
  | 'mainCashierNotOpen'
  | 'crossCompanyOpen'
  | 'genericError';

const DESK_ROLES = ['System Manager', 'Desk User'];


function isBillingUser(
  user: User,
  profile: { role_allowed_for_billing?: Array<{ role?: string | null }> | null },
): boolean {
  if (user.name === 'Administrator') return true;
  const rows = profile.role_allowed_for_billing ?? [];
  if (rows.length === 0) return true;
  return user.roles.some((role) => rows.some((row) => row.role === role));
}


function hasDeskAccess(user: User | null): boolean {
  if (!user) return false;
  if (user.name === 'Administrator') return true;
  return user.roles.some((role) => DESK_ROLES.includes(role));
}

function normalizeOpenEntries(
  message: number | POSOpeningEntryRef[]
): POSOpeningEntryRef[] | null {
  if (message === 1) return null;
  if (!Array.isArray(message)) return null;
  return message.filter(
    (entry): entry is POSOpeningEntryRef =>
      !!entry && typeof entry === 'object' && 'name' in entry
  );
}

function getHttpStatus(error: unknown): number | undefined {
  if (!error || typeof error !== 'object') return undefined;
  const e = error as Record<string, unknown>;
  if (typeof e.httpStatus === 'number') return e.httpStatus;
  if (typeof e.status === 'number') return e.status;
  if (typeof e.statusCode === 'number') return e.statusCode;
  return undefined;
}

function readConfirmedOpening(key: string | null): POSOpeningEntryRef | null {
  if (!key) return null;
  try {
    const entry = JSON.parse(sessionStorage.getItem(key) || 'null');
    return entry && typeof entry === 'object' && 'name' in entry ? entry as POSOpeningEntryRef : null;
  } catch {
    return null;
  }
}

const POSOpeningProvider = ({ children }: POSOpeningProviderProps) => {
  const [isLoading, setIsLoading] = useState(true);
  const [blockingState, setBlockingState] = useState<OpeningBlockingState | null>(null);
  const [blockingMessage, setBlockingMessage] = useState<string>('');
  const [existingEntry, setExistingEntry] = useState<POSOpeningEntryRef | null>(null);
  // Set once checkPOSOpening() confirms the POS is open but today's Opening
  // checklist log isn't status="Complete" yet. Takes priority over the
  // daily-close check below -- the cashier must clear the checklist before
  // we even look at whether a previous session needs closing.
  const [needsOpeningChecklist, setNeedsOpeningChecklist] = useState(false);
  // True once the cashier taps "Close Now" from the dailyClosePending
  // blocking dialog -- renders the real POSClosingDialog instead of just
  // telling them to contact a manager.
  const [showClosingDialog, setShowClosingDialog] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const logoutPending = useRef(false);

  const handleLogout = async () => {
    if (logoutPending.current) return;
    logoutPending.current = true;
    setLoggingOut(true);
    try {
      await clearCashierStorage(true, (window as Window & { csrf_token?: string }).csrf_token);
      window.location.href = '/login?redirect-to=%2Fpos';
    } catch (error) {
      showToast.error(error instanceof Error && /outbox/i.test(error.message) ? error.message : t('errors.failed_logout'));
      logoutPending.current = false;
      setLoggingOut(false);
    }
  };

  const logoutAction = (
    <Button variant="outline" onClick={handleLogout} disabled={loggingOut}>
      <LogOut className="w-4 h-4 mr-2" />
      {t('header.logout')}
    </Button>
  );

  const { posProfile, showVoluntaryClosing, setShowVoluntaryClosing } = usePOSStore();
  const { user } = useRootStore();

  const canAccessDesk = useMemo(() => hasDeskAccess(user), [user]);
  const floorOrder = useMemo(() => !!user && !!posProfile && !isBillingUser(user, posProfile)
    && isFloorOrderPath(window.location.pathname, user), [user, posProfile]);
  const confirmedOpeningKey = getUserSessionStorageKey('ury_offline_opening', user?.name);

  const clearBlockingState = useCallback(() => {
    setBlockingState(null);
    setBlockingMessage('');
    setShowClosingDialog(false);
  }, []);

  const handleScreenError = useCallback(
    (state: OpeningBlockingState, message?: string) => {
      setBlockingState(state);
      setBlockingMessage(message || '');
    },
    []
  );

  const checkPOSStatus = useCallback(async (resumeEntry?: POSOpeningEntryRef) => {
    if (!posProfile) return;

    setIsLoading(true);
    clearBlockingState();
    setExistingEntry(null);
    setNeedsOpeningChecklist(false);

    try {
      const response = resumeEntry
        ? { message: [resumeEntry] }
        : await checkPOSOpening(user?.name);

      // No open entry for this user -> show the native opening screen.
      if (response.message === 1) {
        if (posProfile.custom_daily_pos_close === 1) {
          const closeResponse = await validatePOSClose(posProfile.name);
          if (closeResponse.message === 'Failed') {
            setBlockingState('dailyClosePending');
            setIsLoading(false);
            return;
          }
        }
        setIsLoading(false);
        return;
      }

      const entries = normalizeOpenEntries(response.message);
      if (!entries || entries.length === 0) {
        setIsLoading(false);
        return;
      }

      // `branch` is intentionally NOT part of this comparison: the backing
      // API here (ERPNext's standard `check_opening_entry`) never returns a
      // `branch` field at all -- confirmed live, its response only ever
      // carries `name`/`company`/`pos_profile`/`period_start_date`. Requiring
      // it to match meant `entry.branch === posProfile.branch` was
      // `undefined === "<real branch>"`, always false, so this fired a false
      // "Open Session Elsewhere" block for every single cashier immediately
      // after every successful open. `company` + `pos_profile` alone is a
      // correct match in this app's data model (one POS Profile always
      // belongs to exactly one branch).
      const matchingEntry =
        entries.find(
          (entry) =>
            entry.pos_profile === posProfile.name &&
            entry.company === posProfile.company
        ) || entries[0];

      setExistingEntry(matchingEntry);

      const isSameContext =
        matchingEntry.company === posProfile.company &&
        matchingEntry.pos_profile === posProfile.name;

      if (!isSameContext) {
        setBlockingState('crossCompanyOpen');
        setIsLoading(false);
        return;
      }

      // Gate on the Opening checklist before the daily-close check below --
      // the cashier must clear it before we look at whether a previous
      // session needs closing.
      if (posProfile.name) {
        try {
          const { logStatus } = await getChecklist(posProfile.name, 'Opening');
          if (logStatus !== 'Complete') {
            setNeedsOpeningChecklist(true);
            setIsLoading(false);
            return;
          }
        } catch (error) {
          // A gateway page is the link being down, not a pending checklist.
          if (isLinkDown(error)) throw error;
          console.error('Failed to check opening checklist status:', error);
          // On error, block on the checklist for safety.
          setNeedsOpeningChecklist(true);
          setIsLoading(false);
          return;
        }
      }

      if (posProfile.custom_daily_pos_close === 1) {
        const closeResponse = await validatePOSClose(posProfile.name);
        if (closeResponse.message === 'Failed') {
          setBlockingState('dailyClosePending');
          setIsLoading(false);
          return;
        }
      }

      // Every gate passed online. An opted-in till remembers this opening for
      // the tab so an offline reload can show the cached workspace.
      if (offlineCaptureEnabled(posProfile) && confirmedOpeningKey) {
        sessionStorage.setItem(confirmedOpeningKey, JSON.stringify(matchingEntry));
      }
      setIsLoading(false);
    } catch (error) {
      console.error('Failed to check POS opening status:', error);
      // The opening read is not cached. Only a down link (no HTTP answer, or
      // 502/503/504/52x) on an opted-in till that confirmed this same opening
      // online in this tab may show the workspace. 401/403/500 still block.
      const confirmed = isLinkDown(error) && offlineCaptureEnabled(posProfile)
        ? readConfirmedOpening(confirmedOpeningKey)
        : null;
      if (confirmed && confirmed.pos_profile === posProfile.name && confirmed.company === posProfile.company) {
        setExistingEntry(confirmed);
        setIsLoading(false);
        return;
      }

      const serverMessage = parseFrappeError(error);
      const status = getHttpStatus(error);
      const fallbackMessage =
        error instanceof Error ? error.message : t('errors.posOpening.load_failed');
      const message = serverMessage || fallbackMessage;

      if (status === 403 || /permission/i.test(message)) {
        setBlockingState('permissionDenied');
      } else if (/main cashier/i.test(message)) {
        setBlockingState('mainCashierNotOpen');
        setBlockingMessage(message);
      } else {
        setBlockingState('genericError');
        setBlockingMessage(message);
      }

      setIsLoading(false);
    }
  }, [posProfile, user?.name, clearBlockingState, confirmedOpeningKey]);

  const handleOpeningSuccess = useCallback(() => {
    clearBlockingState();
    checkPOSStatus();
  }, [clearBlockingState, checkPOSStatus]);

  const handleClosingSuccess = useCallback(() => {
    clearBlockingState();
    checkPOSStatus();
  }, [clearBlockingState, checkPOSStatus]);

  const handleOpeningChecklistComplete = useCallback(() => {
    setNeedsOpeningChecklist(false);
    checkPOSStatus(existingEntry || undefined);
  }, [checkPOSStatus, existingEntry]);

  useEffect(() => {
    if (floorOrder) {
      setIsLoading(false);
      return;
    }
    if (posProfile) {
      checkPOSStatus();
    }
  }, [posProfile, checkPOSStatus, floorOrder]);

  if (floorOrder) {
    return <>{children}</>;
  }

  if (isLoading) {
    return (
      <div className="fixed inset-0 bg-white flex items-center justify-center z-50">
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto mb-4"></div>
          <p className="text-gray-600">{t('common.checking_pos_status')}</p>
          <div className="mt-4">{logoutAction}</div>
        </div>
      </div>
    );
  }

  if (showClosingDialog && posProfile) {
    return (
      <POSClosingDialog
        open={showClosingDialog}
        onOpenChange={(open) => {
          if (!open) setShowClosingDialog(false);
        }}
        onClosingSubmitted={handleClosingSuccess}
        logoutAction={logoutAction}
      />
    );
  }

  if (blockingState) {
    return (
      <POSOpeningDialog
        state={blockingState}
        message={blockingMessage}
        existingEntry={existingEntry}
        canAccessDesk={canAccessDesk}
        logoutAction={logoutAction}
        onRetry={() => checkPOSStatus()}
        onContinue={() => setBlockingState(null)}
        onCloseNow={
          blockingState === 'dailyClosePending'
            ? () => setShowClosingDialog(true)
            : undefined
        }
      />
    );
  }

  // Block on the Opening checklist until it's submitted as Complete.
  if (needsOpeningChecklist && posProfile?.name) {
    return (
      <ChecklistGateDialog
        posProfile={posProfile.name}
        checklistType="Opening"
        logoutAction={logoutAction}
        onComplete={handleOpeningChecklistComplete}
      />
    );
  }

  if (!existingEntry) {
    return (
      <POSOpeningScreen
        onSuccess={handleOpeningSuccess}
        onError={handleScreenError}
        onContinue={(entry) => checkPOSStatus(entry)}
      />
    );
  }

  return (
    <>
      {children}
      {/*
        Voluntary close (Header menu "Close Shift"), distinct from the
        dailyClosePending forced path above. Rendered as an overlay
        alongside children -- rather than replacing them, like the forced
        path does -- since the cashier is choosing to close their own
        current, non-stale session and should be able to back out without
        losing their place in the app.
      */}
      {showVoluntaryClosing && (
        <POSClosingDialog
          open={showVoluntaryClosing}
          onOpenChange={(open) => {
            if (!open) setShowVoluntaryClosing(false);
          }}
          onClosingSubmitted={() => {
            setShowVoluntaryClosing(false);
            checkPOSStatus();
          }}
        />
      )}
    </>
  );
};

export default POSOpeningProvider;
