// Ported from fork pos/src/lib/pos-profile-api.test.ts (43ea6fb1, 2c925938):
// node:test harness converted to Vitest. Floor sign-out fields belong to T4.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { getCombinedPosProfile } from './pos-profile-api';

afterEach(() => vi.restoreAllMocks());

describe('getCombinedPosProfile closing fields', () => {
  it('carries the blind-count flag and payment-mode accounts from getPosProfile', async () => {
    vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
      if (method === 'ury.ury_pos.api.getPosProfile') {
        return {
          message: {
            pos_profile: 'POS-1',
            custom_blind_cash_count: 1,
            payment_mode_accounts: { Cash: 'Cash - TC', Card: null },
          },
        };
      }
      if (method === 'ury.ury_pos.api.getPosProfileFull') {
        return { message: { name: 'POS-1', custom_blind_cash_count: 0 } };
      }
      throw new Error(`Unexpected call: ${method}`);
    });

    const profile = await getCombinedPosProfile();

    expect(profile.custom_blind_cash_count).toBe(1);
    expect(profile.payment_mode_accounts).toEqual({ Cash: 'Cash - TC', Card: null });
  });

  it('defaults the blind-count flag to off when getPosProfile omits it', async () => {
    vi.spyOn(call, 'get').mockImplementation(async (method: string) => ({
      message: method === 'ury.ury_pos.api.getPosProfile' ? { pos_profile: 'POS-1' } : { name: 'POS-1' },
    }));

    const profile = await getCombinedPosProfile();

    expect(profile.custom_blind_cash_count).toBe(0);
  });
});
