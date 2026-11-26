import React, { Suspense, useEffect, useState } from 'react';
import { useRootStore } from '../store/root-store';
import { Button } from '@ury/ui';
import { Spinner } from '@ury/ui';
import { RefreshCw } from 'lucide-react';
import { t } from '../i18n'
import FloorPinPad from './FloorPinPad';
import OutboxPanel from './OutboxPanel';
import { assertOutboxEmpty, onOutboxMembershipChange, queuedProfilesFor, type OfflineProfile, type OfflineUser } from '../offline/outbox';
import { getPosProfileFull } from '../lib/pos-profile-api';
import {
  retryFloorSignOut,
  needsFloorReconciliation,
  hasFloorLogoutFailureMarker,
  readFloorLogoutFailureMarker,
  hasFloorSessionMarker,
  hasRecoveryConfirmation,
  isFloorPadActive,
  isStaySignedIn,
  useFloorIdle,
} from '../lib/floor-session';

const LazyOutletPickerDialog = React.lazy(() =>
  import('./OutletSwitcher').then((mod) => ({ default: mod.OutletPickerDialog })),
);

interface Props {
  children: React.ReactNode;
}

function RecoveryOutbox({ profile, user }: { profile: OfflineProfile | null; user: OfflineUser }) {
  const [otherProfiles, setOtherProfiles] = useState<OfflineProfile[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    const load = async () => {
      const names = await queuedProfilesFor(user.name);
      const others = await Promise.allSettled(names.filter((name) => name !== profile?.name).map(getPosProfileFull));
      if (active) {
        setOtherProfiles(others.flatMap((result) => result.status === 'fulfilled' ? [result.value] : []));
        setLoadFailed(others.some((result) => result.status === 'rejected'));
      }
    };
    void load().catch(() => { if (active) setLoadFailed(true); });
    return () => { active = false; };
  }, [profile?.name, user.name, attempt]);

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 px-6">
      <p role="status">{t('floor_pin.pending_signout')}</p>
      {profile && <OutboxPanel posProfile={profile} user={user} />}
      {otherProfiles.map((outlet) => <OutboxPanel key={outlet.name} posProfile={outlet} user={user} />)}
      {loadFailed && <Button variant="outline" onClick={() => setAttempt(attempt + 1)}>{t('floor_pin.retry_outbox')}</Button>}
    </div>
  );
}

function OwnerSignIn({ user, passwordOnly }: { user: OfflineUser; passwordOnly: boolean }) {
  return (
    <>
      <p role="status" className="px-6 py-3 text-center">{t('floor_pin.owner_required')}</p>
      <FloorPinPad recoveryOwner={user.name} usernameOnly={passwordOnly} />
    </>
  );
}

function UnconfirmedWorkspace({ user, children }: Props & { user: OfflineUser }) {
  const [empty, setEmpty] = useState<boolean | null>(null);
  useEffect(() => {
    let revision = 0;
    const check = async () => {
      const current = ++revision;
      try {
        await assertOutboxEmpty();
        if (current === revision) setEmpty(true);
      } catch {
        if (current === revision) setEmpty(false);
      }
    };
    const stop = onOutboxMembershipChange(() => {
      setEmpty((previous) => previous === true ? null : previous);
      void check();
    });
    window.addEventListener('focus', check);
    void check();
    return () => {
      revision++;
      stop();
      window.removeEventListener('focus', check);
    };
  }, []);

  if (empty === null) return <Spinner message={t('common.loading')} />;
  if (!empty) return <OwnerSignIn user={user} passwordOnly={!isFloorPadActive()} />;
  return <>{children}</>;
}

function ColdBootSignOut({ profile, user, passwordOnly, failedLogout }: { failedLogout: boolean; profile: OfflineProfile | null; user: OfflineUser; passwordOnly: boolean }) {
  const [state, setState] = useState<'checking' | 'blocked' | 'signed_out' | 'logout_failed'>('checking');

  useEffect(() => retryFloorSignOut((result) => {
    if (result !== 'cancelled') setState(result);
  }, failedLogout), [failedLogout]);

  if (state === 'signed_out' || state === 'logout_failed') return <FloorPinPad usernameOnly={passwordOnly || state === 'logout_failed'} />;
  if (state === 'checking') return <Spinner message={t('common.loading')} />;
  if (failedLogout || !hasRecoveryConfirmation(user.name) || hasFloorLogoutFailureMarker()) {
    return <OwnerSignIn user={user} passwordOnly={passwordOnly} />;
  }
  return <RecoveryOutbox profile={profile} user={user} />;
}

