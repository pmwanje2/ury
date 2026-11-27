import { useState, useRef, useMemo } from 'react';
import { t } from '../i18n';
import { Sparkles, Star, TrendingUp } from 'lucide-react';
import { PLUKeypad } from '../components/PLUKeypad';
import TillCashDialog from '../components/TillCashDialog';
import { workstation } from '../lib/workstation';
import { isUnavailable } from '../lib/availability-api';
import Sidebar from '../components/Sidebar';
import OrderPanel from '../components/OrderPanel';
import ProductDialog from '../components/ProductDialog';
import MenuList from '../components/MenuList';
import PriceOverrideDialog from '../components/PriceOverrideDialog';
import { usePOSStore, type MenuItem, type OrderItem } from '../store/pos-store';
import { cn } from '@ury/ui';
import { Spinner } from '@ury/ui';
import InitialLoader from '../components/InitialLoader';
import { useMenuAvailability } from '../lib/use-menu-availability';
import { useRootStore } from '../store/root-store';
import { availabilityPermissions, type AvailabilityChange } from '@ury/core/availability';
import { setItemAvailability } from '../lib/availability-api';
import { SoldOutSheet } from '../components/SoldOutSheet';
import { isAuthRequiredError, overrideActionErrorMessage } from '../lib/override-api';
import ManagerApproval, { type ManagerApprovalResult } from '../components/ManagerApproval';
import { showToast } from '@ury/ui';

const EMPTY_MENU: Array<{ item: string }> = [];

function requiresManualPrice(item: { custom_is_open_priced?: number; custom_allow_zero_price?: number; price: number }) {
  return Number(item.custom_is_open_priced) === 1 ||
    (Number(item.custom_allow_zero_price) === 1 && Number(item.price) === 0);
}

function repeatedLine(row: OrderItem, price: number, catalog?: MenuItem): OrderItem {
  return {
    ...row, price, promo_rate: catalog?.promo_rate ?? null, promo_label: catalog?.promo_label ?? null,
    uniqueId: undefined, invoiceItemName: undefined, reservationLineKey: undefined, parentLineKey: undefined,
    fireAt: undefined, fireConfirmationPending: false,
  };
}

