import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { call } from '@ury/core';
import POS from '../src/pages/POS';
import POSOpeningProvider from '../src/components/POSOpeningProvider';
import { usePOSStore } from '../src/store/pos-store';
import { useRootStore } from '../src/store/root-store';
import type { PosProfileCombined } from '../src/lib/pos-profile-api';

const user = { name: 'r5@example.test', roles: ['Cashier'], provisional: true };
const profile = {
  name: 'R5 Till', company: 'Company', currency: 'USD', restaurant: 'Cafe',
  ury_offline_capture: 1, order_type_customer_map: {},
} as PosProfileCombined;
const gateway = { httpStatus: 502, message: 'Bad Gateway' };

beforeEach(() => {
  vi.stubGlobal('indexedDB', new IDBFactory());
  document.cookie = 'user_id=' + encodeURIComponent(user.name) + '; Path=/';
  sessionStorage.setItem('posProfile:' + user.name, JSON.stringify(profile));
  sessionStorage.setItem('ury_offline_opening:' + user.name, JSON.stringify({
    name: 'OPEN-R5', company: profile.company, pos_profile: profile.name,
  }));
  useRootStore.setState({ user });
  // Start after profile hydration. Exercise the real bootstrap, menu requests,
  // opening gate and order panel without depending on profile revalidation IO.
  usePOSStore.setState({
    posProfile: profile, fetchPosProfile: async () => {},
    fetchAvailableOutlets: async () => {}, fetchPaymentModes: async () => {},
  });
});

afterEach(() => {
  cleanup();
  usePOSStore.setState(usePOSStore.getInitialState(), true);
  useRootStore.setState(useRootStore.getInitialState(), true);
  sessionStorage.clear();
  document.cookie = 'user_id=; Max-Age=0; Path=/';
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test.each(['menu', 'courses', 'both'] as const)(
  'confirmed register opens its order panel when %s returns 502 with no cached course list',
  async (outage) => {
    const get = vi.spyOn(call, 'get').mockImplementation(async (method) => {
      if (method.endsWith('.check_opening_entry')) throw gateway;
      if (method.endsWith('.getRestaurantMenu')) {
        if (outage !== 'courses') throw gateway;
        return { message: { items: [{ item: 'Soup', item_name: 'Soup', rate: 3, course: 'Starter' }] } };
      }
      if (method.endsWith('.getMenuCourses')) {
        if (outage !== 'menu') throw gateway;
        return { message: [{ name: 'Starter', label: 'Starter' }] };
      }
      return { message: [] };
    });
    await usePOSStore.getState().initializeApp();
    render(<MemoryRouter initialEntries={['/register']}>
      <POSOpeningProvider><POS /></POSOpeningProvider>
    </MemoryRouter>);

    expect(await screen.findByTestId('order-panel')).toBeVisible();
    for (const method of ['check_opening_entry', 'getRestaurantMenu', 'getMenuCourses']) {
      expect(get.mock.calls.some(([name]) => name.endsWith('.' + method))).toBe(true);
    }
    if (outage === 'courses') {
      expect(usePOSStore.getState().categories.map((course) => course.name)).toEqual(['Starter']);
      expect(await screen.findByText('Soup')).toBeVisible();
    }
  },
);

test.each([
  { status: 403, optedIn: true },
  { status: 502, optedIn: false },
])('keeps the normal register bootstrap error for HTTP $status, offline capture $optedIn', async ({ status, optedIn }) => {
  const till = { ...profile, ury_offline_capture: Number(optedIn) };
  sessionStorage.setItem('posProfile:' + user.name, JSON.stringify(till));
  usePOSStore.setState({ posProfile: till });
  vi.spyOn(call, 'get').mockRejectedValue({ httpStatus: status });
  await usePOSStore.getState().initializeApp();
  expect(usePOSStore.getState().error).toBeTruthy();
});
