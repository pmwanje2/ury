import { useEffect, useMemo, useState } from 'react';
import { Check, Minus, Plus } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@ury/ui';
import { Button } from '@ury/ui';
import { cn } from '@ury/ui';
import { formatCurrency, parseFrappeError } from '@ury/core';
import { t } from '../i18n';
import { CustomerPicker } from './CustomerPicker';
import type { Customer } from '../store/pos-store';

export interface BillSplitItem {
  name: string;
  item_name: string;
  qty: number;
  rate: number;
  amount: number;
  reservation_line_key?: string | null;
  custom_parent_line_key?: string | null;
  custom_is_modifier?: number;
  custom_seat_no?: number;
  is_disposable?: number;
  custom_modifier_prefix?: string | null;
}

export type MultiSplitRequest =
  | { mode: 'by_seat' }
  | { mode: 'even'; splitCount: number }
  | { mode: 'manual'; splits: Array<Array<{ name: string; qty: number }>> };

interface BillSplitDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  invoiceName: string;
  items: BillSplitItem[];
  currency?: string;
  sourceCustomer?: Customer | null;
  onConfirmMulti?: (payload: MultiSplitRequest) => Promise<void>;
  onConfirm: (payload: {
    itemsToMove: Array<{ name: string; qty: number }>;
    customer?: string;
    /** Short name for the new check (David, Seat 2); no Customer is created. */
    guestLabel?: string;
  }) => Promise<void>;
}

