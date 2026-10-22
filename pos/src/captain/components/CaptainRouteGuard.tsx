import React from 'react';
import { Button, Spinner } from '@ury/ui';
import { t } from '../../i18n';
import { floorSignOut } from '../../lib/floor-session';
import { useCaptainContext } from '../hooks/useCaptainContext';

interface Props {
  children: React.ReactNode;
}

/**
 * Capability-gated route wrapper for `/order*`, following the loading /
 * error / denial pattern established by `AuthGuard`
 * (`pos/src/components/AuthGuard.tsx`).
 *
 * Gates on `capabilities.canTakeTableOrders` (from `derivePOSCapabilities`,
 * via `useCaptainContext`) — the client-side UX hint for
 * `!role_restricted_for_table_order`. This is NOT the security boundary:
 * every mutation must still be re-validated server-side per PLAN.md §9.
 *
 * NOTE: this guard runs *inside* `AuthGuard`/`POSOpeningProvider`
 * (`pos/src/App.tsx`). `AuthGuard` admits URY POS roles, including Captain
 * (`pos/src/store/slices/config-slice.ts`). After `canTakeTableOrders`, this
 * guard requires the branch till to be open (`openingState.pos_open` from
 * `get_captain_context`). A null opening state blocks. This is a UX hint:
 * the server still validates the order.
 */
const CaptainRouteGuard: React.FC<Props> = ({ children }) => {
  const { capabilities, openingState, isLoading, error, refetch } = useCaptainContext();

  if (isLoading) {
    return (
      <div className="min-h-screen">
        <Spinner message="Loading captain context..." />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-center">
          <div className="text-red-600 text-xl mb-4">⚠️</div>
          <h2 className="text-xl font-semibold text-gray-800 mb-2">Unable to load Captain context</h2>
          <p className="text-gray-600">{error}</p>
        </div>
      </div>
    );
  }

  if (!capabilities?.canTakeTableOrders) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-center">
          <div className="text-amber-600 text-xl mb-4">🔒</div>
          <h2 className="text-xl font-semibold text-gray-800 mb-2">Not permitted</h2>
          <p className="text-gray-600">
            You do not have permission to take table orders.
          </p>
        </div>
      </div>
    );
  }

  if (openingState?.pos_open !== true) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-center">
          <div className="text-amber-600 text-xl mb-4">🔒</div>
          <h2 className="text-xl font-semibold text-gray-800 mb-2">Till not open</h2>
          <p className="text-gray-600">{t('captain.till_not_open')}</p>
          <div className="mt-4 flex items-center justify-center gap-3">
            <Button type="button" onClick={() => { void refetch(); }}>
              {t('pos_opening.retry')}
            </Button>
            <Button type="button" variant="outline" onClick={() => { void floorSignOut(); }}>
              {t('header.switch_waiter')}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return <>{children}</>;
};

export default CaptainRouteGuard;
