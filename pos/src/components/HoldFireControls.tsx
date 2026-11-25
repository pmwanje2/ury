import { useEffect, useState } from 'react';
import { Badge, Button, Input, showToast } from '@ury/ui';
import { parseFrappeError } from '@ury/core';
import { usePOSStore, type OrderItem } from '../store/pos-store';
import { useOnline } from '../offline/use-online';
import { fireItems, scheduleItems, type HeldSelection, type POSInvoiceItem } from '../lib/order-api';
import { getRealtimeSocket } from '../lib/realtime';
import { t } from '../i18n';

interface HeldUpdate {
  invoice: string;
  previous_modified: string;
  modified: string;
  items: Pick<POSInvoiceItem, 'name' | 'ury_held' | 'fire_at' | 'fire_confirmation_pending'>[];
}

function applyHeldUpdate(invoice: string, expectedVersion: string | null, modified: string | null, rows: HeldUpdate['items']) {
  usePOSStore.setState(state => {
    // Keep local edits and don't advance the version over unrelated server edits.
    if (state.orderId !== invoice || state.lastModifiedTime !== expectedVersion) return {};
    const byName = new Map(rows.map(row => [row.name, row]));
    return {
      activeOrders: state.activeOrders.map(item => {
        const row = item.invoiceItemName && byName.get(item.invoiceItemName);
        return row ? {
          ...item,
          ury_held: row.ury_held || 0,
          fireAt: row.fire_at,
          fireConfirmationPending: !!row.fire_confirmation_pending,
        } : item;
      }),
      lastModifiedTime: modified,
    };
  });
}

function useHeldUpdates(invoice: string | null) {
  useEffect(() => {
    if (!invoice) return;
    let cancelled = false;
    let unsubscribe: (() => void) | undefined;
    const onUpdate = (update: HeldUpdate) => {
      if (update.invoice === invoice) {
        applyHeldUpdate(invoice, update.previous_modified, update.modified, update.items);
      }
    };
    void getRealtimeSocket().then(socket => {
      if (cancelled) return;
      const subscribe = () => socket.emit('doc_subscribe', 'POS Invoice', invoice);
      socket.on('connect', subscribe);
      socket.on('held_kitchen_items_updated', onUpdate);
      if (socket.connected) subscribe();
      unsubscribe = () => {
        socket.off('connect', subscribe);
        socket.off('held_kitchen_items_updated', onUpdate);
        socket.emit('doc_unsubscribe', 'POS Invoice', invoice);
      };
    }).catch(() => {});
    return () => {
      cancelled = true;
      unsubscribe?.();
    };
  }, [invoice]);
}

function FireCountdown({ fireAt, timezone }: { fireAt: string; timezone?: string }) {
  const [clock, setClock] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  // Fire At is stored on the outlet clock. Compare wall times in the same
  // zone even if the device is configured with a different timezone.
  const formatter = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  });
  const parts = Object.fromEntries(formatter.formatToParts(clock).map(({ type, value }) => [type, value]));
  const now = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
  const deadline = Date.parse(fireAt.replace(' ', 'T') + 'Z');
  const seconds = Math.max(0, Math.ceil((deadline - now) / 1000));
  const time = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  return <Badge variant="secondary">{seconds ? t('hold_fire.countdown', { time }) : t('hold_fire.due')}</Badge>;
}

const pendingOrders = new Set<string>();

