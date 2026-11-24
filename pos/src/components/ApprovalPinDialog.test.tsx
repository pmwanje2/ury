import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { initI18n } from '../i18n';
import ApprovalPinDialog from './ApprovalPinDialog';

describe('ApprovalPinDialog (Set my approval PIN + manager clear)', () => {
  beforeEach(async () => {
    await initI18n('en');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('enrols the signed-in user approval PIN after re-entering own password', async () => {
    vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
      if (method === 'ury.ury_pos.override.my_pin_status') {
        return { message: { has_pin: false, external_verifier: false } };
      }
      return { message: [] };
    });
    vi.spyOn(call, 'post').mockResolvedValue({ message: { ok: true, has_pin: true } });

    const user = userEvent.setup();
    render(<ApprovalPinDialog open onOpenChange={vi.fn()} posProfile="Main Till" canManagePins={false} />);

    const pwInput = await screen.findByLabelText(/current password/i);
    const pinInput = screen.getByLabelText(/new approval pin/i);
    const confirmInput = screen.getByLabelText(/confirm approval pin/i);

    await user.type(pwInput, 'MySecretPass1!');
    await user.type(pinInput, '4826');
    await user.type(confirmInput, '9999');
    await user.click(screen.getByRole('button', { name: /save approval pin/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/match/i);
    expect(call.post).not.toHaveBeenCalled();

    await user.clear(confirmInput);
    await user.type(confirmInput, '4826');
    await user.click(screen.getByRole('button', { name: /save approval pin/i }));

    await waitFor(() => {
      expect(call.post).toHaveBeenCalledWith('ury.ury_pos.override.set_my_pin', {
        password: 'MySecretPass1!',
        pin: '4826',
      });
    });

    // Inputs must clear after save
    expect((pwInput as HTMLInputElement).value).toBe('');
    expect((pinInput as HTMLInputElement).value).toBe('');
  });

  it('lets a manager clear another user approval PIN, never read or set it', async () => {
    vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
      if (method === 'ury.ury_pos.override.my_pin_status') {
        return { message: { has_pin: true, external_verifier: false } };
      }
      if (method === 'ury.ury_pos.override.list_pin_users') {
        return {
          message: [
            { user: 'mgr.b@example.com', full_name: 'Bob Supervisor', has_pin: true },
            { user: 'cashier@example.com', full_name: 'Grace Cashier', has_pin: false },
          ],
        };
      }
      return { message: null };
    });
    vi.spyOn(call, 'post').mockResolvedValue({ message: { ok: true, user: 'mgr.b@example.com', has_pin: false } });

    const user = userEvent.setup();
    render(<ApprovalPinDialog open onOpenChange={vi.fn()} posProfile="Main Till" canManagePins />);

    await user.click(await screen.findByRole('button', { name: /clear staff pin/i }));

    expect(await screen.findByText('Bob Supervisor')).toBeInTheDocument();
    expect(screen.getByText('Grace Cashier')).toBeInTheDocument();

    const clearButtons = screen.getAllByRole('button', { name: /clear pin/i });
    expect(clearButtons).toHaveLength(1);
    await user.click(clearButtons[0]);

    await waitFor(() => {
      expect(call.post).toHaveBeenCalledWith('ury.ury_pos.override.clear_user_pin', {
        user: 'mgr.b@example.com',
        pos_profile: 'Main Till',
      });
    });
  });
});
