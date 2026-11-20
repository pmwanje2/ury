import { useEffect, useState } from 'react'
import { getCustomerProduct, type MenuItem, type ProductDetail as ProductDetailData, type ProductOption } from '../../lib/api'
import { t } from '../../i18n'
import { useGuestLanguage } from '../../components/LanguageSwitcher'

export interface ProductDetailProps {
  /** Session token for the `get_customer_product` lookup. */
  session: string
  /** Item code to fetch detail for. */
  itemCode: string
  /**
   * Base menu item this detail screen was opened from — used for its
   * `rate`/`item_image` fallback and as the `item` passed to `onAddToCart`,
   * since `addToCart(item, options)` expects a full `MenuItem`, not the
   * narrower `ProductDetail` shape `get_customer_product` returns.
   */
  menuItem: MenuItem
  /** Called with the same options shape `useOrderingSession`'s `addToCart` accepts. */
  onAddToCart: (item: MenuItem, options: { comment?: string; variant?: string; addons?: string[] }) => void
  onBack: () => void
}

/**
 * Product detail screen/sheet: fetches richer detail for a single item
 * (description, larger image, variants, add-ons) via `get_customer_product`,
 * and lets the customer pick a variant/add-ons and quantity before adding
 * to cart. Purely presentational about navigation — callers wire `onBack`/
 * `onAddToCart` to whatever the hosting layout's navigation is.
 */
function ProductDetail({ session, itemCode, menuItem, onAddToCart, onBack }: ProductDetailProps) {
  useGuestLanguage()
  const [loading, setLoading] = useState(true)
  // null with failed=true shows the catalog sentence, translated each render.
  const [error, setError] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  const [detail, setDetail] = useState<ProductDetailData | null>(null)
  const [qty, setQty] = useState(1)
  const [selectedVariant, setSelectedVariant] = useState<string | null>(null)
  const [selectedAddons, setSelectedAddons] = useState<string[]>([])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setFailed(false)
    setDetail(null)
    setQty(1)
    setSelectedVariant(null)
    setSelectedAddons([])

    getCustomerProduct(session, itemCode)
      .then((data) => {
        if (!cancelled) setDetail(data)
      })
      .catch((err) => {
        if (!cancelled) {
          setFailed(true)
          setError(err instanceof Error ? err.message : null)
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [session, itemCode])

  function toggleAddon(addonCode: string) {
    setSelectedAddons((prev) =>
      prev.includes(addonCode) ? prev.filter((code) => code !== addonCode) : [...prev, addonCode],
    )
  }

  function handleAddToCart() {
    for (let i = 0; i < qty; i += 1) {
      onAddToCart(menuItem, {
        variant: selectedVariant ?? undefined,
        addons: selectedAddons.length > 0 ? selectedAddons : undefined,
      })
    }
    onBack()
  }

  if (loading) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-6">
        <div className="text-muted-foreground">{t('item.loading')}</div>
        <button onClick={onBack} className="text-sm underline">
          {t('nav.back')}
        </button>
      </div>
    )
  }

  if (error || !detail) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-6 text-center">
        <div className="text-destructive">{error ?? (failed ? t('item.load_error') : t('item.not_found'))}</div>
        <button onClick={onBack} className="rounded-full border px-4 py-2 text-sm font-medium">
          {t('nav.back_to_menu')}
        </button>
      </div>
    )
  }

  const image = detail.image ?? menuItem.item_image

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <div className="flex items-center gap-2 p-4">
        <button onClick={onBack} className="text-sm underline">
          {t('nav.back')}
        </button>
      </div>

      {image && <img src={image} alt={detail.item_name} className="w-full object-cover" />}

      <div className="flex flex-col gap-4 p-6">
        <div>
          <h1 className="text-xl font-semibold">{detail.item_name}</h1>
          {detail.description && <p className="mt-1 text-muted-foreground">{detail.description}</p>}
        </div>

        <div className="text-lg font-medium">{menuItem.rate}</div>

        {detail.variants.length > 0 && (
          <div className="flex flex-col gap-2">
            <div className="text-sm font-semibold">{t('item.variants')}</div>
            <div className="flex flex-col gap-2">
              {detail.variants.map((variant) => (
                <OptionRow
                  key={variant.item_code}
                  option={variant}
                  selected={selectedVariant === variant.item_code}
                  onSelect={() =>
                    setSelectedVariant((prev) => (prev === variant.item_code ? null : variant.item_code))
                  }
                />
              ))}
            </div>
          </div>
        )}

        {detail.addons.length > 0 && (
          <div className="flex flex-col gap-2">
            <div className="text-sm font-semibold">{t('item.addons')}</div>
            <div className="flex flex-col gap-2">
              {detail.addons.map((addon) => (
                <OptionRow
                  key={addon.item_code}
                  option={addon}
                  selected={selectedAddons.includes(addon.item_code)}
                  onSelect={() => toggleAddon(addon.item_code)}
                />
              ))}
            </div>
          </div>
        )}

        <div className="flex items-center gap-4">
          <button
            onClick={() => setQty((q) => Math.max(1, q - 1))}
            className="h-9 w-9 rounded-full border font-semibold"
            aria-label={t('item.decrease')}
          >
            -
          </button>
          <div className="w-6 text-center font-medium">{qty}</div>
          <button
            onClick={() => setQty((q) => q + 1)}
            className="h-9 w-9 rounded-full border font-semibold"
            aria-label={t('item.increase')}
          >
            +
          </button>
        </div>

        <button
          onClick={handleAddToCart}
          className="mt-2 rounded-full bg-primary px-4 py-3 font-semibold text-primary-foreground"
        >
          {t('item.add_to_cart')}
        </button>
      </div>
    </div>
  )
}

interface OptionRowProps {
  option: ProductOption
  selected: boolean
  onSelect: () => void
}

/** A single variant/add-on row. `rate` renders a fallback dash when null (see `ProductOption`). */
function OptionRow({ option, selected, onSelect }: OptionRowProps) {
  return (
    <button
      onClick={onSelect}
      className={`flex items-center justify-between rounded-lg border px-4 py-3 text-left ${
        selected ? 'border-primary bg-primary/10' : 'border-border'
      }`}
    >
      <span>{option.item_name}</span>
      <span className="text-muted-foreground">{option.rate ?? '—'}</span>
    </button>
  )
}

export default ProductDetail
