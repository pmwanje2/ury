import { afterEach, expect, test, vi } from 'vitest';
import { call } from '@ury/core';
import { initI18n, t } from '../src/i18n';
import { fireItems, scheduleItems, syncOrder, type SyncOrderRequest } from '../src/lib/order-api';

afterEach(() => vi.restoreAllMocks());

test.each([
  ['en', 'hold and fire'],
  ['fr', 'la mise en attente et l’envoi en cuisine'],
  ['es', 'la retención y el envío a cocina'],
  ['ar', 'تأجيل الأصناف وإرسالها إلى المطبخ'],
])('held-order offline refusals use a localized action in %s', async (locale, action) => {
  await initI18n(locale);
  vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false);
  const post = vi.spyOn(call, 'post');
  const message = t('offline.refused', { action });
  for (const run of [
    () => syncOrder({ items: [{ ury_held: 1 }] } as SyncOrderRequest),
    () => fireItems('check', { line: 'item' }),
    () => scheduleItems('check', { line: 'item', delay_minutes: 10 }),
  ]) {
    await expect(run()).rejects.toThrow(message);
  }
  expect(post).not.toHaveBeenCalled();
});

test('a register 502 localizes held-order refusals while preserving ordinary offline replay', async () => {
  await initI18n('fr');
  vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(true);
  const gatewayFailure = { httpStatus: 502, message: '502 Bad Gateway' };
  vi.spyOn(call, 'post').mockRejectedValue(gatewayFailure);
  const message = t('offline.refused', { action: 'la mise en attente et l’envoi en cuisine' });

  for (const run of [
    () => syncOrder({ items: [{ ury_held: 1 }] } as SyncOrderRequest),
    () => fireItems('check', { line: 'item' }),
    () => scheduleItems('check', { line: 'item', delay_minutes: 10 }),
  ]) {
    await expect(run()).rejects.toMatchObject({ httpStatus: 502, message });
  }
  await expect(syncOrder({ items: [{ ury_held: 0 }] } as SyncOrderRequest)).rejects.toBe(gatewayFailure);
});
