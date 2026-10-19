import { useDeviceBootstrap } from './hooks/useDeviceBootstrap'
import MobileQRLayout from './layouts/MobileQRLayout'
import TabletLayout from './layouts/TabletLayout'
import LandscapeKioskLayout from './layouts/LandscapeKioskLayout'
import PortraitKioskLayout from './layouts/PortraitKioskLayout'
import type { OrderingLayout } from './lib/api'
import { t } from './i18n'
import LanguageSwitcher, { useGuestLanguage } from './components/LanguageSwitcher'
import GuestFeedback from './components/GuestFeedback'

const LAYOUTS: Record<OrderingLayout, typeof MobileQRLayout> = {
  Mobile: MobileQRLayout,
  Tablet: TabletLayout,
  'Landscape Kiosk': LandscapeKioskLayout,
  'Portrait Kiosk': PortraitKioskLayout,
}

/**
 * Layout-shell selector. A provisioned device (kiosk/tablet, resolved via
 * useDeviceBootstrap from a stored device credential) picks its layout from
 * the server-returned `deviceContext.layout` — never a client guess — and
 * skips straight to it with an already-resolved context, so it never
 * re-bootstraps via QR token. Anything else (no stored device credential —
 * the QR/mobile case) falls through to MobileQRLayout, which does its own
 * token/session bootstrap via useOrderingSession().
 */
function OrderingApp() {
  const { isDevice, deviceContext, deviceLoading, deviceError } = useDeviceBootstrap()

  if (isDevice) {
    if (deviceLoading) {
      return (
        <div className="flex min-h-screen flex-col items-center justify-center gap-4 text-muted-foreground">
          <LanguageSwitcher />
          {t('status.starting')}
        </div>
      )
    }
    if (deviceError || !deviceContext) {
      return (
        <div className="flex min-h-screen flex-col items-center justify-center gap-4 p-6 text-center">
          <LanguageSwitcher />
          <p className="text-destructive">{deviceError ?? t('errors.device_start')}</p>
        </div>
      )
    }
    const Layout = LAYOUTS[deviceContext.layout] ?? MobileQRLayout
    return <Layout initialContext={deviceContext} />
  }

  return <MobileQRLayout />
}

function App() {
  useGuestLanguage()
  if (window.location.pathname === '/order/feedback') return <GuestFeedback />
  return <OrderingApp />
}

export default App
