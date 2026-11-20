import type { OrderStatus } from '../../lib/api'
import { t } from '../../i18n'
import { useGuestLanguage } from '../../components/LanguageSwitcher'

// URY Service Request options -> catalog keys; an unknown option shows as sent.
const REQUEST_LABELS: Record<string, string> = {
  Bill: 'requests.bill',
  Assistance: 'requests.assistance',
  Open: 'requests.open',
  Acknowledged: 'requests.acknowledged',
  Resolved: 'requests.resolved',
}

function requestLabel(value: string): string {
  const key = REQUEST_LABELS[value]
  return key ? t(key) : value
}

interface OrderStatusScreenProps {
  status: OrderStatus | null
  isPickup: boolean
  pickupCode?: string | null
  canAddMore: boolean
  onAddMore: () => void
  onDone?: () => void
}

/**
 * Post-submit confirmation/status screen. Deliberately scoped to what
 * `get_order_status` actually returns (`session_status`/`invoice`/`billed`/
 * `submitted`/`open_requests`) — there is no kitchen-stage data
 * (preparing/ready) on the backend, so this does not fabricate one.
 */
function OrderStatusScreen({ status, isPickup, pickupCode, canAddMore, onAddMore, onDone }: OrderStatusScreenProps) {
  useGuestLanguage()
  const openRequests = status?.open_requests ?? []

  return (
    <div className="flex min-h-screen flex-col items-center px-4 py-8 text-center">
      <h1 className="text-xl font-semibold">{t('status_screen.confirmed')}</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        {t('status_screen.sent')}
      </p>

      {isPickup && pickupCode && (
        <div className="mt-6 w-full max-w-xs rounded-lg border bg-muted p-4">
          <div className="text-xs uppercase tracking-wide text-muted-foreground">{t('status_screen.pickup_code')}</div>
          <div className="mt-1 text-3xl font-bold">{pickupCode}</div>
        </div>
      )}

      <div className="mt-6 w-full max-w-xs space-y-2 text-left text-sm">
        {status?.submitted !== undefined && (
          <div className="flex justify-between rounded-md border p-2">
            <span className="text-muted-foreground">{t('status_screen.order_status')}</span>
            <span className="font-medium">{status.submitted ? t('status_screen.submitted') : t('status_screen.pending')}</span>
          </div>
        )}
        {status?.billed !== undefined && (
          <div className="flex justify-between rounded-md border p-2">
            <span className="text-muted-foreground">{t('status_screen.bill_status')}</span>
            <span className="font-medium">{status.billed ? t('status_screen.billed') : t('status_screen.not_billed')}</span>
          </div>
        )}
        {openRequests.length > 0 && (
          <div className="rounded-md border p-2">
            <div className="mb-1 text-muted-foreground">{t('status_screen.open_requests')}</div>
            <ul className="space-y-1">
              {openRequests.map((request) => (
                <li key={request.name} className="flex justify-between">
                  <span>{requestLabel(request.request_type)}</span>
                  <span className="font-medium">{requestLabel(request.status)}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      <div className="mt-8 flex w-full max-w-xs flex-col gap-2">
        {canAddMore && (
          <button
            onClick={onAddMore}
            className="w-full rounded-md bg-primary py-3 font-medium text-primary-foreground"
          >
            {t('status_screen.add_more')}
          </button>
        )}
        {onDone && (
          <button onClick={onDone} className="w-full rounded-md border py-3 font-medium">
            {t('status_screen.done')}
          </button>
        )}
      </div>
    </div>
  )
}

export default OrderStatusScreen
