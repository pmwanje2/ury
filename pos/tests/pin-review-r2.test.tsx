import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import AppLayout from '../src/components/AppLayout';
import Settings from '../src/pages/Settings';
import POSOpeningScreen from '../src/components/POSOpeningScreen';
import ScreenSizeDialog from '../src/components/ScreenSizeDialog';
import { homePath } from '../src/lib/home-route';
import { useRootStore } from '../src/store/root-store';
import { usePOSStore } from '../src/store/pos-store';
import { getPOSOpeningContext } from '../src/lib/pos-opening-api';
import * as pin from '../src/lib/override-api';

vi.mock('../src/components/PrinterWatch', () => ({ PrinterStatusButton: () => null }));
vi.mock('../src/components/POSCloseFlow', () => ({ default: () => null }));
vi.mock('../src/components/TimeclockButton', () => ({ default: () => null }));
vi.mock('../src/lib/pos-opening-api', async (original) => ({
  ...await original<object>(), getPOSOpeningContext: vi.fn(),
}));

beforeEach(() => {
  useRootStore.setState({ user: { name: 'lee', full_name: 'Lee', roles: ['URY Manager'] } });
  usePOSStore.setState({ posProfile: { name: 'Till' } as NonNullable<ReturnType<typeof usePOSStore.getState>['posProfile']> });
  vi.spyOn(call, 'get').mockImplementation(async method => ({
    message: method === 'frappe.client.has_permission' ? { has_permission: true } : [],
  }));
});
afterEach(() => vi.restoreAllMocks());

function renderSettings() {
  render(<MemoryRouter initialEntries={['/settings']}><Routes><Route element={<AppLayout />}>
    <Route path="/settings" element={<main><Settings /></main>} />
  </Route></Routes></MemoryRouter>);
  return within(screen.getByRole('main'));
}

it.each([
  [[], '/order'],
  [['Guest'], '/order'],
  ...['URY Floor Lead', 'Floor Lead', 'URY Cashier', 'URY Manager', 'Sales User', 'System Manager']
    .map(role => [[role], '/orders']),
])('routes a system-user captain with additional roles %j to %s', (extra, expected) => {
  expect(homePath({ name: 'lee', roles: ['URY Captain', 'Desk User', 'All', ...extra] })).toBe(expected);
});

it('offers Back Office on the small-screen gate', () => {
  render(<ScreenSizeDialog />);
  expect(screen.getByRole('button', { name: 'Open Back Office' })).toBeVisible();
});

it('uses Back Office in the occupied till instructions', async () => {
  vi.mocked(getPOSOpeningContext).mockResolvedValue({
    selected_profile: 'Till', allowed_profiles: [{ name: 'Till' }], payment_modes: [],
    permissions: { create: true, submit: true }, multi_cashier: { enabled: false },
    occupied_entry: { joinable: false, user_full_name: 'Other cashier', period_start_date: '2026-11-10 08:00:00' },
  } as Awaited<ReturnType<typeof getPOSOpeningContext>>);
  render(<POSOpeningScreen />);
  expect(await screen.findByText(/must close the shift/)).toHaveTextContent('Back Office');
});

it.each(['en', 'fr', 'es', 'ar'])('uses white-label copy and distinct credential labels in %s', async locale => {
  const data = (await import(`../src/i18n/locales/${locale}.json`)).default;
  expect.soft(JSON.stringify(data)).not.toMatch(/\bDesk\b/i);
  expect.soft(data.pos_opening.occupied_till).toBeTruthy();
  expect.soft(data.approval_pin.tab_clear_staff).not.toBe(data.header.staff_pins);
  if (locale === 'en') expect(data.approval_pin.tab_clear_staff).toBe('Clear staff approval PINs');
});

it('shows externally managed status and omits self-enrollment instructions', async () => {
  vi.spyOn(pin, 'getMyPinStatus').mockResolvedValue({ has_pin: true, can_manage_here: false, external_verifier: true });
  const settings = renderSettings();
  expect(await settings.findByText('Managed externally')).toBeVisible();
  expect(settings.queryByText(/using your password/i)).toBeNull();
  expect(settings.queryByText('Approval PIN set')).toBeNull();
});

it('shares Header PIN mutations with the mounted Settings screen through Escape', async () => {
  let hasPin = false;
  vi.spyOn(pin, 'getMyPinStatus').mockImplementation(async () => ({ has_pin: hasPin, external_verifier: false }));
  vi.spyOn(pin, 'setMyPin').mockImplementation(async () => ({ ok: true, has_pin: hasPin = true }));
  vi.spyOn(pin, 'listPinUsers').mockResolvedValue([{ user: 'sam', full_name: 'Sam', has_pin: true }]);
  vi.spyOn(pin, 'clearUserPin').mockResolvedValue({ ok: true, has_pin: false, user: 'sam' });
  const user = userEvent.setup();
  const settings = renderSettings();
  expect(await settings.findByText('No approval PIN set')).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Lee' }));
  await user.click(screen.getByRole('button', { name: 'My approval PIN' }));
  const dialog = within(await screen.findByRole('dialog'));
  for (const [label, value] of [['Current password', 'test-password'], ['New approval PIN', '4826'], ['Confirm approval PIN', '4826']]) {
    fireEvent.change(await dialog.findByLabelText(label), { target: { value } });
  }
  await user.click(dialog.getByRole('button', { name: 'Save approval PIN' }));
  expect(await settings.findByText('Approval PIN set')).toBeInTheDocument();
  await user.click(dialog.getByRole('button', { name: 'Clear staff approval PINs' }));
  await user.click(await dialog.findByRole('button', { name: 'Clear PIN' }));
  expect(await dialog.findByText('No approval PIN set')).toBeVisible();
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(settings.getByText('Approval PIN set')).toBeVisible();
});