const AuthGuard: React.FC<Props> = ({ children }) => {
  const { 
    checkAuth, 
    user, 
    isLoading: authLoading, 
    error: authError,
    fetchPosProfile,
    posProfile,
    requiresOutletSelection = false,
    availableOutlets = [],
    isLoading: configLoading,
    error: configError,
    hasAccess,
  } = useRootStore();

  // State to track if we're rechecking permissions
  const [isRechecking, setIsRechecking] = useState(false);
  const floorPadActive = isFloorPadActive();
  // Keep the gate mounted through async logout, including stale store renders before navigation.
  const [failedOnBoot, setFailedOnBoot] = useState<boolean | null>(null);
  useEffect(() => {
    let active = true;
    void readFloorLogoutFailureMarker().then(failed => { if (active) setFailedOnBoot(failed); });
    return () => { active = false; };
  }, []);
  const logoutFailed = failedOnBoot || hasFloorLogoutFailureMarker();
  const recovery = needsFloorReconciliation();

  // A surviving cookie on a paired tablet must not admit the previous waiter.
  const coldBoot = Boolean(
    floorPadActive && user && !user.provisional && posProfile
      && !hasFloorSessionMarker(user.name) && !logoutFailed && !isStaySignedIn(user, posProfile),
  );

  const showFloorPad = floorPadActive && (!user || user.provisional || coldBoot || logoutFailed);
  useFloorIdle(showFloorPad || logoutFailed || recovery ? null : user, posProfile);

  useEffect(() => {
    const onLogoutMarker = (event: StorageEvent) => {
      if (event.key === 'ury_floor_logout_failed') window.location.reload();
    };
    window.addEventListener('storage', onLogoutMarker);
    // Start auth check
    checkAuth();
    return () => window.removeEventListener('storage', onLogoutMarker);
  }, [checkAuth]);

  // Once we have a user, fetch POS profile
  useEffect(() => {
    if (user) {
      fetchPosProfile();
    }
  }, [user, fetchPosProfile]);

  // Keep root-store profile synced when an already-loaded session switches outlets in Header
  useEffect(() => {
    if (!user || !posProfile) return;
    const handleOutletSwitched = () => {
      void fetchPosProfile(true);
    };
    window.addEventListener('pos:outlet-switched', handleOutletSwitched);
    return () => window.removeEventListener('pos:outlet-switched', handleOutletSwitched);
  }, [user, posProfile, fetchPosProfile]);

  // Show loading state while either auth or config is loading
  if (failedOnBoot === null || authLoading || (user && configLoading) || isRechecking) {
    return (
      <div className="min-h-screen">
        <Spinner  message={t('common.loading')} />
      </div>
    );
  }

  if ((coldBoot || logoutFailed || recovery) && user && !user.provisional) {
    return <ColdBootSignOut key={JSON.stringify([posProfile?.name, user.name])} profile={posProfile} user={user} failedLogout={!!logoutFailed} passwordOnly={!!logoutFailed || !floorPadActive} />;
  }

  if (logoutFailed || recovery) return <FloorPinPad usernameOnly offline={user?.provisional} />;

  if (authError || configError) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-center">
          <div className="text-red-600 text-xl mb-4">⚠️</div>
          <h2 className="text-xl font-semibold text-gray-800 mb-2">{t('auth.access_denied')}</h2>
          <p className="text-gray-600">{authError || configError}</p>
        </div>
      </div>
    );
  }

  if (showFloorPad) {
    return <FloorPinPad offline={user?.provisional} />;
  }

  if (!user) {
    return <FloorPinPad usernameOnly />;
  }

  if (requiresOutletSelection && !posProfile) {
    return (
      <Suspense
        fallback={
          <div className="min-h-screen">
            <Spinner message={t('common.loading')} />
          </div>
        }
      >
        <LazyOutletPickerDialog
          open={true}
          mandatory={true}
          outlets={availableOutlets}
          onOpenChange={() => {}}
          onSwitched={async () => {
            await fetchPosProfile(true);
          }}
        />
      </Suspense>
    );
  }

  if (!posProfile) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-center">
          <div className="text-amber-600 text-xl mb-4">⚠️</div>
          <h2 className="text-xl font-semibold text-gray-800 mb-2">{t('auth.configuration_error')}</h2>
          <p className="text-gray-600">{t('auth.profile_not_found')}</p>
        </div>
      </div>
    );
  }

  if (!hasAccess) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-center">
          <div className="text-amber-600 text-xl mb-4">🔒</div>
          <h2 className="text-xl font-semibold text-gray-800 mb-2">{t('auth.permission_required')}</h2>
          <p className="text-gray-600">{t('auth.no_permission')}</p>
          <Button 
            variant="outline"
            className="mt-4"
            onClick={async () => {
              setIsRechecking(true);
              try {
                await fetchPosProfile(true); // Force refresh the POS profile
              } finally {
                setIsRechecking(false);
              }
            }}
          >
            <RefreshCw className="w-4 h-4 me-2" />
            {t('auth.recheck')}
          </Button>
        </div>
      </div>
    );
  }

  if (!hasFloorSessionMarker(user.name)) {
    return <UnconfirmedWorkspace key={user.name} user={user}>{children}</UnconfirmedWorkspace>;
  }
  return <>{children}</>;
};

export default AuthGuard; 