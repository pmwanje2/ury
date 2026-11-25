import { Minus, Plus, MessageSquare, RotateCcw, Trash2 } from 'lucide-react';
import { cn } from '@ury/ui';
import { formatCurrency } from '@ury/core';
import type { OrderDeltaLine } from '../hooks/useTableOrderContext';
import { t } from '../../i18n';

interface CaptainOrderLineProps {
  line: OrderDeltaLine;
  /** 'confirmed' = Already Ordered row, 'delta' = New/Changed row, 'reduction' = Reduction pending row. */
  variant: 'confirmed' | 'delta' | 'reduction';
  disabled?: boolean;
  onIncrement?: () => void;
  onDecrement?: () => void;
  onRemove?: () => void;
  onRestore?: () => void;
  onEditNote?: () => void;
  currency?: string;
}

/**
 * Single row in the delta-aware order list (PLAN.md §8). Deliberately one
 * component for all three groups (rather than three near-duplicates) since
 * they share the same line shape and differ only in which controls show.
 */
const CaptainOrderLine: React.FC<CaptainOrderLineProps> = ({
  line,
  variant,
  disabled,
  onIncrement,
  onDecrement,
  onRemove,
  onRestore,
  onEditNote,
  currency,
}) => {
  const displayQty = variant === 'confirmed' ? line.confirmedQty : variant === 'reduction' ? Math.abs(line.delta) : line.delta;
  const sign = variant === 'delta' ? '+' : variant === 'reduction' ? '−' : '';

  if (line.isModifier) {
    const prefixText = line.modifierPrefix ? `${line.modifierPrefix} ` : '';
    return (
      <div
        data-testid={`captain-modifier-child-${line.uniqueId}`}
        className={cn(
          'ms-4 flex items-center justify-between gap-2 border-s-2 py-2 ps-3 pe-2.5 rounded-e-lg text-xs',
          variant === 'delta' && 'border-s-blue-300 bg-blue-50/60',
          variant === 'reduction' && 'border-s-red-300 bg-red-50/60',
          variant === 'confirmed' && 'border-s-blue-200 bg-gray-50/80'
        )}
      >
        <div className="min-w-0 flex-1">
          <p
            className={cn(
              'font-medium truncate',
              variant === 'delta' && 'text-blue-900',
              variant === 'reduction' && 'text-red-800 line-through decoration-red-400',
              variant === 'confirmed' && 'text-gray-700'
            )}
          >
            + {prefixText}
            {line.name}
          </p>
          <p className="text-gray-500">
            {sign}
            {displayQty} &times; {formatCurrency(line.price, currency)}
          </p>
        </div>
        <span
          className={cn(
            'shrink-0 font-medium tabular-nums',
            variant === 'reduction' ? 'text-red-800 line-through' : 'text-gray-700'
          )}
        >
          {formatCurrency(line.price * displayQty, currency)}
        </span>
      </div>
    );
  }

  const pillLabels = new Set<string>();
  for (const mod of line.selectedModifiers || []) {
    const txt = mod.prefix ? `${mod.prefix} ${mod.label}` : mod.label;
    if (txt) pillLabels.add(txt.trim().toLowerCase());
  }
  for (const ov of line.bundleOverrides || []) {
    if (ov.label) pillLabels.add(ov.label.trim().toLowerCase());
  }
  const residualComment = line.comment
    ? line.comment
        .split('·')
        .map((seg) => seg.trim())
        .filter((seg) => seg && !pillLabels.has(seg.toLowerCase()))
        .join(' · ')
    : '';

  return (
    <div
      className={cn(
        'flex items-center justify-between gap-3 py-3 px-3 rounded-lg',
        variant === 'delta' && 'bg-blue-50',
        variant === 'reduction' && 'bg-red-50',
        variant === 'confirmed' && 'bg-white'
      )}
    >
      <button
        type="button"
        onClick={onEditNote}
        disabled={!onEditNote}
        className={cn('flex-1 min-w-0 text-start', !onEditNote && 'cursor-default')}
      >
        <div className="flex items-center gap-2">
          <span
            className={cn(
              'font-medium text-sm',
              variant === 'delta' && 'text-blue-900',
              variant === 'reduction' && 'text-red-900 line-through decoration-red-400',
              variant === 'confirmed' && 'text-gray-900'
            )}
          >
            {sign}
            {displayQty} &times; {line.name}
          </span>
        </div>
        {line.selectedModifiers && line.selectedModifiers.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {line.selectedModifiers.map((mod) => (
              <span
                key={`${mod.group}-${mod.option_code}-${mod.prefix || ''}`}
                className="inline-flex items-center rounded bg-blue-100/80 px-1.5 py-0.5 text-xs text-blue-800"
              >
                {mod.prefix ? `${mod.prefix} ` : ''}
                {mod.label}
              </span>
            ))}
          </div>
        )}
        {line.bundleOverrides && line.bundleOverrides.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {line.bundleOverrides.map((ov) => (
              <span
                key={`${ov.group}-${ov.chosen_item}`}
                className="inline-flex items-center rounded bg-emerald-50 px-1.5 py-0.5 text-xs text-emerald-800"
              >
                {ov.label}
              </span>
            ))}
          </div>
        )}
        {residualComment && (
          <p className="text-xs text-gray-500 mt-0.5 flex items-center gap-1">
            <MessageSquare className="w-3 h-3 shrink-0" />
            {residualComment}
          </p>
        )}
        <p className="text-xs text-gray-500 mt-0.5 flex items-center gap-1.5 tabular-nums">
          <span>{formatCurrency(line.price * displayQty, currency)}</span>
          {line.originalPrice != null && line.originalPrice > line.price && (
            <span className="text-gray-400 line-through">
              {formatCurrency(line.originalPrice * displayQty, currency)}
            </span>
          )}
        </p>
      </button>

      <div className="flex items-center gap-1 shrink-0">
        {variant === 'delta' && (onDecrement || onIncrement) && (
          <>
            <button
              type="button"
              onClick={onDecrement}
              disabled={disabled}
              className="w-9 h-9 rounded-full border border-border flex items-center justify-center disabled:opacity-40"
              aria-label={t('captain.decrease')}
            >
              <Minus className="w-4 h-4" />
            </button>
            <button
              type="button"
              onClick={onIncrement}
              disabled={disabled}
              className="w-9 h-9 rounded-full border border-border flex items-center justify-center disabled:opacity-40"
              aria-label={t('captain.increase')}
            >
              <Plus className="w-4 h-4" />
            </button>
          </>
        )}

        {variant === 'confirmed' && onDecrement && (
          <button
            type="button"
            onClick={onDecrement}
            disabled={disabled}
            className="w-9 h-9 rounded-full border border-border flex items-center justify-center disabled:opacity-40"
            aria-label={t('captain.reduce_qty')}
            title={t('captain.reduce_qty')}
          >
            <Minus className="w-4 h-4" />
          </button>
        )}

        {variant === 'confirmed' && onRemove && (
          <button
            type="button"
            onClick={onRemove}
            disabled={disabled}
            className="w-9 h-9 rounded-full border border-red-200 text-red-600 flex items-center justify-center disabled:opacity-40"
            aria-label={t('captain.remove_item')}
            title={t('captain.remove_item')}
          >
            <Trash2 className="w-4 h-4" />
          </button>
        )}

        {variant === 'reduction' && onRestore && (
          <button
            type="button"
            onClick={onRestore}
            disabled={disabled}
            className="w-9 h-9 rounded-full border border-border flex items-center justify-center disabled:opacity-40"
            aria-label={t('captain.undo_reduction')}
            title={t('captain.undo')}
          >
            <RotateCcw className="w-4 h-4" />
          </button>
        )}
      </div>
    </div>
  );
};

export default CaptainOrderLine;
