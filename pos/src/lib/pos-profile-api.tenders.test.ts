import { beforeEach, expect, it, vi } from 'vitest';

// G17/G21: getPosProfile reports the till's tender configuration: the POS Profile's default
// payment method and the room-charge methods (those posting to Guest Ledger Clearing). The
// combined profile the till reads must carry both.

const get = vi.hoisted(() => vi.fn());
vi.mock('@ury/core', () => ({ call: { get }, db: {} }));

import { getCombinedPosProfile } from './pos-profile-api';

function answer(limited: Record<string, unknown>) {
  get.mockImplementation(async (method: string) => {
    if (method === 'ury.ury_pos.api.getPosProfile') {
      return { message: { pos_profile: 'Till A', owner: 'o', waiter: 'w', cashier: 'c', ...limited } };
    }
    return { message: { name: 'Till A' } };
  });
}

beforeEach(() => {
  get.mockReset();
  vi.spyOn(console, 'log').mockImplementation(() => {});
});

it('carries the default tender and the room-charge tenders into the combined profile', async () => {
  answer({ default_payment_mode: 'Mobile Money', room_charge_modes: ['Guest Folio'] });
  const profile = await getCombinedPosProfile();
  expect(profile.default_payment_mode).toBe('Mobile Money');
  expect(profile.room_charge_modes).toEqual(['Guest Folio']);
});

it('an older server without the keys prefills nothing and has no room-charge tender', async () => {
  answer({});
  const profile = await getCombinedPosProfile();
  expect(profile.default_payment_mode).toBe('');
  expect(profile.room_charge_modes).toEqual([]);
});

it('carries room_charge_unconfigured into the combined profile and defaults to false', async () => {
  answer({ room_charge_modes: [], room_charge_unconfigured: true });
  expect((await getCombinedPosProfile()).room_charge_unconfigured).toBe(true);

  answer({ room_charge_modes: [] });
  expect((await getCombinedPosProfile()).room_charge_unconfigured).toBe(false);
});

it('falls back to getPosProfile currency when full profile currency is unset', async () => {
  answer({ currency: 'UGX' });
  expect((await getCombinedPosProfile()).currency).toBe('UGX');
});
