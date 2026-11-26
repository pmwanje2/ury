import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { ApprovalPinProvider } from '../src/components/ApprovalPinProvider';
import Settings from '../src/pages/Settings';

const { permission, status } = vi.hoisted(() => ({ permission: vi.fn(), status: vi.fn() }));
vi.mock('@ury/core', async (original) => ({ ...await original<object>(), call: { get: permission } }));
vi.mock('../src/store/root-store', () => ({
  useRootStore: (select: Function) => select({ user: { name: 'operator', roles: [] } }),
}));
vi.mock('../src/store/pos-store', () => ({
  usePOSStore: (select?: Function) => {
    const state = { posProfile: { name: 'Till A' } };
    return select ? select(state) : state;
  },
}));
vi.mock('../src/lib/override-api', async (original) => ({ ...await original<object>(), getMyPinStatus: status }));
vi.mock('../src/components/POSCloseFlow', () => ({ default: () => null }));

function renderSettings() {
  return render(<MemoryRouter initialEntries={['/settings']}><ApprovalPinProvider>
    <Settings />
  </ApprovalPinProvider></MemoryRouter>);
}

beforeEach(() => {
  permission.mockReset().mockResolvedValue({ message: { has_permission: true } });
  status.mockReset().mockResolvedValue({ has_pin: true, must_reset: true, locked: true });
});

it('shows personal status and opens both approval PIN tabs for a manager', async () => {
  renderSettings();
  expect(screen.getByRole('heading', { name: /security/i })).toBeVisible();
  expect(screen.queryByText(/coming soon/i)).toBeNull();
  for (const badge of ['Approval PIN set', 'Reset required', 'Locked']) {
    expect(await screen.findByText(badge)).toBeVisible();
  }
  fireEvent.click(screen.getByRole('button', { name: /my approval pin/i }));
  const dialog = within(await screen.findByRole('dialog'));
  expect(await dialog.findByText('Current password')).toBeVisible();
  expect(await dialog.findByText('Clear staff approval PINs')).toBeVisible();
});

it.each(['Staff PINs', 'Pair this tablet'])('opens the authorized %s dialog', async (name) => {
  renderSettings();
  fireEvent.click(await screen.findByRole('button', { name }));
  expect(await screen.findByRole('dialog', { name })).toBeVisible();
});

it.each([false, '1', 'error'])('fails closed for permission result %j', async (result) => {
  if (result === 'error') permission.mockRejectedValue(new Error('offline'));
  else permission.mockResolvedValue({ message: { has_permission: result } });
  status.mockResolvedValue({ has_pin: false });
  renderSettings();
  await waitFor(() => expect(permission).toHaveBeenCalledWith('frappe.client.has_permission',
    { doctype: 'POS Profile', docname: 'Till A', perm_type: 'write' }));
  expect(await screen.findByText('No approval PIN set')).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Staff PINs' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Pair this tablet' })).toBeNull();
});
