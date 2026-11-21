import React from 'react';
import { Button, Spinner } from '@ury/ui';
import { useCaptainContext } from '../hooks/useCaptainContext';
import ServiceRequestPanel from './ServiceRequestPanel';
import ServiceRequestAlert from '../../components/ServiceRequestAlert';
import { t } from '../../i18n';

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
 * (`pos/src/store/slices/config-slice.ts`), and `POSOpeningProvider` does not
 * gate `/order*`. After `canTakeTableOrders`, this guard requires the branch
 * till to be open (`openingState.pos_open` from `get_captain_context`); a
 * missing opening state blocks. This is a UX hint: the server still
 * validates the order.
 */
const CaptainRouteGuard: React.FC<Props> = ({ children }) => {
  const { capabilities, branch, openingState, isLoading, error, refetch } = useCaptainContext();

  if (isLoading) {
    return (
      <div className="min-h-screen">
        <Spinner message={t('captain.loading_context')} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-center">
          <div className="text-red-600 text-xl mb-4">⚠️</div>
          <h2 className="text-xl font-semibold text-gray-800 mb-2">{t('captain.unable_load_context')}</h2>
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
          <h2 className="text-xl font-semibold text-gray-800 mb-2">{t('captain.not_permitted')}</h2>
          <p className="text-gray-600">
            {t('captain.no_permission_take_orders')}
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
          <p className="text-gray-600">{t('captain.till_not_open')}</p>
          <div className="mt-4 flex items-center justify-center">
            <Button type="button" onClick={() => { void refetch(); }}>
              {t('pos_opening.retry')}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="sticky top-0 z-30">
        <ServiceRequestAlert />
      </div>
      {children}
      <ServiceRequestPanel branch={branch} />
    </>
  );
};

export default CaptainRouteGuard;