export default function POS({ onRoundSent, onCashierCheck, lastRound, checkDetail }: React.ComponentProps<typeof OrderPanel> & { checkDetail?: React.ReactNode; lastRound?: { invoice: string; lines: OrderItem[] } }) {
  const {
    categories, selectedCategory, setSelectedCategory, isOrderInteractionDisabled,
    quickFilter,
    setQuickFilter,
    setSelectedItem,
    addToOrder, addConfiguredItems,
    loading,
    error,
    isMenuInteractionDisabled,
    isInitializing,
    menuItems,
    posProfile,
    orderId,
    selectedOrderType,
    selectedTable,
    selectedCustomer,
    noOfPax,
    currency,
    loadTableOrder,
  } = usePOSStore();
  const layout = useMemo(() => workstation(posProfile?.custom_workstation_layout, currency || ''), [posProfile, currency]);
  const [page, setPage] = useState('');
  const [quantity, setQuantity] = useState(1);
  const [dialogQuantity, setDialogQuantity] = useState(1);
  const [noSale, setNoSale] = useState(false);
  const activePage = layout.pages.find(p => p.label === page);
  const openMenuItems = useMemo(
    () =>
      (menuItems ?? [])
        .filter(
          (item) =>
            Number((item as { custom_is_open_priced?: number }).custom_is_open_priced || 0) === 1 ||
            Number((item as { custom_allow_zero_price?: number }).custom_allow_zero_price || 0) === 1,
        )
        .map((item) => ({
          item: item.item || item.id,
          item_name: item.item_name || item.name,
          rate: item.price,
        })),
    [menuItems],
  );
  const [openItemDialog, setOpenItemDialog] = useState<{
    itemCode: string;
    itemName: string;
  } | null>(null);
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
      showToast.error(overrideActionErrorMessage(err));
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

  const handleItemClick = (item: any, count = quantity, direct = false) => {
    if (isMenuInteractionDisabled() || item?.unavailable_daypart) return;

    setQuantity(1);
    setDialogQuantity(count);
    if (requiresManualPrice(item)) {
      if (clickTimerRef.current) {
        clearTimeout(clickTimerRef.current);
        clickTimerRef.current = null;
      }
      clickCountRef.current = 0;
      setOpenItemDialog({
        itemCode: item.item || item.id,
        itemName: item.item_name || item.name || item.item,
      });
      return;
    }

    // Single tap on any item with required modifiers, auto-prompt modifiers, or combo choices
    // immediately opens the selector without adding an unconfigured item to the cart.
    if (item.has_required_modifiers || item.prompt_on_add || item.is_combo_meal) {
      if (clickTimerRef.current) {
        clearTimeout(clickTimerRef.current);
        clickTimerRef.current = null;
      }
      clickCountRef.current = 0;
      setSelectedItem(item);
      setIsDialogOpen(true);
      return;
    }

    if (direct || count > 1) {
      void addToOrder({ ...item, quantity: count });
      return;
    }
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

  const repeatRound = () => {
    if (!lastRound || lastRound.invoice !== orderId || isMenuInteractionDisabled() || isOrderInteractionDisabled()) return;
    const parents = lastRound.lines.filter(row => !row.isModifier && !row.parentLineKey);
    const catalog = new Map(menuItems.map(item => [item.item, item]));
    const groups: { parent: OrderItem; children: OrderItem[] }[] = [];
    for (const row of parents) {
      const item = catalog.get(row.item);
      if (!item || item.unavailable_daypart || isUnavailable(availabilityMap?.[row.item]) || requiresManualPrice(item)) {
        showToast.error(t('menu.unavailable'));
        return;
      }
      const children: OrderItem[] = [];
      for (const child of lastRound.lines.filter(line => line.parentLineKey && line.parentLineKey === row.reservationLineKey)) {
        const option = item.modifier_groups?.find(group => group.name === child.modifierGroup)
          ?.options.find(option => option.option_code === child.modifierOption);
        if (!option || option.available === false || option.price_mode === 'Unpriced Prep') {
          showToast.error(t('menu.unavailable'));
          return;
        }
        const price = Number(option.unit_price ?? option.upcharge_amount ?? 0);
        children.push(repeatedLine(child, price));
      }
      const parent = repeatedLine(row, item.price, item);
      parent.modifier_groups = item.modifier_groups;
      if (row.selectedVariant) {
        parent.selectedVariant = item.variants?.find(variant => variant.id === row.selectedVariant?.id);
        if (!parent.selectedVariant) {
          showToast.error(t('menu.unavailable'));
          return;
        }
      }
      if (row.selectedAddons?.length) {
        parent.selectedAddons = item.addons?.filter(addon => row.selectedAddons?.some(chosen => chosen.id === addon.id));
        if (parent.selectedAddons?.length !== row.selectedAddons.length) {
          showToast.error(t('menu.unavailable'));
          return;
        }
      }
      groups.push({parent, children});
    }
    for (const {parent, children} of groups) void addConfiguredItems(parent, children);
  };

  const QuickFilterButton = ({ filter, icon: Icon, label }: { 
    filter: 'all' | 'special';
    icon: React.ElementType;
    label: string;
  }) => (
    <button
      onClick={() => {
        setQuickFilter(filter);
        if (filter === 'all') {
          setSelectedCategory('');
          setPage('');
        }
      }}
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
      <div className={layout.pages.length || checkDetail ? 'hidden' : 'hidden lg:contents'}>
        <Sidebar disabled={isMenuInteractionDisabled()} />
      </div>
      <div inert={checkDetail ? true : undefined} className={cn("flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden", checkDetail && "hidden opacity-60 lg:flex")}>
        <div className="p-2 bg-background text-foreground border-b border-border">
          <div className="flex items-center gap-2">
            <PLUKeypad disabled={isMenuInteractionDisabled() || isOrderInteractionDisabled?.()} multiplier={quantity} onQuantity={layout.keys.includes('quantity') ? setQuantity : undefined}
              onLookup={(code, count) => {
                const item = menuItems?.find(row => row.item === code);
                if (!item || count > 99 || isMenuInteractionDisabled() || isOrderInteractionDisabled?.() ||
                    item.unavailable_daypart || isUnavailable(availabilityMap?.[code])) return false;
                handleItemClick(item, count, true);
                return true;
              }} />
            {layout.keys.includes('repeat') && lastRound && lastRound.invoice === orderId &&
              <button className="min-h-11 rounded border px-2" onClick={repeatRound}>{t('workstation.repeat')}</button>}
            {layout.keys.includes('noSale') && !!posProfile?.enable_cash_movements &&
              <button className="min-h-11 rounded border px-2" onClick={() => setNoSale(true)}>{t('till_cash.no_sale')}</button>}
            <div className={cn('flex gap-1 overflow-x-auto', !layout.pages.length && 'lg:hidden')}>
              {(layout.pages.length ? layout.pages.map(p => ({ name: p.label, label: p.label })) : categories || []).map(p =>
                <button key={p.name} className="min-h-11 shrink-0 rounded border px-2 aria-pressed:bg-muted"
                  aria-pressed={layout.pages.length ? page === p.name : selectedCategory === p.name}
                  onClick={() => layout.pages.length ? setPage(page === p.name ? '' : p.name) : setSelectedCategory(selectedCategory === p.name ? '' : p.name)}>{p.label}</button>)}
            </div>
          </div>
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
              {openMenuItems.length > 0 && (
                <button
                  type="button"
                  data-testid="register-open-item-btn"
                  disabled={isMenuInteractionDisabled()}
                  onClick={() => {
                    setDialogQuantity(1);
                    setOpenItemDialog({
                      itemCode: openMenuItems[0].item,
                      itemName: openMenuItems[0].item_name,
                    });
                  }}
                  className={cn(
                    'flex min-h-11 shrink-0 items-center gap-2 rounded-full bg-amber-50 px-3.5 py-2 text-sm font-medium text-amber-800 border border-amber-200 transition-colors hover:bg-amber-100',
                    isMenuInteractionDisabled() && 'opacity-50 cursor-not-allowed pointer-events-none',
                  )}
                >
                  <Sparkles className="w-4 h-4" />
                  {t('privileged_actions.open_item')}
                </button>
              )}
              {branch && company && itemAvailabilityActions && <SoldOutSheet key={branch}
                branch={branch} company={company} availability={availabilityMap} actions={itemAvailabilityActions} />}
            </div>
          </div>
        </div>

        <MenuList pageItems={layout.pages.length ? (activePage?.items ?? menuItems?.map(row => row.item)) : undefined} onItemClick={handleItemClick} availabilityMap={availabilityMap} availabilityStale={availabilityStale}
          itemAvailabilityActions={itemAvailabilityActions} />
      </div>
      {checkDetail ? <section className="h-full w-full overflow-y-auto bg-background lg:w-1/2">{checkDetail}</section>
        : <OrderPanel onRoundSent={onRoundSent} onCashierCheck={onCashierCheck} availabilityMap={availabilityMap} />}
      {noSale && <TillCashDialog initialTab="No Sale" open onOpenChange={setNoSale} />}
      {isDialogOpen && <ProductDialog initialQuantity={dialogQuantity} onClose={() => setIsDialogOpen(false)} />}
      {openItemDialog && posProfile && (
        <PriceOverrideDialog
          open
          onOpenChange={(next) => !next && setOpenItemDialog(null)}
          mode="open_item"
          posProfile={posProfile.name}
          invoiceName={orderId || ''}
          itemCode={openItemDialog.itemCode}
          itemName={openItemDialog.itemName}
          initialQuantity={dialogQuantity}
          currentRate={0}
          currency={currency || posProfile.currency || 'UGX'}
          openItems={openMenuItems}
          orderType={selectedOrderType}
          table={selectedTable}
          customer={selectedCustomer?.id || null}
          noOfPax={noOfPax}
          onSuccess={async () => {
            if (selectedTable && typeof loadTableOrder === 'function') {
              await loadTableOrder(selectedTable);
            }
          }}
        />
      )}
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
