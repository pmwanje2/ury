import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import FloorPinPad from '../src/components/FloorPinPad';
import TimeclockButton from '../src/components/TimeclockButton';

vi.mock('@ury/core', async (importOriginal) => ({
  ...await importOriginal<typeof import('@ury/core')>(),
  call: { get: vi.fn(), post: vi.fn() },
}));

vi.mock('../src/i18n', () => ({ t: (value: string) => value }));

describe('POS outlet configuration', () => {
  beforeEach(() => vi.resetAllMocks());

  it('lets staff finish a variable-length floor PIN explicitly', async () => {
    Object.defineProperty(window, 'floor_pad', {
      configurable: true, value: { paired: true, till_open: true, min_length: 5, max_length: 6 },
    });
    vi.mocked(call.post).mockRejectedValue(new Error('Refused'));
    render(<FloorPinPad />);
    for (const digit of ['0', '1', '2', '3', '4']) fireEvent.click(screen.getByRole('button', { name: digit }));
    expect(call.post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'floor_pin.sign_in' }));
    await waitFor(() => expect(call.post).toHaveBeenCalledWith('ury.ury_pos.floor_pin.sign_in', { pin: '01234' }));
    delete window.floor_pad;
  });

  it('requires staff to choose their configured job code and sends it with the punch', async () => {
    vi.mocked(call.get).mockResolvedValue({ message: {
      available: true, has_employee: true, employee_name: 'Grace',
      is_clocked_in: false, job_codes: ['Server', 'Bartender'],
    } });
    vi.mocked(call.post).mockResolvedValue({ message: { available: true, is_clocked_in: true } });
    render(<TimeclockButton posProfile="Patio POS" />);
    fireEvent.click(await screen.findByRole('button', { name: 'timeclock.clock_in' }));
    const confirm = screen.getByRole('button', { name: 'timeclock.confirm_clock_in' });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByRole('combobox', { name: 'timeclock.job_code' }), { target: { value: 'Bartender' } });
    fireEvent.click(confirm);
    await waitFor(() => expect(call.post).toHaveBeenCalledWith('ury.ury_pos.timeclock.punch_timeclock',
      { action: 'IN', pos_profile: 'Patio POS', job_code: 'Bartender' }));
  });
});
