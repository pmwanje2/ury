import assert from 'node:assert/strict';
import { mock } from 'node:test';
import { call, db } from '@ury/core';
import { getCombinedPosProfile } from './pos-profile-api';

mock.method(call, 'get', async () => ({
  message: {
    pos_profile: 'POS-1',
    custom_blind_cash_count: 1,
    floor_idle_signout_seconds: 60,
    floor_signout_after_send: 1,
  },
}));
mock.method(db, 'getDoc', async () => ({ name: 'POS-1', custom_blind_cash_count: 0 }));
mock.method(console, 'log', () => {});

try {
  const profile = await getCombinedPosProfile();
  assert.equal(profile.custom_blind_cash_count, 1, 'The combined profile must use the exposed API flag');
  assert.equal(profile.floor_idle_signout_seconds, 60, 'The combined profile must carry the idle sign-out setting');
  assert.equal(profile.floor_signout_after_send, 1, 'The combined profile must carry the after-send setting');
} finally {
  mock.restoreAll();
}
console.log('PASS: the combined POS Profile carries its floor-session API flags (3 assertions).');
