import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { UserPage } from '../src/pages/Dashboard/UserPage';

vi.mock('@ury/core', () => ({ call: vi.fn(async () => ({ message: [] })) }));
vi.mock('../src/services/dashboard', () => ({
  dashboardService: {
    getModuleRecords: vi.fn(async () => [
      { name: 'cashier@example.test', email: 'cashier@example.test', first_name: 'Cash', roles: [{ role: 'URY Cashier' }] },
    ]),
  },
}));
vi.mock('../src/context/BranchContext', () => ({ useBranchContext: () => ({ activeBranchId: 'all' }) }));

beforeEach(() => vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true));
afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

it('shows the Restaurant POS label in the users table, not the raw role', async () => {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(createElement(UserPage)));
  const cell = container.querySelector('tbody td:nth-child(3)');
  expect(cell?.textContent).toBe('Restaurant POS Cashier');
  act(() => root.unmount());
});
