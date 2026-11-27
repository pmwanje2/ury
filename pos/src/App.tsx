import { BrowserRouter as Router, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import AppLayout from './components/AppLayout';
import Orders from './pages/Orders';
import POS from './pages/POS';
import Table from './pages/Table';
import Reservations from './pages/Reservations';
import Dashboard from './pages/Dashboard';
import Settings from './pages/Settings';
import OpenEntries from './pages/OpenEntries';
import AuthGuard from './components/AuthGuard';
import POSOpeningProvider from './components/POSOpeningProvider';
import ScreenSizeProvider from './components/ScreenSizeProvider';
import KotAlertListener from './components/KotAlertListener';
import { PrinterWatchProvider } from './components/PrinterWatch';
import CaptainRouteGuard from './captain/components/CaptainRouteGuard';
import CaptainTables from './captain/pages/CaptainTables';
import CaptainOrder from './captain/pages/CaptainOrder';
import { ToastProvider } from '@ury/ui';
import { homePath } from './lib/home-route';
import { useRootStore } from './store/root-store';
import { usePOSStore, type OrderItem } from './store/pos-store';
import { useEffect, useRef, useState } from 'react';
import { installDrawerKickListener } from './lib/drawer-kick';
import { getActiveLanguage, getActiveDirection } from './i18n';
import { ActiveReportProvider } from './components/chat/ActiveReportContext';
import ChatWidget, {
  ChatWidgetRefProvider,
  AiEnabledProvider,
  type ChatWidgetHandle,
} from './components/chat/ChatWidget';

function useAiSettings() {
  // Fail-closed: no AI surface renders until the backend explicitly says
  // {enabled: true}. Any fetch failure (network, permissions, etc.) leaves
  // this false rather than defaulting open.
  const [aiEnabled, setAiEnabled] = useState(false);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const { call } = await import('@ury/core');
        const res = await call.get('ury.ury.api.ury_ai_settings.get_ai_settings');
        const data = res?.message ?? res;
        if (!cancelled && data?.enabled === true) {
          setAiEnabled(true);
        }
      } catch (err) {
        console.error('Error fetching AI settings:', err);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  return aiEnabled;
}


function RoutedScreenSizeProvider({ children }: { children: React.ReactNode }) {
  const { pathname } = useLocation();
  return <ScreenSizeProvider pathname={pathname}>{children}</ScreenSizeProvider>;
}

function RegisterRoute() {
  const user = useRootStore(s => s.user?.name);
  const profile = usePOSStore().posProfile?.name;
  const context = user + '/' + profile;
  return <RegisterWorkstation key={context} />;
}

function RegisterWorkstation() {
  const [round, setRound] = useState<{ invoice: string; lines: OrderItem[] }>();
  const [check, setCheck] = useState(false);
  const detail = check ? <Orders onRegister={() => setCheck(false)} /> : null;
  return (
    <POS
      checkDetail={detail}
      lastRound={round}
      onRoundSent={(invoice, lines) => setRound({ invoice, lines })}
      onCashierCheck={invoice => {
        setCheck(true);
        void useRootStore.getState().selectOrder(invoice);
      }}
    />
  );
}

function App() {
  const user = useRootStore((state) => state.user);
  const {
    initializeApp, posProfile
  } = usePOSStore();
  const chatRef = useRef<ChatWidgetHandle>(null);
  const aiEnabled = useAiSettings();
  const provisional = useRootStore((state) => state.user?.provisional);

  useEffect(() => {
    initializeApp();
  }, [initializeApp]);

  useEffect(() => {
    return installDrawerKickListener({
      getProfile: () => {
        const state = usePOSStore.getState();
        const profile = state.posProfile;
        if (!profile) return null;
        return {
          pos_profile: profile.pos_profile || profile.name,
          name: profile.name,
          print_type: profile.print_type,
          qz_host: profile.qz_host,
          printer: profile.printer,
          custom_drawer_pin: profile.custom_drawer_pin,
          custom_drawer_kick_hex: profile.custom_drawer_kick_hex,
          custom_auto_kick_on_cash_tender: profile.custom_auto_kick_on_cash_tender,
          drawer_opening_modes: profile.drawer_opening_modes,
        };
      },
    });
  }, []);

  useEffect(() => {
    const lang = getActiveLanguage();
    // Direction comes from the locale file's _meta.direction, not a hardcoded language list.
    const isRtl = getActiveDirection() === 'rtl';
    document.documentElement.dir = isRtl ? 'rtl' : 'ltr';
    document.documentElement.lang = lang || 'en';
  }, []);
  return (
    <PrinterWatchProvider>
      <ToastProvider />
      <KotAlertListener />
      <Router basename="/pos">
        <RoutedScreenSizeProvider>
          <AuthGuard>
            <POSOpeningProvider>
              {/*
                PLAN.md item 4: ActiveReportProvider + the floating ChatWidget
                are mounted once here, wrapping the whole routed app shell, so
                every page under both the pos/ dashboard routes and the
                captain "Order" routes shares one chat instance and one
                active-report context. ChatWidgetRefProvider hands the same
                instance's ref down to any page's ⌘K AskBar (see
                components/chat/AskBar.tsx) without prop-threading it through
                AppLayout.
              */}
              <ActiveReportProvider>
                <AiEnabledProvider enabled={aiEnabled}>
                  <ChatWidgetRefProvider chatRef={chatRef}>
                    <Routes>
                      <Route element={<AppLayout />}>
                        <Route index element={<Navigate to={provisional ? "/register" : homePath(user, posProfile)} replace />} />
                        <Route path="/dashboard" element={<Dashboard />} />
                        <Route path="/register" element={<RegisterRoute />} />
                        <Route path="/tables" element={<Table />} />
                        <Route path="/table" element={<Table />} />
                        <Route path="/orders" element={<Orders />} />
                        <Route path="/orders/settled" element={<Orders />} />
                        <Route path="/reservations" element={<Reservations />} />
                        <Route path="/settings" element={<Settings />} />
                        <Route path="/open-entries" element={<OpenEntries />} />
                      </Route>
                      {/*
                        Captain "Order" module — its own shell, sibling to the
                        Cashier POS routes above, not nested under AppLayout
                        (PLAN.md §6/§10: own navigation, mobile-first, not the
                        desktop Header/Footer shell). This Router (basename
                        "/ury") is already the outer app-nesting layer that mounts
                        "/pos" today, so "/order" sits alongside it here rather
                        than in a separate outer router file.
                      */}
                      <Route
                        path="/order"
                        element={
                          <CaptainRouteGuard>
                            <CaptainTables />
                          </CaptainRouteGuard>
                        }
                      />
                      <Route
                        path="/order/table/:table"
                        element={
                          <CaptainRouteGuard>
                            <CaptainOrder />
                          </CaptainRouteGuard>
                        }
                      />
                    </Routes>
                    {aiEnabled && <ChatWidget ref={chatRef} />}
                  </ChatWidgetRefProvider>
                </AiEnabledProvider>
              </ActiveReportProvider>
            </POSOpeningProvider>
          </AuthGuard>
        </RoutedScreenSizeProvider>
      </Router>
    </PrinterWatchProvider>
  );
}

export default App;