const LegacyBillSplitDialog = ({
  open,
  onOpenChange,
  invoiceName,
  items,
  currency,
  sourceCustomer,
  onConfirm,
}: BillSplitDialogProps) => {
  const [moveQtyByRow, setMoveQtyByRow] = useState<Record<string, number>>({});
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sameAsOriginal, setSameAsOriginal] = useState(true);
  const [selectedCustomer, setSelectedCustomer] = useState<Customer | null>(null);
  const [guestLabel, setGuestLabel] = useState('');

  useEffect(() => {
    if (open) {
      setSameAsOriginal(true);
      setSelectedCustomer(sourceCustomer ?? null);
      setMoveQtyByRow({});
      setGuestLabel('');
      setError(null);
    }
  }, [open, sourceCustomer, invoiceName]);

  const resetState = () => {
    setMoveQtyByRow({});
    setError(null);
    setSameAsOriginal(true);
    setSelectedCustomer(sourceCustomer ?? null);
  };

  const handleOpenChange = (next: boolean) => {
    if (isSubmitting) return;
    if (!next) resetState();
    onOpenChange(next);
  };

  const totals = useMemo(() => {
    let moving = 0;
    let staying = 0;

    for (const item of items) {
      const moveQty = moveQtyByRow[item.name] ?? 0;
      const stayQty = item.qty - moveQty;
      moving += moveQty * item.rate;
      staying += stayQty * item.rate;
    }

    return { moving, staying };
  }, [items, moveQtyByRow]);

  const hasValidSelection = useMemo(() => {
    if (totals.moving <= 0) return false;
    if (totals.staying <= 0) return false;
    return items.every((item) => (moveQtyByRow[item.name] ?? 0) <= item.qty);
  }, [items, moveQtyByRow, totals.moving, totals.staying]);

  const linkedChildrenOf = (parent: BillSplitItem) => {
    const pkey = parent.reservation_line_key || parent.name;
    if (!pkey) return [];
    return items.filter(
      (row) => Number(row.custom_is_modifier || 0) === 1 && row.custom_parent_line_key === pkey
    );
  };

  const toggleItem = (item: BillSplitItem, checked: boolean) => {
    if (Number(item.custom_is_modifier || 0) === 1) return;
    setMoveQtyByRow((prev) => {
      const next = { ...prev, [item.name]: checked ? item.qty : 0 };
      for (const child of linkedChildrenOf(item)) {
        next[child.name] = checked ? child.qty : 0;
      }
      return next;
    });
    setError(null);
  };

  const adjustQty = (item: BillSplitItem, delta: number) => {
    if (Number(item.custom_is_modifier || 0) === 1) return;
    setMoveQtyByRow((prev) => {
      const current = prev[item.name] ?? 0;
      const nextQty = Math.min(item.qty, Math.max(0, current + delta));
      const next = { ...prev, [item.name]: nextQty };
      const ratio = item.qty > 0 ? nextQty / item.qty : 0;
      for (const child of linkedChildrenOf(item)) {
        next[child.name] = nextQty === 0 ? 0 : Math.min(child.qty, Math.round(child.qty * ratio * 1000) / 1000);
      }
      return next;
    });
    setError(null);
  };

  const handleConfirm = async () => {
    if (!hasValidSelection) {
      setError(t('bill_split.cannot_move_all_items'));
      return;
    }

    const itemsToMove = items
      .map((item) => ({ name: item.name, qty: moveQtyByRow[item.name] ?? 0 }))
      .filter((row) => row.qty > 0);

    const customerId = sameAsOriginal ? sourceCustomer?.id : selectedCustomer?.id;
    if (!sameAsOriginal && !customerId) {
      setError(t('bill_split.customer_required'));
      return;
    }

    setIsSubmitting(true);
    setError(null);
    try {
      await onConfirm({
        itemsToMove,
        customer: sameAsOriginal ? undefined : customerId,
        guestLabel: guestLabel.trim() || undefined,
      });
      resetState();
      onOpenChange(false);
    } catch (err) {
      setError(parseFrappeError(err, t('bill_split.split_failed')));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        variant="large"
        size="lg"
        onClose={() => handleOpenChange(false)}
        className="max-h-dialog-max-h overflow-y-auto"
      >
        <DialogHeader>
          <DialogTitle>{t('bill_split.split_bill')}</DialogTitle>
          <DialogDescription>
            {t('bill_split.select_items', { invoice: invoiceName })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 px-6 pb-2">
          {items.map((item) => {
            const moveQty = moveQtyByRow[item.name] ?? 0;
            const isSelected = moveQty > 0;
            const isModifier = Number(item.custom_is_modifier || 0) === 1;

            if (isModifier) {
              return (
                <div
                  key={item.name}
                  className={cn(
                    'ms-6 rounded-md border border-s-2 border-s-blue-200 bg-gray-50/70 px-3 py-2 text-xs',
                    isSelected ? 'border-primary/40 bg-primary-50/30' : 'border-border'
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium text-gray-700">
                      + {item.custom_modifier_prefix ? `${item.custom_modifier_prefix} ` : ''}
                      {item.item_name} ({item.qty})
                    </span>
                    <span className="font-semibold text-gray-700">
                      {formatCurrency(item.amount, currency)}
                    </span>
                  </div>
                </div>
              );
            }

            return (
              <div
                key={item.name}
                role="button"
                tabIndex={isSubmitting ? -1 : 0}
                onClick={() => !isSubmitting && toggleItem(item, !isSelected)}
                onKeyDown={(e) => {
                  if (isSubmitting) return;
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    toggleItem(item, !isSelected);
                  }
                }}
                className={cn(
                  'rounded-lg border p-3 transition-colors',
                  isSubmitting ? 'cursor-default' : 'cursor-pointer',
                  isSelected ? 'border-primary bg-primary-50/40' : 'border-border hover:border-border'
                )}
              >
                <div className="flex items-start gap-3">
                  <div
                    className={cn(
                      'mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border',
                      isSelected ? 'border-foreground bg-foreground text-background' : 'border-border bg-white'
                    )}
                    aria-hidden
                  >
                    {isSelected && <Check className="h-3 w-3" strokeWidth={3} />}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-start justify-between gap-2">
                      <div>
                        <p className="font-medium text-gray-900">{item.item_name}</p>
                        <p className="text-xs text-gray-500">
                          {t('bill_split.available_qty', { qty: item.qty })}
                        </p>
                      </div>
                      <span className="text-sm font-semibold text-gray-900">
                        {formatCurrency(item.amount, currency)}
                      </span>
                    </div>

                    {isSelected && item.qty > 1 && (
                      <div
                        className="mt-3 flex items-center gap-2"
                        onClick={(e) => e.stopPropagation()}
                        onKeyDown={(e) => e.stopPropagation()}
                      >
                        <span className="text-xs font-medium text-gray-600">
                          {t('bill_split.move_qty')}
                        </span>
                        <Button
                          type="button"
                          variant="outline"
                          size="icon"
                          className="h-7 w-7"
                          onClick={(e) => {
                            e.stopPropagation();
                            adjustQty(item, -1);
                          }}
                          disabled={isSubmitting || moveQty <= 0}
                        >
                          <Minus className="h-3 w-3" />
                        </Button>
                        <span className="w-8 text-center text-sm font-semibold">{moveQty}</span>
                        <Button
                          type="button"
                          variant="outline"
                          size="icon"
                          className="h-7 w-7"
                          onClick={(e) => {
                            e.stopPropagation();
                            adjustQty(item, 1);
                          }}
                          disabled={isSubmitting || moveQty >= item.qty}
                        >
                          <Plus className="h-3 w-3" />
                        </Button>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        <div className="mx-6 mb-4 space-y-3 rounded-lg border border-border p-4">
          <p className="text-sm font-medium text-gray-900">{t('bill_split.customer_for_new_bill')}</p>
          <label className="flex items-center gap-2 text-sm text-gray-700">
            <input
              type="checkbox"
              checked={sameAsOriginal}
              onChange={(e) => {
                setSameAsOriginal(e.target.checked);
                if (e.target.checked) {
                  setSelectedCustomer(sourceCustomer ?? null);
                }
              }}
              disabled={isSubmitting}
            />
            {t('bill_split.same_as_original')}
          </label>
          {!sameAsOriginal && (
            <CustomerPicker
              value={selectedCustomer}
              onChange={setSelectedCustomer}
              disabled={isSubmitting}
            />
          )}
          <label className="block space-y-1 text-sm text-gray-700">
            <span>{t('bill_split.guest_label')}</span>
            <input
              type="text"
              name="guest_label"
              maxLength={40}
              value={guestLabel}
              onChange={(e) => setGuestLabel(e.target.value)}
              placeholder={t('bill_split.guest_label_placeholder')}
              className="w-full rounded-md border border-border px-3 py-2 text-sm"
              disabled={isSubmitting}
            />
          </label>
        </div>

        <div className="mx-6 mb-2 grid grid-cols-2 gap-3 rounded-lg bg-gray-50 p-4 text-sm">
          <div>
            <p className="text-gray-500">{t('bill_split.stays_on_bill')}</p>
            <p className="font-semibold text-gray-900">{formatCurrency(totals.staying, currency)}</p>
          </div>
          <div>
            <p className="text-gray-500">{t('bill_split.moves_to_new_bill')}</p>
            <p className="font-semibold text-primary">{formatCurrency(totals.moving, currency)}</p>
          </div>
        </div>

        {error && <p className="px-6 pb-2 text-sm text-red-600">{error}</p>}

        <DialogFooter>
          <Button variant="outline" onClick={() => handleOpenChange(false)} disabled={isSubmitting}>
            {t('common.cancel')}
          </Button>
          <Button onClick={handleConfirm} disabled={isSubmitting || !hasValidSelection}>
            {isSubmitting ? t('common.loading') : t('bill_split.split_confirm')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

function BillSplitDialog(props: BillSplitDialogProps) {
  return props.onConfirmMulti && props.open
    ? <MultiCheckDialog key={props.invoiceName} {...props} onConfirmMulti={props.onConfirmMulti} />
    : <LegacyBillSplitDialog {...props} />;
}

function MultiCheckDialog({
  items, currency, onConfirmMulti, onOpenChange, ...props
}: BillSplitDialogProps & { onConfirmMulti: NonNullable<BillSplitDialogProps['onConfirmMulti']> }) {
  const [mode, setMode] = useState<MultiSplitRequest['mode']>('manual');
  const [count, setCount] = useState('2');
  const [panes, setPanes] = useState<Array<Record<string, number>>>([{}]);
  const [shareByLine, setShareByLine] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [legacy, setLegacy] = useState(false);
  const [error, setError] = useState('');
  const parents = items.filter((item) => !item.custom_parent_line_key && !item.custom_is_modifier && !item.is_disposable);
  const seats = [...new Set(parents.map((item) => item.custom_seat_no || 0))].sort((a, b) => a - b);
  const remainder = (name: string, qty: number) => Math.round(
    (qty - panes.reduce((sum, pane) => sum + (pane[name] || 0), 0)) * 10000,
  ) / 10000;
  const validCount = Number.isInteger(Number(count)) && Number(count) >= 2 && Number(count) <= 20;
  const validManual = parents.some((item) => remainder(item.name, item.qty) > 0)
    && parents.every((item) => remainder(item.name, item.qty) >= 0)
    && panes.every((pane) => Object.values(pane).some((qty) => qty > 0));

  if (legacy) {
    return <LegacyBillSplitDialog {...props} items={items} currency={currency} onOpenChange={onOpenChange} />;
  }

  const setQuantity = (paneIndex: number, name: string, qty: number) => {
    setPanes((current) => current.map((pane, index) => index === paneIndex ? { ...pane, [name]: qty } : pane));
    setError('');
  };
  const applyShare = (item: BillSplitItem, divisor: number) => {
    if (!Number.isInteger(divisor) || divisor < 2 || divisor > 20) return;
    const part = Math.floor(item.qty * 10000 / divisor) / 10000;
    setPanes((current) => Array.from({ length: Math.max(current.length, divisor - 1) }, (_, index) => ({
      ...current[index], [item.name]: index < divisor - 1 ? part : 0,
    })));
    setError('');
  };
  const submit = async () => {
    setBusy(true);
    setError('');
    try {
      if (mode === 'manual') {
        await onConfirmMulti({
          mode, splits: panes.map((pane) => Object.entries(pane).filter(([, qty]) => qty > 0)
            .map(([name, qty]) => ({ name, qty }))),
        });
      } else if (mode === 'even') {
        await onConfirmMulti({ mode, splitCount: Number(count) });
      } else {
        await onConfirmMulti({ mode });
      }
      onOpenChange(false);
    } catch (err) {
      setError(parseFrappeError(err, t('bill_split.split_failed')));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!busy) onOpenChange(open); }}>
      <DialogContent variant="large" size="lg" className="max-h-dialog-max-h overflow-y-auto bg-background text-foreground">
        <DialogHeader>
          <DialogTitle>{t('bill_split.split_bill')}</DialogTitle>
          <DialogDescription>{props.invoiceName}</DialogDescription>
        </DialogHeader>
        <div className="space-y-4 px-4 pb-3 sm:px-6">
          <div className="grid grid-cols-3 gap-1 rounded-lg bg-muted p-1" role="tablist" aria-label={t('bill_split.split_bill')}>
            {(['by_seat', 'even', 'manual'] as const).map((tab, index, tabs) => (
              <button key={tab} type="button" role="tab" id={`split-${tab}`} aria-controls="split-panel"
                aria-selected={mode === tab} tabIndex={mode === tab ? 0 : -1} disabled={busy}
                className={cn('min-h-11 rounded-md px-1 text-sm font-semibold', mode === tab && 'bg-background shadow')}
                onKeyDown={(event) => {
                  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
                  event.preventDefault();
                  const rtl = document.dir === 'rtl';
                  const next = tabs[(index + ((event.key === 'ArrowRight') !== rtl ? 1 : 2)) % 3];
                  setMode(next);
                  document.getElementById(`split-${next}`)?.focus();
                }}
                onClick={() => { setMode(tab); setError(''); }}>{t(`floor.split_${tab}`)}</button>
            ))}
          </div>

          <div role="tabpanel" id="split-panel" aria-labelledby={`split-${mode}`} className="space-y-3">
            {mode === 'by_seat' && (
              <div className="grid gap-2 sm:grid-cols-2">
                {seats.map((seat) => (
                  <section key={seat} className="rounded-lg border p-3">
                    <h3 className="font-semibold">{seat ? t('floor.seat_number', { seat }) : t('floor.table_shared')}</h3>
                    <ul>{parents.filter((item) => (item.custom_seat_no || 0) === seat)
                      .map((item) => <li key={item.name}>{item.qty} × {item.item_name}</li>)}</ul>
                  </section>
                ))}
              </div>
            )}
            {mode === 'even' && (
              <label className="flex items-center justify-between gap-4">
                {t('floor.split_count')}
                <input type="number" min={2} max={20} step={1} value={count} disabled={busy}
                  onChange={(event) => setCount(event.target.value)}
                  className="min-h-11 w-24 rounded border bg-background px-2" />
              </label>
            )}
            {mode === 'manual' && <>
              <div className="space-y-2 rounded-lg border p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="font-semibold">{t('floor.check_number', { check: 1 })}</h3>
                  <button type="button" className="min-h-11 text-sm underline" onClick={() => setLegacy(true)} disabled={busy}>
                    {t('floor.guest_split')}
                  </button>
                </div>
                {parents.map((item) => (
                  <div key={item.name} className="flex flex-wrap items-center justify-between gap-2">
                    <span>{remainder(item.name, item.qty)} × {item.item_name}</span>
                    <label className="flex items-center gap-2 text-sm">
                      <span>{t('floor.share')}</span>
                      <select className="min-h-11 rounded border bg-background px-2" disabled={busy}
                        value={shareByLine[item.name] || ''} onChange={(event) => {
                          const value = event.target.value;
                          setShareByLine((previous) => ({ ...previous, [item.name]: value }));
                          if (value !== 'custom') applyShare(item, Number(value));
                        }}>
                        <option value="">—</option>
                        {[2, 3, 4].map((divisor) => <option key={divisor} value={divisor}>1/{divisor}</option>)}
                        <option value="custom">{t('floor.custom')}</option>
                      </select>
                      {shareByLine[item.name] === 'custom' && <input type="number" min={2} max={20}
                        aria-label={t('floor.split_count')} className="min-h-11 w-16 rounded border bg-background px-2"
                        disabled={busy} onChange={(event) => applyShare(item, Number(event.target.value))} />}
                    </label>
                  </div>
                ))}
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                {panes.map((pane, index) => (
                  <section key={index} className="min-w-0 rounded-lg border p-3">
                    <h3 className="font-semibold">{t('floor.check_number', { check: index + 2 })}</h3>
                    <p className="mb-1 text-sm tabular-nums">{formatCurrency(
                      parents.reduce((sum, item) => sum + item.rate * (pane[item.name] || 0), 0), currency,
                    )}</p>
                    {parents.map((item) => (
                      <label key={item.name} className="flex items-center justify-between gap-2 text-sm">
                        <span>{item.item_name}</span>
                        <input aria-label={`${item.item_name} · ${t('floor.check_number', { check: index + 2 })}`}
                          type="number" min={0} max={item.qty} step={0.0001} disabled={busy}
                          value={pane[item.name] || ''} onChange={(event) => setQuantity(index, item.name, Number(event.target.value))}
                          className="min-h-11 w-24 rounded border bg-background px-2" />
                      </label>
                    ))}
                  </section>
                ))}
              </div>
              <div className="flex flex-wrap gap-2">
                <Button type="button" variant="outline" className="min-h-11" disabled={busy || panes.length >= 19}
                  onClick={() => setPanes([...panes, {}])}>{t('floor.add_check')}</Button>
                {panes.length > 1 && <Button type="button" variant="outline" className="min-h-11" disabled={busy}
                  onClick={() => setPanes(panes.slice(0, -1))}>{t('floor.remove_check')}</Button>}
              </div>
            </>}
          </div>
          {error && <p role="alert" className="text-sm font-semibold text-destructive">{error}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" className="min-h-11" disabled={busy} onClick={() => onOpenChange(false)}>{t('common.cancel')}</Button>
          <Button className="min-h-11" disabled={busy || !(mode === 'manual' ? validManual : mode === 'even' ? validCount : seats.length > 1)}
            onClick={() => void submit()}>{t('bill_split.confirm_split')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default BillSplitDialog;
