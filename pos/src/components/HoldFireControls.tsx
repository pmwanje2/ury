import { useState } from 'react';
import { Button, showToast } from '@ury/ui';
import { parseFrappeError } from '@ury/core';
import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';
import { useOutbox } from '../offline/use-outbox';
import { fireItems } from '../lib/order-api';
import { t } from '../i18n';

/** Staff controls; quantities, course selection and authority stay in URY. */
export function HoldFireControls() {
  const { activeOrders, posProfile, orderId, lastModifiedTime, setItemHeld, orderLoading } = usePOSStore();
  const user = useRootStore(state => state.user);
  const { offline } = useOutbox(posProfile, user);
  const [busy, setBusy] = useState(false);
  if (!posProfile?.ury_enable_hold_fire || offline || navigator.onLine === false) return null;
  const fire = async (selection: { line?: string; course?: string }) => {
    if (!orderId) return;
    setBusy(true);
    try {
      const { message } = await fireItems(orderId, { ...selection, last_modified_time: lastModifiedTime || undefined });
      // Preserve unsaved quantity/note edits and new rows for the next sync.
      usePOSStore.setState(state => state.orderId !== message.name ? {} : {
        activeOrders: state.activeOrders.map(item => {
          const row = message.items.find(row => row.name === item.invoiceItemName);
          return row ? { ...item, ury_held: row.ury_held || 0 } : item;
        }),
        lastModifiedTime: message.modified || null,
      });
    } catch (error) {
      showToast.error(parseFrappeError(error, t('hold_fire.failed')));
    } finally { setBusy(false); }
  };
  const courses = [...new Set(activeOrders.filter(row => row.ury_held && row.invoiceItemName && row.course).map(row => row.course))];
  return <section aria-label={t('hold_fire.title')} className="border-t border-gray-200 p-3 space-y-2">
    <h3 className="text-sm font-semibold">{t('hold_fire.title')}</h3>
    {activeOrders.map(item => <div key={item.uniqueId} className="flex items-center gap-2 text-sm">
      <span className="flex-1">{item.name} × {item.quantity}</span>
      <span className={item.ury_held ? 'text-amber-700 font-semibold' : 'text-green-700'}>
        {t(item.ury_held ? 'hold_fire.held' : item.invoiceItemName ? 'hold_fire.sent' : 'hold_fire.ready')}
      </span>
      {!item.invoiceItemName && <label className="flex items-center gap-1">
        <input type="checkbox" checked={!!item.ury_held} disabled={busy || orderLoading}
          onChange={event => setItemHeld(item.uniqueId!, event.target.checked)} />{t('hold_fire.hold')}
      </label>}
      {!!item.ury_held && item.invoiceItemName && <Button size="sm" disabled={busy || orderLoading}
        onClick={() => void fire({ line: item.invoiceItemName })}>{t('hold_fire.fire_line')}</Button>}
    </div>)}
    {courses.map(course => <Button key={course} size="sm" variant="outline" disabled={busy || orderLoading}
      onClick={() => void fire({ course })}>{t('hold_fire.fire_course', { course })}</Button>)}
    {activeOrders.some(item => item.ury_held && !item.invoiceItemName) &&
      <p className="text-xs text-gray-500">{t('hold_fire.save_first')}</p>}
  </section>;
}
