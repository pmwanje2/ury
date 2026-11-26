import React, { Suspense, useEffect, useState } from 'react';
import { useRootStore } from '../store/root-store';
import { Button } from '@ury/ui';
import { Spinner } from '@ury/ui';
import { RefreshCw } from 'lucide-react';
import { t } from '../i18n'
import FloorPinPad from './FloorPinPad';
import {
  floorSignOut,
  hasFloorLogoutFailureMarker,
  hasFloorSessionMarker,
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
  const [isColdBootSigningOut, setIsColdBootSigningOut] = useState(false);
  const floorPadActive = isFloorPadActive();
  const logoutFailed = hasFloorLogoutFailureMarker();

  // A surviving cookie on a paired tablet must not admit the previous waiter.
  const coldBoot = Boolean(
    floorPadActive && user && !user.provisional && posProfile
      && !hasFloorSessionMarker() && !logoutFailed && !isStaySignedIn(user, posProfile),
  );

  const showFloorPad = floorPadActive && (!user || user.provisional || coldBoot || isColdBootSigningOut || logoutFailed);
  useFloorIdle(showFloorPad ? null : user, posProfile);

  useEffect(() => {
    // Start auth check
    checkAuth();
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

  useEffect(() => {
    if (!coldBoot || isColdBootSigningOut) return;
    setIsColdBootSigningOut(true);
    void floorSignOut();
  }, [coldBoot, isColdBootSigningOut]);

  // Show loading state while either auth or config is loading
  if (authLoading || (user && configLoading) || isRechecking) {
    return (
      <div className="min-h-screen">
        <Spinner  message={t('common.loading')} />
      </div>
    );
  }

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

  return <>{children}</>;
};

export default AuthGuard; 