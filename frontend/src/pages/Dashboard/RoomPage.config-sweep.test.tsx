import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { RoomPage } from './RoomPage';

vi.mock('../../context/BranchContext', () => ({ useBranchContext: () => ({ activeBranchId: 'Patio' }) }));
vi.mock('../../services/dashboard', () => ({
  dashboardService: {
    getModuleRecords: vi.fn(async (doctype: string) =>
      doctype === 'Dining Area Type' ? [{ name: 'Winter Garden' }] : []),
  },
}));
vi.mock('../../i18n', () => ({ __: (text: string) => text }));

it('offers a dining area type configured in the master', async () => {
  render(<RoomPage />);
  fireEvent.click((await screen.findAllByRole('button', { name: /add room/i }))[0]);
  const select = screen.getByRole('combobox', { name: 'Room Type' });
  fireEvent.change(select, { target: { value: 'Winter Garden' } });
  expect((select as HTMLSelectElement).value).toBe('Winter Garden');
});
