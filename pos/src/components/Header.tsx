import { useTillLogout } from './TillLogout';
import { useState, useEffect, useRef } from 'react';
import { t, tUser } from '../i18n';
import { useLocation } from 'react-router-dom';
import {
  Command,
  User,
  ChevronDown,
  Monitor,
  LogOut,
  RefreshCw,
  Lock,
  Wallet,
  KeyRound,
  Clock,
  Receipt,
} from 'lucide-react';
import { orderTypeLabel } from '../data/order-types';
import { Button, Input } from '@ury/ui';
import { useRootStore } from '../store/root-store';
import { usePOSStore } from '../store/pos-store';
import type { RootState } from '../store/root-store';
import { call, resolveBrand, isPrintStatusDisabled } from '@ury/core';
import { clearCashierStorage } from '../offline/cashier';
import { useOutbox } from '../offline/use-outbox';
import { showToast } from '@ury/ui';
import { PrinterStatusButton } from './PrinterWatch';
import PairTabletDialog from './PairTabletDialog';
import StaffPinsDialog from './StaffPinsDialog';
import TillCashDialog from './TillCashDialog';
import ApprovalPinDialog from './ApprovalPinDialog';

const Header = () => {
  const brand = resolveBrand({ name: 'Maximus POS', logo: '/assets/ury/Images/maximus-mark.svg' });
  const [showUserMenu, setShowUserMenu] = useState(false);
  const userMenuRef = useRef<HTMLDivElement>(null);
  const user = useRootStore((state: RootState) => state.user);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const location = useLocation();
  const {
    searchQuery,
    setSearchQuery,
    setShowVoluntaryClosing,
    posProfile,
    selectedOrderType,
    activeServingPeriod,
    activeTaxSummary,
  } = usePOSStore();
  const taxBadgeText =
    activeTaxSummary?.label ||
    activeTaxSummary?.taxes_and_charges ||
    activeTaxSummary?.tax_category ||
    null;
  const showStatusStrip = Boolean(
    activeServingPeriod?.period_name || selectedOrderType || taxBadgeText,
  );
  const outbox = useOutbox(posProfile, user);
  const printStatusDisabled = isPrintStatusDisabled(posProfile);
  const { orderSearchQuery, setOrderSearchQuery } = useRootStore();
  const [orderSearchInput, setOrderSearchInput] = useState(orderSearchQuery);
  const [showStaffPins, setShowStaffPins] = useState(false);
  const [showPairTablet, setShowPairTablet] = useState(false);
  const [showTillCash, setShowTillCash] = useState(false);
  const [showApprovalPin, setShowApprovalPin] = useState(false);
  const [canManagePins, setCanManagePins] = useState(false);
  const posProfileName = posProfile?.name;
  const cashMovementsEnabled = Number(posProfile?.enable_cash_movements) === 1;

  // Floor tablet setup follows native POS Profile write permission; fail closed.
  useEffect(() => {
    let cancelled = false;
    setCanManagePins(false);
    if (!posProfileName) return () => { cancelled = true; };
    call.get('frappe.client.has_permission', {
      doctype: 'POS Profile', docname: posProfileName, perm_type: 'write',
    }).then((response) => {
      if (!cancelled) {
        setCanManagePins((response as { message?: { has_permission?: unknown } })?.message?.has_permission === true);
      }
    }).catch(() => {
      if (!cancelled) setCanManagePins(false);
    });
    return () => { cancelled = true; };
  }, [posProfileName]);

  // Determine placeholder and handlers based on route
  let searchPlaceholder = t('header.search_placeholder_default');
  let searchValue: string | undefined = undefined;
  let searchOnChange: ((e: React.ChangeEvent<HTMLInputElement>) => void) | undefined = undefined;
  if (location.pathname === '/orders') {
    searchPlaceholder = t('header.search_placeholder_orders');
    searchValue = orderSearchInput;
    searchOnChange = (e) => setOrderSearchInput(e.target.value);
  } else if (location.pathname === '/reservations') {
    searchPlaceholder = t('header.search_placeholder_reservations');
    searchValue = searchQuery;
    searchOnChange = (e) => setSearchQuery(e.target.value);
  } else if (location.pathname === '/register') {
    searchPlaceholder = t('header.search_placeholder_menu');
    searchValue = searchQuery;
    searchOnChange = (e) => setSearchQuery(e.target.value);
  }

  // Debounce order search
  useEffect(() => {
    if (location.pathname !== '/orders') return;
    const handler = setTimeout(() => {
      setOrderSearchQuery(orderSearchInput);
    }, 300);
    return () => clearTimeout(handler);
  }, [orderSearchInput, setOrderSearchQuery, location.pathname]);

  // Keep input in sync with store (if cleared elsewhere)
  useEffect(() => {
    if (location.pathname === '/orders') {
      setOrderSearchInput(orderSearchQuery);
    }
  }, [location.pathname, orderSearchQuery]);

  // Handle clicks outside of menus
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (userMenuRef.current && !userMenuRef.current.contains(event.target as Node)) {
        setShowUserMenu(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
    };
  }, []);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        searchInputRef.current?.focus();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, []);

  const handleUserMenuToggle = () => {
    setShowUserMenu(!showUserMenu);
  };

  const { loggingOut, handleLogout } = useTillLogout();

  const handleClearCache = async () => {
    try {
      await clearCashierStorage();
      window.location.reload();
    } catch (error) {
      showToast.error(error instanceof Error ? error.message : t('errors.failed_logout'));
    }
  };

  const handleCloseShift = () => {
    setShowUserMenu(false);
    setShowVoluntaryClosing(true);
  };

  return (
    <>
    <header className="sticky top-0 z-40 bg-header text-header-foreground border-b border-header-border">
      <div className="flex items-center justify-between gap-2 h-12 px-2 sm:px-4 md:px-6">
        {/* Logo */}
        <div className="flex shrink-0 items-center">
          <div className="flex items-center gap-3">
            <img
              src={brand.logo}
              alt={brand.name}
              className="h-8 w-auto"
            />
          </div>
        </div>

        {/* Search Bar */}
        {location.pathname !== '/dashboard' ? (
          <div className="min-w-0 px-2 sm:px-3 py-1 h-7 flex-1 flex items-center max-w-2xl mx-1 sm:mx-4 md:mx-8 bg-gray-50 hover:bg-gray-100 border border-input rounded-md text-sm">
            <Input
              ref={searchInputRef}
              placeholder={searchPlaceholder}
              className="h-fit min-w-0 p-0 w-full bg-transparent border-0 focus:outline-none focus-visible:ring-0 focus-visible:ring-offset-0"
              value={searchValue}
              onChange={searchOnChange}
            />
            <div className="hidden sm:flex items-center gap-2 shrink-0 text-gray-400">
              <Command className="w-4 h-4" />
              <span>K</span>
            </div>
          </div>
        ) : (
          <div className="flex-1" />
        )}

        {/* Right side actions */}
        <div className="flex shrink-0 items-center gap-2 sm:gap-4">
          {cashMovementsEnabled && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="flex items-center gap-1.5 h-7"
              disabled={outbox.offline || outbox.replaying}
              onClick={() => setShowTillCash(true)}
            >
              <Wallet className="w-4 h-4" />
              <span>{t('header.till_cash')}</span>
            </Button>
          )}
          {/* Printer status (fail-closed when the watch app is absent) */}
          {!printStatusDisabled && <PrinterStatusButton />}
          {/* User menu */}
          <div className="relative min-w-0" ref={userMenuRef}>
            <Button
              onClick={handleUserMenuToggle}
              variant="ghost"
              className="flex items-center gap-1.5 sm:gap-2 h-7 px-1.5 sm:px-3 text-header-user hover:text-header-title"
            >
              <div className="w-6 h-6 shrink-0 bg-primary-500 rounded-full flex items-center justify-center">
                <User className="w-3.5 h-3.5 text-white" />
              </div>
              <span className="max-w-[7rem] sm:max-w-none truncate text-sm font-medium">{tUser(user?.full_name) || t('header.user_fallback')}</span>
              <ChevronDown className="w-4 h-4 shrink-0" />
            </Button>

            {/* User dropdown */}
            {showUserMenu && (
              <div className="absolute end-0 mt-2 w-56 bg-white rounded-lg shadow-lg border border-border z-50">
                <div className="p-4 border-b border-border">
                  <p className="text-sm font-medium text-gray-900">{tUser(user?.full_name) || t('header.user_fallback')}</p>
                  <p className="text-sm text-gray-500">{user?.name || ''}</p>
                </div>
                <div className="py-2">
                  <Button
                    variant="ghost"
                    className="flex justify-start items-center w-full px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 transition-colors"
                    disabled={outbox.offline || outbox.replaying || outbox.rows.length > 0}
                    onClick={handleCloseShift}
                  >
                    <Lock className="w-4 h-4 me-3" />
                    {t('header.close_shift')}
                  </Button>
                  <Button
                    variant="ghost"
                    className="flex justify-start items-center w-full px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 transition-colors"
                    onClick={() => window.location.href = '/ury/dashboard'}
                  >
                    <Monitor className="w-4 h-4 me-3" />
                    {t('header.switch_to_dashboard')}
                  </Button>
                  <Button
                    variant="ghost"
                    className="flex justify-start items-center w-full px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 transition-colors"
                    onClick={() => { setShowUserMenu(false); setShowApprovalPin(true); }}
                  >
                    <KeyRound className="w-4 h-4 me-3" />
                    {t('header.my_approval_pin')}
                  </Button>
                  <Button
                    variant="ghost"
                    className="flex justify-start items-center w-full px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 transition-colors"
                    onClick={handleClearCache}
                  >
                    <RefreshCw className="w-4 h-4 me-3" />
                    {t('header.clear_cache')}
                  </Button>
                  {canManagePins && (
                    <>
                      <Button
                        variant="ghost"
                        className="flex justify-start items-center w-full px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 transition-colors"
                        onClick={() => { setShowUserMenu(false); setShowPairTablet(true); }}
                      >
                        <Monitor className="w-4 h-4 me-3" />
                        {t('header.pair_tablet')}
                      </Button>
                      <Button
                        variant="ghost"
                        className="flex justify-start items-center w-full px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 transition-colors"
                        onClick={() => { setShowUserMenu(false); setShowStaffPins(true); }}
                      >
                        <User className="w-4 h-4 me-3" />
                        {t('header.staff_pins')}
                      </Button>
                    </>
                  )}
                  <Button
                    variant="ghost"
                    className="flex justify-start items-center w-full px-4 py-2 text-sm text-red-600 hover:bg-red-50 hover:text-red-700 transition-colors"
                    disabled={loggingOut}
                    onClick={handleLogout}
                  >
                    <LogOut className="w-4 h-4 me-3" />
                    {t('header.logout')}
                  </Button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
      {showStatusStrip && (
        <div
          data-testid="pos-status-strip"
          className="flex flex-wrap items-center gap-1.5 border-t border-header-border/60 bg-gray-50/80 px-2 py-1 text-xs text-gray-700 sm:px-4 md:px-6"
        >
          {activeServingPeriod?.period_name && (
            <span
              data-testid="status-serving-period"
              className="inline-flex items-center gap-1 rounded-full bg-amber-100/90 px-2 py-0.5 font-medium text-amber-900"
            >
              <Clock className="h-3 w-3 shrink-0" aria-hidden />
              <span>
                {activeServingPeriod.period_name}
                {activeServingPeriod.start_time && activeServingPeriod.end_time
                  ? ` (${activeServingPeriod.start_time}–${activeServingPeriod.end_time})`
                  : ''}
              </span>
            </span>
          )}
          {selectedOrderType && (
            <span
              data-testid="status-order-type"
              className="inline-flex items-center rounded-full bg-blue-100/90 px-2 py-0.5 font-medium text-blue-900"
            >
              {orderTypeLabel(selectedOrderType)}
            </span>
          )}
          {taxBadgeText && (
            <span
              data-testid="status-tax-badge"
              className="inline-flex items-center gap-1 rounded-full bg-emerald-100/90 px-2 py-0.5 font-medium text-emerald-900"
            >
              <Receipt className="h-3 w-3 shrink-0" aria-hidden />
              <span>{taxBadgeText}</span>
            </span>
          )}
        </div>
      )}
    </header>
    {cashMovementsEnabled && posProfileName && (
      <TillCashDialog open={showTillCash} onOpenChange={setShowTillCash} />
    )}
    <ApprovalPinDialog
      open={showApprovalPin}
      onOpenChange={setShowApprovalPin}
      posProfile={posProfileName}
      canManagePins={canManagePins}
    />
    {canManagePins && posProfileName && (
      <>
        <StaffPinsDialog open={showStaffPins} onOpenChange={setShowStaffPins} />
        <PairTabletDialog open={showPairTablet} onOpenChange={setShowPairTablet} posProfile={posProfileName} />
      </>
    )}
    </>
  );
};

export default Header; 