function FireActions({ selection, label, disabled, fireAt, pending }: {
  selection: HeldSelection;
  label?: string;
  disabled?: boolean;
  fireAt?: string | null;
  pending?: boolean;
}) {
  const { orderId, lastModifiedTime, posProfile } = usePOSStore();
  const [delay, setDelay] = useState<number | undefined>();
  const [busy, setBusy] = useState(false);
  const minutes = delay ?? posProfile?.default_fire_delay_minutes ?? 10;
  const change = async (schedule: boolean) => {
    if (!orderId || pendingOrders.has(orderId)) return;
    pendingOrders.add(orderId);
    setBusy(true);
    try {
      const args = { ...selection, last_modified_time: lastModifiedTime || undefined };
      const { message } = schedule
        ? await scheduleItems(orderId, { ...args, delay_minutes: minutes })
        : await fireItems(orderId, args);
      const previousVersion = message.held_update_previous_modified;
      if (previousVersion && previousVersion !== lastModifiedTime) {
        if (usePOSStore.getState().lastModifiedTime !== message.modified) {
          showToast.info(t('hold_fire.reload'));
        }
      } else {
        applyHeldUpdate(message.name, lastModifiedTime, message.modified || null, message.items);
      }
    } catch (error) {
      const message = parseFrappeError(error, t('hold_fire.failed')).replace(/\bURY_HOLD_DISABLED:\s*/g, '');
      showToast.error(message);
    } finally {
      pendingOrders.delete(orderId);
      setBusy(false);
    }
  };
  return <div className="flex flex-wrap items-center gap-2 text-sm">
    {label && <span className="font-medium">{label}</span>}
    {fireAt && <FireCountdown fireAt={fireAt} timezone={posProfile?.fire_time_zone || undefined} />}
    {pending && <Badge variant="secondary">{t('hold_fire.confirmation_pending')}</Badge>}
    <select className="h-8 rounded-md border border-input bg-background px-2 text-foreground"
      aria-label={t('hold_fire.delay')} value={minutes} disabled={disabled || busy}
      onChange={event => setDelay(Number(event.target.value))}>
      {[...new Set([10, 15, 20, posProfile?.default_fire_delay_minutes || 10])].sort((a, b) => a - b).map(value =>
        <option key={value} value={value}>{t('hold_fire.minutes', { minutes: value })}</option>)}
    </select>
    <Button type="button" size="sm" variant="outline" disabled={disabled || busy} onClick={() => void change(true)}>
      {t(label ? 'hold_fire.schedule_course' : 'hold_fire.schedule_line')}
    </Button>
    <Button type="button" size="sm" variant="outline" disabled={disabled || busy} onClick={() => void change(false)}>
      {t('hold_fire.fire_now')}
    </Button>
  </div>;
}

function HoldFireLine({ item, disabled }: { item: OrderItem; disabled?: boolean }) {
  const setItemHeld = usePOSStore(state => state.setItemHeld);
  if (item.parentLineKey) return null;
  if (!item.invoiceItemName) return <label className="mt-2 flex items-center gap-2 text-sm">
    <Input type="checkbox" className="h-4 w-4 p-0" checked={!!item.ury_held} disabled={disabled}
      onChange={event => item.uniqueId && setItemHeld(item.uniqueId, event.target.checked)} />
    {t('hold_fire.hold')}
  </label>;
  if (!item.ury_held) return null;
  return <div className="mt-2 space-y-1">
    <Badge variant="secondary">{t('hold_fire.held')}</Badge>
    <FireActions selection={{ line: item.invoiceItemName }} disabled={disabled}
      fireAt={item.fireAt} pending={item.fireConfirmationPending} />
  </div>;
}

/** Show shared course actions; render each item's controls on its existing cart row. */
export function HoldFireControls({ lineId, disabled }: { lineId?: string; disabled?: boolean } = {}) {
  const { activeOrders, posProfile, orderId, orderLoading } = usePOSStore();
  const online = useOnline();
  const enabled = !!posProfile?.ury_enable_hold_fire && online;
  const held = activeOrders.filter(item => item.ury_held && !item.parentLineKey);
  useHeldUpdates(enabled && !lineId && held.length ? orderId : null);
  if (!enabled) return null;
  if (lineId) {
    const item = activeOrders.find(row => row.uniqueId === lineId);
    return item ? <HoldFireLine item={item} disabled={disabled || orderLoading} /> : null;
  }
  const courses = new Map<string, OrderItem[]>();
  for (const row of held) {
    if (!row.invoiceItemName || !row.course) continue;
    const rows = courses.get(row.course) || [];
    rows.push(row);
    courses.set(row.course, rows);
  }
  const unsaved = held.some(row => !row.invoiceItemName);
  if (!courses.size && !unsaved) return null;
  return <section aria-label={t('hold_fire.title')} className="my-2 space-y-2 rounded-md border border-border bg-background p-3 text-foreground">
    <h3 className="text-sm font-semibold">{t('hold_fire.title')}</h3>
    {[...courses].map(([course, rows]) => {
      const fireAt = rows[0].fireAt;
      return <FireActions key={course} selection={{ course }} label={course}
        disabled={disabled || orderLoading}
        fireAt={rows.every(row => row.fireAt === fireAt) ? fireAt : null}
        pending={rows.some(row => row.fireConfirmationPending)} />;
    })}
    {unsaved && <p className="text-xs text-muted-foreground">{t('hold_fire.save_first')}</p>}
  </section>;
}
