import { beforeEach, expect, it, vi } from 'vitest';

// M02: getPosProfile reports the POS Profile's room lookup method; the
// combined profile the till reads must carry it ("" when unset).

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

it('carries the configured room lookup method into the combined profile', async () => {
  answer({ room_lookup_method: 'pms_bridge.room_charge_lookup' });
  const profile = await getCombinedPosProfile();
  expect(profile.room_lookup_method).toBe('pms_bridge.room_charge_lookup');
});

it('an older server without the key gives an empty method, so the till keeps free text', async () => {
  answer({});
  const profile = await getCombinedPosProfile();
  expect(profile.room_lookup_method).toBe('');
});
