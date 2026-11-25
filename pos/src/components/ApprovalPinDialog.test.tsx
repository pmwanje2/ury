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
        pin_secret: '4826',
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

    await user.click(await screen.findByRole('button', { name: /staff pins/i }));

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

  it('hides Clear staff PIN tab when external_verifier manages PINs and displays neutral wrong-password refusal', async () => {
    vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
      if (method === 'ury.ury_pos.override.my_pin_status') {
        return { message: { has_pin: true, external_verifier: true } };
      }
      return { message: [] };
    });

    const { rerender } = render(<ApprovalPinDialog open onOpenChange={vi.fn()} posProfile="Main Till" canManagePins />);
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /staff pins/i })).not.toBeInTheDocument();
    });

    // When built-in store is active and password is wrong, show neutral POS-AUTH-PASSWORD message
    vi.spyOn(call, 'get').mockResolvedValue({ message: { has_pin: false, external_verifier: false } });
    vi.spyOn(call, 'post').mockRejectedValue(new Error('Your current password did not match. [POS-AUTH-PASSWORD]'));
    rerender(<ApprovalPinDialog open={false} onOpenChange={vi.fn()} posProfile="Main Till" canManagePins={false} />);
    rerender(<ApprovalPinDialog open onOpenChange={vi.fn()} posProfile="Main Till" canManagePins={false} />);

    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/current password/i), 'wrong');
    await user.type(screen.getByLabelText(/new approval pin/i), '4826');
    await user.type(screen.getByLabelText(/confirm approval pin/i), '4826');
    await user.click(screen.getByRole('button', { name: /save approval pin/i }));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/password/i);
    expect(alert.textContent).not.toMatch(/POS-AUTH-PASSWORD|URY|HUF|Kamra/i);
  });
  it('shows external PIN status and lets staff enrol a PIN when management is enabled', async () => {
    vi.spyOn(call, 'get')
      .mockResolvedValueOnce({
        message: { has_pin: false, external_verifier: true, can_manage_here: true, must_reset: true, locked: true },
      })
      .mockResolvedValue({
        message: { has_pin: true, external_verifier: true, can_manage_here: true, must_reset: false, locked: false },
      });
    const save = vi.spyOn(call, 'post').mockResolvedValue({ message: { ok: true, has_pin: true } });

    const user = userEvent.setup();
    render(<ApprovalPinDialog open onOpenChange={vi.fn()} />);

    expect(await screen.findByText('Reset required')).toBeInTheDocument();
    expect(screen.getByText('Locked')).toBeInTheDocument();
    expect(screen.queryByText(/property.s cashier PIN store/)).not.toBeInTheDocument();
    await user.type(screen.getByLabelText(/current password/i), 'MySecretPass1!');
    await user.type(screen.getByLabelText(/new approval pin/i), '4826');
    await user.type(screen.getByLabelText(/confirm approval pin/i), '4826');
    await user.click(screen.getByRole('button', { name: /save approval pin/i }));

    expect(save).toHaveBeenCalledWith('ury.ury_pos.override.set_my_pin', {
      password: 'MySecretPass1!',
      pin_secret: '4826',
    });
    expect(await screen.findByText('Approval PIN set')).toBeInTheDocument();
    expect(screen.queryByText('Reset required')).not.toBeInTheDocument();
    expect(screen.queryByText('Locked')).not.toBeInTheDocument();
  });

  it('shows external staff flags and updates the row after a manager clears its PIN', async () => {
    vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
      if (method === 'ury.ury_pos.override.my_pin_status') {
        return { message: { has_pin: true, external_verifier: true, can_manage_here: true } };
      }
      return { message: [{ user: 'staff@example.com', full_name: 'Staff Member', has_pin: true, locked: true }] };
    });
    const clear = vi.spyOn(call, 'post').mockResolvedValue({
      message: { ok: true, has_pin: false, must_reset: true },
    });

    const user = userEvent.setup();
    render(<ApprovalPinDialog open onOpenChange={vi.fn()} posProfile="Main Till" canManagePins />);

    await user.click(await screen.findByRole('button', { name: 'Staff PINs' }));
    expect(await screen.findByText('Locked')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /clear pin/i }));

    expect(clear).toHaveBeenCalledWith('ury.ury_pos.override.clear_user_pin', {
      user: 'staff@example.com',
      pos_profile: 'Main Till',
    });
    expect(await screen.findByText('Reset required')).toBeInTheDocument();
    expect(screen.queryByText('Locked')).not.toBeInTheDocument();
  });
});
