import { formatCurrency } from '@ury/core'
import { hasPrice, type AddonState } from '../hooks/useOrderingSession'
import type { MenuItem } from '../lib/api'
import { t } from '../i18n'

export default function GuestAddons({ state, onAdd, onRetry }: {
  state?: AddonState; onAdd: (item: MenuItem) => void; onRetry: () => void
}) {
  if (!state) return null
  if (state.status === 'loading') return <p role="status" className="p-2 text-sm">{t('item.addons_loading')}</p>
  if (state.status === 'error') return (
    <div className="space-y-1 p-2 text-sm">
      <p role="alert">{t('item.addons_error')}</p>
      <button onClick={onRetry} className="min-h-11 rounded-md border px-3 py-2">{t('item.addons_retry')}</button>
    </div>
  )
  if (!state.items.length) return null
  return (
    <div className="space-y-1 border-t p-2 text-sm">
      <p className="font-medium">{t('item.addons')}</p>
      {state.items.map(item => (
        <button key={item.item} onClick={() => onAdd(item)} disabled={!hasPrice(item)}
          aria-label={t('item.addon_add', { name: item.item_name })}
          className="flex min-h-11 w-full items-center justify-between gap-2 rounded-md border px-2 py-2 text-left disabled:opacity-60">
          <span>{item.item_name}</span>
          <span>{hasPrice(item) ? formatCurrency(item.rate) : t('item.price_unavailable')}</span>
        </button>
      ))}
    </div>
  )
}
