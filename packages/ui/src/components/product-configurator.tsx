import { useEffect, useState } from 'react'
import { Minus, Plus, X } from 'lucide-react'
import { Button } from './button'
import { Dialog, DialogContent } from './dialog'
import { Input } from './input'
import { cn } from '../lib/cn'

export interface ProductOption {
  id: string
  name: string
  priceLabel: string
}

export interface ModifierOptionView {
  optionCode: string
  label: string
  prefix?: string | null
  priceLabel: string | null
  selected: boolean
  isDefault: boolean
  available: boolean
}

export interface ModifierGroupView {
  name: string
  displayTitle: string
  groupType: 'Condiment' | 'Combo Choice'
  required: boolean
  minSelections: number
  maxSelections: number
  allowPrefixes: boolean
  allowedPrefixes: string[]
  activePrefix?: string | null
  selectedCount: number
  options: ModifierOptionView[]
}

export interface ProductConfiguratorLabels {
  specialInstructions: string
  specialInstructionsPlaceholder: string
  quantity: string
  variants: string
  addons: string
  loadingAddons: string
  noAddons: string
  total: string
  submit: string
  requiredBadge?: string
  optionalBadge?: string
  includedBadge?: string
  soldOutBadge?: string
  regularPrefix?: string
  prefixStripLabel?: string
  selectRequiredPrompt?: string
}

export interface ProductConfiguratorProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  itemName: string
  itemCode: string
  courseLabel?: string
  imageUrl?: string | null
  quantity: string
  onQuantityChange: (value: string) => void
  onQuantityBlur?: () => void
  onIncrement: () => void
  onDecrement: () => void
  comments: string
  onCommentsChange: (value: string) => void
  variants: ProductOption[]
  selectedVariantId?: string | null
  onSelectVariant: (id: string) => void
  addons: ProductOption[]
  selectedAddonIds: string[]
  onToggleAddon: (id: string) => void
  addonsLoading?: boolean
  addonsError?: string | null
  modifierGroups?: ModifierGroupView[]
  onSelectModifierPrefix?: (groupName: string, prefix: string | null) => void
  onToggleModifierOption?: (groupName: string, optionCode: string) => void
  missingRequiredGroups?: string[]
  totalLabel: string
  submitDisabled?: boolean
  onSubmit: () => void
  labels: ProductConfiguratorLabels
}

