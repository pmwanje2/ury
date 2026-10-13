import { useEffect, useState } from 'react'
import { parseFrappeError } from '@ury/core'
import { t } from '../i18n'
import { useGuestLanguage } from '../components/LanguageSwitcher'
import { bootstrapDevice, type OrderingContext } from '../lib/api'

// Storage keys for a provisioned kiosk/tablet device's credentials. These
// are set once at provisioning time (out of band — see
// `enroll_device` in the backend, a staff-only endpoint) and persisted
// across sessions so the device can self-bootstrap on every load without a
// QR scan.
const DEVICE_ID_KEY = 'ury_device_id'
const DEVICE_CREDENTIAL_KEY = 'ury_device_credential'

// Same sentinel useOrderingSession uses so a transport failure with no ERP
// sentence stays a catalog key. "There was an error." is the SDK placeholder,
// which parseFrappeError already treats as "no server text".
const CATALOG_FALLBACK = '\u0000'

type ShownDeviceError =
  | { source: 'catalog' }
  | { source: 'server'; text: string }

function shownDeviceError(err: unknown): ShownDeviceError {
  const parsed = parseFrappeError(err, CATALOG_FALLBACK)
  if (parsed === CATALOG_FALLBACK) return { source: 'catalog' }
  return { source: 'server', text: parsed }
}

/**
 * On mount, checks localStorage for a stored device_id/device_credential
 * pair. If present, bootstraps an ordering session via the device-credential
 * path (as opposed to a QR token). This is a separate concern from
 * `useOrderingSession` — it only answers "is this a provisioned device and,
 * if so, what's its ordering context." A later integration point decides
 * how to combine that with layout selection and the rest of the ordering
 * flow.
 */
export function useDeviceBootstrap() {
  const [isDevice, setIsDevice] = useState(false)
  const [deviceContext, setDeviceContext] = useState<OrderingContext | null>(null)
  const [deviceLoading, setDeviceLoading] = useState(true)
  const [shownError, setShownError] = useState<ShownDeviceError | null>(null)
  useGuestLanguage()

  useEffect(() => {
    async function init() {
      const deviceId = localStorage.getItem(DEVICE_ID_KEY)
      const deviceCredential = localStorage.getItem(DEVICE_CREDENTIAL_KEY)

      if (!deviceId || !deviceCredential) {
        setIsDevice(false)
        setDeviceLoading(false)
        return
      }

      setIsDevice(true)
      try {
        const context = await bootstrapDevice(deviceId, deviceCredential)
        setDeviceContext(context)
      } catch (err) {
        setShownError(shownDeviceError(err))
      } finally {
        setDeviceLoading(false)
      }
    }
    init()
  }, [])

  const deviceError = shownError == null
    ? null
    : shownError.source === 'server'
      ? shownError.text
      : t('errors.device_bootstrap')

  return { isDevice, deviceContext, deviceLoading, deviceError }
}
