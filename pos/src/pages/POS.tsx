import { useState, useRef } from 'react';
import { t } from '../i18n';
import { Star, TrendingUp } from 'lucide-react';
import Sidebar from '../components/Sidebar';
import OrderPanel from '../components/OrderPanel';
import ProductDialog from '../components/ProductDialog';
import MenuList from '../components/MenuList';
import { usePOSStore } from '../store/pos-store';
import { cn } from '@ury/ui';
import { Spinner } from '@ury/ui';
import InitialLoader from '../components/InitialLoader';
import { useMenuAvailability } from '../lib/use-menu-availability';
import { useRootStore } from '../store/root-store';
import { availabilityPermissions, type AvailabilityChange } from '@ury/core/availability';
import { setItemAvailability } from '../lib/availability-api';
import { SoldOutSheet } from '../components/SoldOutSheet';
import { isAuthRequiredError, overrideErrorMessage } from '../lib/override-api';
import ManagerApproval, { type ManagerApprovalResult } from '../components/ManagerApproval';
import { showToast } from '@ury/ui';

const EMPTY_MENU: Array<{ item: string }> = [];

export default function POS({ onCashierCheck }: React.ComponentProps<typeof OrderPanel>) {
  const {
    quickFilter,
    setQuickFilter,
    setSelectedItem,
    addToOrder,
    loading,
    error,
    isMenuInteractionDisabled,
    isInitializing,
    menuItems,
    posProfile,
  } = usePOSStore();
  const { availabilityMap, availabilityStale, refetchAll } = useMenuAvailability(menuItems ?? EMPTY_MENU, posProfile?.branch, posProfile?.company);
  const user = useRootStore(state => state.user);
  const { canMark, canRestore, canRequestRestoreWithPin } = availabilityPermissions(user?.roles);
  const branch = posProfile?.branch;
  const company = posProfile?.company;
  const [pending86Approval, setPending86Approval] = useState<{
    branch: string;
    item: string;
    change: AvailabilityChange;
  } | null>(null);

  const runAvailabilityUpdate = async (
    targetBranch: string,
    item: string,
    change: AvailabilityChange,
    approval?: ManagerApprovalResult,
  ) => {
    try {
      await setItemAvailability(targetBranch, item, change, {
        approvalToken: approval?.token,
        reason: approval?.reason,
        reasonNote: approval?.note,
        posProfile: posProfile?.name,
      });
      await refetchAll();
    } catch (err) {
      if (!approval?.token && isAuthRequiredError(err)) {
        setPending86Approval({ branch: targetBranch, item, change });
        return;
      }
      showToast.error(overrideErrorMessage(err));
      throw err;
    }
  };

  const itemAvailabilityActions = canMark && branch ? (item: string) => ({
    canRestore: canRestore || canRequestRestoreWithPin,
    update: async (change: AvailabilityChange) => {
      await runAvailabilityUpdate(branch, item, change);
    },
  }) : undefined;
  
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const clickTimerRef = useRef<NodeJS.Timeout | null>(null);
  const clickCountRef = useRef(0);

  const handleItemClick = (item: any) => {
    if (isMenuInteractionDisabled()) return;
    
    clickCountRef.current += 1;
    
    if (clickTimerRef.current) {
      clearTimeout(clickTimerRef.current);
    }

    clickTimerRef.current = setTimeout(() => {
      if (clickCountRef.current === 1) {
        // Single click - add to cart
        addToOrder({ ...item, quantity: 1 });
      } else if (clickCountRef.current === 2) {
        // Double click - open dialog
        setSelectedItem(item);
        setIsDialogOpen(true);
      }
      clickCountRef.current = 0;
    }, 250); // 250ms threshold for double click
  };

  const QuickFilterButton = ({ filter, icon: Icon, label }: { 
    filter: 'all' | 'special';
    icon: React.ElementType;
    label: string;
  }) => (
    <button
      onClick={() => setQuickFilter(filter)}
      className={cn(
        'flex items-center gap-2 px-3 py-1.5 rounded-full text-sm font-medium transition-colors',
        quickFilter === filter
          ? 'bg-blue-100 text-blue-700'
          : 'bg-gray-100 text-gray-700 hover:bg-gray-200',
        isMenuInteractionDisabled() && 'opacity-50 cursor-not-allowed pointer-events-none'
      )}
      disabled={isMenuInteractionDisabled()}
    >
      <Icon className="w-4 h-4" />
      {label}
    </button>
  );

  if (isInitializing) {
    return <InitialLoader />;
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-screen">
        <div className="text-center">
          <p className="text-xl font-semibold text-red-600 mb-2">{t('pos.failed_to_load')}</p>
          <p className="text-gray-600">{error}</p>
          <button 
            onClick={() => window.location.reload()}
            className="mt-4 px-4 py-2 bg-primary-600 text-white rounded-md hover:bg-primary-700"
          >
            {t('common.retry')}
          </button>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Spinner message={t('common.loading_menu_items')} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center">
          <p className="text-lg font-medium text-red-600">{t('common.error_loading_menu_items')}</p>
          <p className="text-sm text-gray-500 mt-2">{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="absolute inset-0 flex min-h-0 flex-col overflow-hidden lg:flex-row">
      <div className="hidden lg:contents">
        <Sidebar disabled={isMenuInteractionDisabled()} />
      </div>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <div className="p-4 bg-white border-b border-border">
          <div className="max-w-screen-xl mx-auto space-y-3">
            <div className="flex items-center gap-2 overflow-x-auto overflow-y-hidden">
              {/* <SearchBar
                value={searchQuery}
                onChange={setSearchQuery}
                onVisibilityChange={setShowSearch}
                isVisible={showSearch}
                disabled={isMenuInteractionDisabled()}
              /> */}
              
              <QuickFilterButton filter="all" icon={Star} label={t('common.all')} />
              <QuickFilterButton filter="special" icon={TrendingUp} label={t('menu.special_items')} />
              {branch && company && itemAvailabilityActions && <SoldOutSheet key={branch}
                branch={branch} company={company} availability={availabilityMap} actions={itemAvailabilityActions} />}
            </div>
          </div>
        </div>

        <MenuList onItemClick={handleItemClick} availabilityMap={availabilityMap} availabilityStale={availabilityStale}
          itemAvailabilityActions={itemAvailabilityActions} />
      </div>
      <OrderPanel onCashierCheck={onCashierCheck} availabilityMap={availabilityMap} />
      {isDialogOpen && <ProductDialog onClose={() => setIsDialogOpen(false)} />}
      {pending86Approval && posProfile && (
        <ManagerApproval
          open
          posProfile={posProfile.name}
          privilege="item_86_restore"
          title={t('privileged_actions.restore_86')}
          summaryLines={[
            `${pending86Approval.item} (${pending86Approval.branch})`,
            pending86Approval.change.status || String(pending86Approval.change.count ?? pending86Approval.change.reset_policy ?? ''),
          ]}
          reference={{ doctype: 'URY Item Availability', name: `${pending86Approval.branch}:${pending86Approval.item}` }}
          claim={{
            action: 'item_86_restore',
            branch: pending86Approval.branch,
            item_code: pending86Approval.item,
            status: pending86Approval.change.status || '',
            count: pending86Approval.change.count ?? '',
            reset_policy: pending86Approval.change.reset_policy || '',
          }}
          onApproved={(res) => {
            const pending = pending86Approval;
            setPending86Approval(null);
            void runAvailabilityUpdate(pending.branch, pending.item, pending.change, res);
          }}
          onCancel={() => setPending86Approval(null)}
        />
      )}
    </div>
  );
}