export function ProductConfigurator({
  open,
  onOpenChange,
  itemName,
  itemCode,
  courseLabel,
  imageUrl,
  quantity,
  onQuantityChange,
  onQuantityBlur,
  onIncrement,
  onDecrement,
  comments,
  onCommentsChange,
  variants,
  selectedVariantId,
  onSelectVariant,
  addons,
  selectedAddonIds,
  onToggleAddon,
  addonsLoading,
  addonsError,
  modifierGroups = [],
  onSelectModifierPrefix,
  onToggleModifierOption,
  missingRequiredGroups = [],
  totalLabel,
  submitDisabled,
  onSubmit,
  labels,
}: ProductConfiguratorProps) {
  const [imageFailed, setImageFailed] = useState(false)
  const showImage = Boolean(imageUrl) && !imageFailed
  const hasModifierGroups = modifierGroups.length > 0

  useEffect(() => {
    setImageFailed(false)
  }, [imageUrl])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        variant="xlarge"
        className="flex max-h-dialog-max-h w-[calc(100vw-1rem)] max-w-[90rem] flex-col overflow-x-hidden overflow-y-auto bg-white p-0 sm:w-full md:flex-row"
        showCloseButton={false}
      >
        <div className="relative h-40 shrink-0 sm:h-48 md:h-auto md:min-h-[16rem] md:w-1/3 md:self-stretch">
          {showImage ? (
            <img
              src={imageUrl!}
              alt={itemName}
              className="h-full w-full rounded-t-lg object-cover md:absolute md:inset-0 md:rounded-s-lg md:rounded-se-none"
              style={{ filter: 'saturate(0.75) brightness(0.95)' }}
              onError={() => setImageFailed(true)}
            />
          ) : (
            <div className="flex h-full w-full items-center justify-center rounded-t-lg bg-gray-200 text-4xl font-medium text-gray-400 sm:text-5xl md:absolute md:inset-0 md:rounded-s-lg md:rounded-se-none md:text-6xl">
              {itemName.slice(0, 2).toUpperCase()}
            </div>
          )}
          <Button
            type="button"
            onClick={() => onOpenChange(false)}
            variant="outline"
            size="icon"
            className="absolute end-3 top-3 bg-white shadow-lg"
            aria-label="Close"
          >
            <X className="h-5 w-5" />
          </Button>
        </div>

        <div className="overflow-y-auto p-6 md:w-1/3">
          <h2 className="text-2xl font-bold text-gray-900">{itemName}</h2>
          <div className="mt-1 flex items-center gap-2">
            <span className="text-sm text-gray-500">{itemCode}</span>
            {courseLabel && (
              <>
                <span className="text-gray-300">•</span>
                <span className="text-sm font-medium text-blue-600">{courseLabel}</span>
              </>
            )}
          </div>

          <div className="mt-6">
            <h3 className="mb-3 text-lg font-semibold">{labels.specialInstructions}</h3>
            <Input
              placeholder={labels.specialInstructionsPlaceholder}
              value={comments}
              onChange={(e) => onCommentsChange(e.target.value)}
            />
          </div>

          <div className="mt-6">
            <h3 className="mb-3 text-lg font-semibold">{labels.quantity}</h3>
            <div className="flex items-center gap-2" role="group" aria-label={labels.quantity}>
              <Button
                type="button"
                onClick={onDecrement}
                variant="outline"
                size="icon"
                aria-label={`Decrease ${labels.quantity}`}
              >
                <Minus className="h-4 w-4" />
              </Button>
              <Input
                type="number"
                min="0"
                max="99"
                value={quantity}
                onChange={(e) => onQuantityChange(e.target.value)}
                onBlur={() => onQuantityBlur?.()}
                aria-label={labels.quantity}
                className="h-11 w-16 text-center [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
              />
              <Button
                type="button"
                onClick={onIncrement}
                variant="outline"
                size="icon"
                aria-label={`Increase ${labels.quantity}`}
              >
                <Plus className="h-4 w-4" />
              </Button>
            </div>
          </div>

          {variants.length > 0 && (
            <div className="mt-6">
              <h3 className="mb-3 text-lg font-semibold">{labels.variants}</h3>
              <div className="flex flex-wrap gap-2">
                {variants.map((variant) => (
                  <Button
                    key={variant.id}
                    type="button"
                    onClick={() => onSelectVariant(variant.id)}
                    variant="outline"
                    aria-pressed={variant.id === selectedVariantId}
                    className={cn(
                      'flex w-full items-center justify-between rounded-lg p-2 text-start',
                      variant.id === selectedVariantId
                        ? 'border-blue-500 bg-blue-50 hover:border-blue-500'
                        : 'border-border hover:border-blue-200'
                    )}
                  >
                    <div className="font-medium">{variant.name}</div>
                    <div className="text-sm text-gray-500">{variant.priceLabel}</div>
                  </Button>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="flex h-auto min-w-0 flex-col overflow-y-auto border-t border-border p-4 sm:p-6 md:w-1/3 md:border-s md:border-t-0">
          <div className="mb-6 space-y-5 overflow-y-auto">
            {hasModifierGroups && (
              <div className="space-y-5" data-testid="modifier-groups-section">
                {modifierGroups.map((group) => {
                  const isMissing = group.required && group.selectedCount < Math.max(1, group.minSelections)
                  return (
                    <section key={group.name} className="space-y-2.5" aria-label={group.displayTitle}>
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <h3 className="text-base font-semibold text-gray-900">{group.displayTitle}</h3>
                        <div className="flex items-center gap-1.5">
                          <span
                            className={cn(
                              'rounded-full px-2 py-0.5 text-xs font-medium',
                              group.required
                                ? isMissing
                                  ? 'bg-amber-100 text-amber-800'
                                  : 'bg-emerald-100 text-emerald-800'
                                : 'bg-gray-100 text-gray-600'
                            )}
                          >
                            {group.required
                              ? `${labels.requiredBadge || 'Required'} (${group.selectedCount}/${Math.max(1, group.minSelections)})`
                              : group.maxSelections > 0
                                ? `${labels.optionalBadge || 'Optional'} (${group.selectedCount}/${group.maxSelections})`
                                : labels.optionalBadge || 'Optional'}
                          </span>
                        </div>
                      </div>

                      {group.allowPrefixes && group.allowedPrefixes.length > 0 && (
                        <div
                          role="group"
                          aria-label={labels.prefixStripLabel || 'Modifier prefix'}
                          className="flex flex-wrap gap-1.5"
                        >
                          <button
                            type="button"
                            onClick={() => onSelectModifierPrefix?.(group.name, null)}
                            aria-pressed={!group.activePrefix}
                            className={cn(
                              'min-h-[44px] min-w-[44px] rounded-md border px-3 py-2 text-xs font-medium transition-colors touch-manipulation',
                              !group.activePrefix
                                ? 'border-blue-600 bg-blue-600 text-white'
                                : 'border-border bg-white text-gray-700 hover:bg-gray-50'
                            )}
                          >
                            {labels.regularPrefix || 'Regular'}
                          </button>
                          {group.allowedPrefixes.map((prefix) => {
                            const active = group.activePrefix === prefix
                            return (
                              <button
                                key={prefix}
                                type="button"
                                onClick={() => onSelectModifierPrefix?.(group.name, active ? null : prefix)}
                                aria-pressed={active}
                                className={cn(
                                  'min-h-[44px] min-w-[44px] rounded-md border px-3 py-2 text-xs font-medium transition-colors touch-manipulation',
                                  active
                                    ? 'border-blue-600 bg-blue-600 text-white'
                                    : 'border-border bg-white text-gray-700 hover:bg-gray-50'
                                )}
                              >
                                {prefix}
                              </button>
                            )
                          })}
                        </div>
                      )}

                      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                        {group.options.map((opt) => (
                          <button
                            key={opt.optionCode}
                            type="button"
                            disabled={!opt.available}
                            aria-pressed={opt.selected}
                            onClick={() => onToggleModifierOption?.(group.name, opt.optionCode)}
                            className={cn(
                              'flex min-h-[44px] w-full items-center justify-between gap-2 rounded-lg border px-3 py-2 text-start text-sm font-medium transition-colors touch-manipulation',
                              !opt.available
                                ? 'cursor-not-allowed border-gray-200 bg-gray-100 text-gray-400'
                                : opt.selected
                                  ? 'border-blue-600 bg-blue-50 text-blue-900'
                                  : 'border-border bg-white text-gray-900 hover:border-blue-300'
                            )}
                          >
                            <span className="min-w-0 truncate">
                              {opt.selected && opt.prefix ? `${opt.prefix} ` : ''}
                              {opt.label}
                            </span>
                            <span className="shrink-0 text-xs font-normal text-gray-500">
                              {!opt.available
                                ? labels.soldOutBadge || 'Sold Out'
                                : opt.priceLabel
                                  ? opt.priceLabel
                                  : group.groupType === 'Combo Choice' || opt.isDefault
                                    ? labels.includedBadge || 'Included'
                                    : null}
                            </span>
                          </button>
                        ))}
                      </div>
                    </section>
                  )
                })}
              </div>
            )}

            {addonsLoading ? (
              <div className="flex items-center justify-center text-gray-500">{labels.loadingAddons}</div>
            ) : addonsError ? (
              <div className="flex items-center justify-center text-red-500">{addonsError}</div>
            ) : addons.length > 0 ? (
              <div>
                <h3 className="mb-3 text-lg font-semibold">{labels.addons}</h3>
                <div className="space-y-2">
                  {addons.map((addon) => (
                    <Button
                      key={addon.id}
                      type="button"
                      onClick={() => onToggleAddon(addon.id)}
                      variant="outline"
                      aria-pressed={selectedAddonIds.includes(addon.id)}
                      className={cn(
                        'min-h-[44px] w-full rounded-lg p-3 text-start',
                        selectedAddonIds.includes(addon.id)
                          ? 'border-blue-500 bg-blue-50 hover:border-blue-500'
                          : 'border-border hover:border-blue-200'
                      )}
                    >
                      <div className="flex w-full items-center justify-between">
                        <span>{addon.name}</span>
                        <span className="text-sm text-gray-500">+{addon.priceLabel}</span>
                      </div>
                    </Button>
                  ))}
                </div>
              </div>
            ) : !hasModifierGroups ? (
              <div className="flex items-center justify-center text-sm text-gray-400">{labels.noAddons}</div>
            ) : null}
          </div>

          <div className="mt-auto border-t border-border pt-2">
            {missingRequiredGroups.length > 0 && (
              <p className="mb-2 text-xs font-medium text-amber-700" role="status">
                {labels.selectRequiredPrompt || 'Complete required selections'}: {missingRequiredGroups.join(', ')}
              </p>
            )}
            <div className="flex items-center justify-between text-lg font-semibold">
              <span>{labels.total}&nbsp;</span>
              <span>{totalLabel}</span>
            </div>
            <Button type="button" onClick={onSubmit} className="mt-4 min-h-[48px] w-full" size="lg" disabled={submitDisabled}>
              {labels.submit}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
