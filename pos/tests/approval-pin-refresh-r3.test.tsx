import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import AppLayout from '../src/components/AppLayout';
import Settings from '../src/pages/Settings';
import * as api from '../src/lib/override-api';
import { useRootStore } from '../src/store/root-store';

vi.mock('../src/components/PrinterWatch', () => ({ PrinterStatusButton: () => null }));
vi.mock('../src/components/POSCloseFlow', () => ({ default: () => null }));
vi.mock('../src/components/TimeclockButton', () => ({ default: () => null }));

const enrolled = { has_pin: true, external_verifier: false };
const cleared = { ...enrolled, has_pin: false };

beforeEach(() => {
  useRootStore.setState({ user: { name: 'lee', full_name: 'Lee', roles: ['URY Cashier'] } });
  vi.spyOn(call, 'get').mockResolvedValue({ message: [] });
});
afterEach(() => vi.restoreAllMocks());

function renderTill(path = '/pos/settings') {
  return render(
    <MemoryRouter basename="/pos" initialEntries={[path]}>
      <Routes>
        <Route element={<AppLayout />}>
          <Route path="/settings" element={<main><Settings /></main>} />
          <Route path="/orders" element={<main>Orders</main>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

async function openPin(from: 'Header' | 'Settings', user: ReturnType<typeof userEvent.setup>) {
  if (from === 'Header') {
    await user.click(screen.getByRole('button', { name: 'Lee' }));
    await user.click(screen.getByRole('button', { name: 'My approval PIN' }));
  } else {
    await user.click(screen.getByRole('button', { name: 'Manage my approval PIN' }));
  }
  return within(await screen.findByRole('dialog'));
}

it.each(['Header', 'Settings'] as const)('reloads a remotely cleared PIN when reopened from %s', async from => {
  const status = vi.spyOn(api, 'getMyPinStatus').mockResolvedValue(enrolled);
  const user = userEvent.setup();
  renderTill();
  const settings = within(screen.getByRole('main'));
  expect(await settings.findByText('Approval PIN set')).toBeVisible();
  const first = await openPin(from, user);
  expect(await first.findByText('Approval PIN set')).toBeVisible();
  await user.keyboard('{Escape}');
  const previousReads = status.mock.calls.length;
  status.mockResolvedValue(cleared);
  const reopened = await openPin(from, user);
  expect(status).toHaveBeenCalledTimes(previousReads + 1);
  expect(await reopened.findByText('No approval PIN set')).toBeVisible();
  expect(settings.getByText('No approval PIN set')).toBeVisible();
});

it('loads external management status on the mixed-case Settings route', async () => {
  const status = vi.spyOn(api, 'getMyPinStatus').mockResolvedValue({
    has_pin: true, external_verifier: true, can_manage_here: false,
  });
  renderTill('/pos/Settings');
  const settings = within(screen.getByRole('main'));
  expect(await settings.findByText('Managed externally')).toBeVisible();
  expect(status).toHaveBeenCalledTimes(1);
  expect(settings.queryByText(/using your password/i)).toBeNull();
});

it('ignores an earlier Settings read that completes after the dialog read', async () => {
  let finishInitial!: (value: api.MyPinStatus) => void;
  const initial = new Promise<api.MyPinStatus>(resolve => { finishInitial = resolve; });
  vi.spyOn(api, 'getMyPinStatus').mockReturnValueOnce(initial).mockResolvedValue(cleared);
  renderTill();
  const dialog = await openPin('Header', userEvent.setup());
  await act(async () => { finishInitial(enrolled); });
  expect(await dialog.findByText('No approval PIN set')).toBeVisible();
  expect(within(screen.getByRole('main')).getByText('No approval PIN set')).toBeVisible();
});

it('makes a single status request for a Header open away from Settings', async () => {
  const status = vi.spyOn(api, 'getMyPinStatus').mockResolvedValue(enrolled);
  renderTill('/pos/orders');
  expect(status).not.toHaveBeenCalled();
  const dialog = await openPin('Header', userEvent.setup());
  expect(await dialog.findByText('Approval PIN set')).toBeVisible();
  expect(status).toHaveBeenCalledTimes(1);
});
