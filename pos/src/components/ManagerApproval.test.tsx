import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { initI18n, t } from '../i18n';
import { overrideErrorMessage } from '../lib/override-api';
import ManagerApproval from './ManagerApproval';

const APPROVERS = [
  { user: 'mgr.a@example.com', full_name: 'Alice Manager' },
  { user: 'mgr.b@example.com', full_name: 'Bob Supervisor' },
];

const REASONS = [
  { code: 'VOID-GUEST', description: 'Guest changed mind', requires_note: 0, applies_to: ['void_sent_item'] },
  { code: 'OTHER', description: 'Other', requires_note: 1, applies_to: [] },
];

function mockServer(overrides?: {
  approvers?: typeof APPROVERS;
  requiresReason?: boolean;
  reasons?: typeof REASONS;
  authoriseImpl?: (method: string, payload: Record<string, unknown>) => Promise<unknown>;
}) {
  const approvers = overrides?.approvers ?? APPROVERS;
  const requiresReason = overrides?.requiresReason ?? false;
  const reasons = overrides?.reasons ?? REASONS;

  vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
    if (method === 'ury.ury_pos.override.list_approvers') return { message: approvers };
    if (method === 'ury.ury_pos.override.privilege_policy') {
      return { message: { privilege: 'void_sent_item', label: 'Void Sent Item', may_perform: false, requires_reason: requiresReason } };
    }
    if (method === 'ury.ury_pos.override.list_reasons') return { message: reasons };
    return { message: null };
  });

  vi.spyOn(call, 'post').mockImplementation(async (method: string, payload?: unknown) => {
    if (overrides?.authoriseImpl) {
      return overrides.authoriseImpl(method, (payload || {}) as Record<string, unknown>);
    }
    if (method === 'ury.ury_pos.override.authorise') {
      return {
        message: {
          token: 'tok-signed-123',
          approver: 'mgr.a@example.com',
          approver_name: 'Alice Manager',
          expires_in: 120,
        },
      };
    }
    return { message: null };
  });
}

describe('ManagerApproval (B2a unified Simphony/SAP prompt)', () => {
  beforeEach(async () => {
    await initI18n('en');
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('localizes unrecognized provider errors without showing provider details', () => {
    const error = new Error('Third-party provider traceback and internal storage error');
    expect(overrideErrorMessage(error, 'approval_pin.save_failed')).toBe(t('approval_pin.save_failed'));
  });

  it.each([
    ['POS-AUTH-REFRESH', 'Actualisez la caisse et réessayez.'],
    ['POS-AUTH-PROVIDER-UNKNOWN', undefined],
  ])('displays the localized approval failure for HTTP 417 %s', async (code, expected) => {
    await initI18n('fr');
    mockServer({
      approvers: [APPROVERS[0]],
      authoriseImpl: async () => {
        throw {
          httpStatus: 417,
          _server_messages: JSON.stringify([JSON.stringify({ message: `Server response [${code}]` })]),
        };
      },
    });
    const user = userEvent.setup();
    const onApproved = vi.fn();
    render(
      <ManagerApproval
        open
        posProfile="Main Till"
        privilege="void_sent_item"
        reference="POS-INV-001"
        summaryLines={[]}
        claim={{}}
        onApproved={onApproved}
        onCancel={vi.fn()}
      />,
    );

    await screen.findByRole('combobox', { name: t('manager_approval.manager') });
    for (const digit of '4826') {
      await user.click(screen.getByRole('button', { name: digit }));
    }
    await user.click(screen.getByRole('button', { name: t('manager_approval.approve') }));

    expect((await screen.findByRole('alert')).textContent).toBe(expected ?? t('manager_approval.error_generic'));
    expect(onApproved).not.toHaveBeenCalled();
  });

  it('gives the dismiss button a 44px touch target', async () => {
    mockServer();
    render(
      <ManagerApproval
        open
        posProfile="Main Till"
        privilege="void_sent_item"
        reference="POS-INV-001"
        summaryLines={[]}
        claim={{}}
        onApproved={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    await screen.findByRole('combobox', { name: /manager/i });
    const dismiss = screen.getByRole('button', { name: t('common.dismiss') });
    expect([...dismiss.classList]).toEqual(expect.arrayContaining(['h-11', 'w-11']));
  });

  it('renders action summary, approver picker, masked PIN pad with >=48px keys, and calls onApproved', async () => {
    mockServer({ requiresReason: false });
    const onApproved = vi.fn();
    const onCancel = vi.fn();
    const user = userEvent.setup();

    render(
      <ManagerApproval
        open
        posProfile="Main Till"
        privilege="void_sent_item"
        title="Approve sent item void"
        summaryLines={['Void Grilled Fish (2 -> 1)', 'Amount: $25.00', 'Requested by Grace K.']}
        reference="POS-INV-001"
        claim={{ action: 'reduce_order_item_qty', item_row_name: 'ROW-1', from_qty: 2, to_qty: 1, amount: 25 }}
        amount={25}
        onApproved={onApproved}
        onCancel={onCancel}
      />,
    );

    const dialog = screen.getByRole('dialog');
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveAttribute('aria-labelledby');
    const headingId = dialog.getAttribute('aria-labelledby')!;
    expect(document.getElementById(headingId)?.textContent).toBe('Approve sent item void');

    expect(screen.getByText('Void Grilled Fish (2 -> 1)')).toBeInTheDocument();
    expect(screen.getByText('Amount: $25.00')).toBeInTheDocument();
    expect(screen.getByText('Requested by Grace K.')).toBeInTheDocument();

    const select = await screen.findByRole('combobox', { name: /manager/i });
    const aliceChip = screen.getByRole('button', { name: 'Alice Manager' });
    expect(aliceChip).toHaveAttribute('aria-pressed', 'false');
    await user.selectOptions(select, 'mgr.a@example.com');
    expect(aliceChip).toHaveAttribute('aria-pressed', 'true');

    // Touch keypad keys have >= 48px height classes (min-h-12 or h-12 or h-14)
    const key4 = screen.getByRole('button', { name: '4' });
    expect(key4.className).toMatch(/min-h-12|h-12|h-14/);

    await user.click(key4);
    await user.click(screen.getByRole('button', { name: '8' }));
    await user.click(screen.getByRole('button', { name: '2' }));
    await user.click(screen.getByRole('button', { name: '6' }));

    // PIN must be masked (4 bullets), accessible live region announces count, and raw PIN digits never leaked
    const display = screen.getByTestId('manager-pin-display');
    expect(display).toHaveAttribute('role', 'status');
    expect(display).toHaveAttribute('aria-live', 'polite');
    expect(display).toHaveAttribute('aria-label', expect.stringMatching(/4/));
    expect(display.textContent).toContain('••••');
    expect(dialog.textContent).not.toContain('4826');

    await user.click(screen.getByRole('button', { name: /approve/i }));

    await waitFor(() => {
      expect(onApproved).toHaveBeenCalledWith(
        expect.objectContaining({ token: 'tok-signed-123', approverName: 'Alice Manager' }),
      );
    });
    expect(call.post).toHaveBeenCalledWith(
      'ury.ury_pos.override.authorise',
      expect.objectContaining({
        pos_profile: 'Main Till',
        privilege: 'void_sent_item',
        approver: 'mgr.a@example.com',
        pin_secret: '4826',
        reference_doctype: 'POS Invoice',
        reference_name: 'POS-INV-001',
      }),
    );
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('supports hardware keyboard digits, Backspace, Enter, Escape, and traps Tab focus inside the dialog', async () => {
    mockServer({ approvers: [APPROVERS[0]], requiresReason: false });
    const onApproved = vi.fn();
    const onCancel = vi.fn();

    render(
      <>
        <button type="button">Outside Background Button</button>
        <ManagerApproval
          open
          posProfile="Main Till"
          privilege="void_sent_item"
          summaryLines={['Void item']}
          reference={{ doctype: 'POS Invoice', name: 'POS-INV-002' }}
          claim={{ amount: 10 }}
          onApproved={onApproved}
          onCancel={onCancel}
        />
      </>,
    );

    // Single approver auto-selected
    await waitFor(() => {
      expect((screen.getByRole('combobox', { name: /manager/i }) as HTMLSelectElement).value).toBe('mgr.a@example.com');
    });

    // Focus trap: Shift+Tab on first focusable element wraps to last focusable inside dialog
    const dialog = screen.getByRole('dialog');
    const focusables = Array.from(
      dialog.querySelectorAll<HTMLElement>('button:not([disabled]), select:not([disabled]), textarea:not([disabled]), input:not([disabled])'),
    );
    expect(focusables.length).toBeGreaterThan(2);
    focusables[0].focus();
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(focusables[focusables.length - 1]);
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: false });
    expect(document.activeElement).toBe(focusables[0]);

    fireEvent.keyDown(window, { key: '1' });
    fireEvent.keyDown(window, { key: '2' });
    fireEvent.keyDown(window, { key: '3' });
    fireEvent.keyDown(window, { key: '9' });
    fireEvent.keyDown(window, { key: 'Backspace' });
    fireEvent.keyDown(window, { key: '4' });
    expect(screen.getByTestId('manager-pin-display').textContent).toContain('••••');

    fireEvent.keyDown(window, { key: 'Enter' });

    await waitFor(() => {
      expect(onApproved).toHaveBeenCalledWith(expect.objectContaining({ token: 'tok-signed-123' }));
    });
    expect(call.post).toHaveBeenCalledWith(
      'ury.ury_pos.override.authorise',
      expect.objectContaining({ pin_secret: '1234' }),
    );
  });

  it('requires a POS Reason and note (when requires_note=1) if policy requires_reason is true', async () => {
    mockServer({ approvers: [APPROVERS[0]], requiresReason: true });
    const onApproved = vi.fn();
    const user = userEvent.setup();

    render(
      <ManagerApproval
        open
        posProfile="Main Till"
        privilege="void_sent_item"
        summaryLines={['Void Grilled Fish']}
        reference="POS-INV-003"
        claim={{ amount: 25, reason: '' }}
        onApproved={onApproved}
        onCancel={vi.fn()}
      />,
    );

    const reasonSelect = await screen.findByLabelText(/reason/i);
    await user.click(screen.getByRole('button', { name: '1' }));
    await user.click(screen.getByRole('button', { name: '3' }));
    await user.click(screen.getByRole('button', { name: '5' }));
    await user.click(screen.getByRole('button', { name: '7' }));

    expect(screen.getByRole('button', { name: /approve/i })).toBeDisabled();

    await user.selectOptions(reasonSelect, 'OTHER');
    const noteBox = await screen.findByLabelText(/note/i);
    expect(screen.getByRole('button', { name: /approve/i })).toBeDisabled();

    await user.type(noteBox, 'Dropped plate in kitchen');
    expect(screen.getByRole('button', { name: /approve/i })).toBeEnabled();

    await user.click(screen.getByRole('button', { name: /approve/i }));
    await waitFor(() => {
      expect(onApproved).toHaveBeenCalledWith(
        expect.objectContaining({
          token: 'tok-signed-123',
          reason: 'Other: Dropped plate in kitchen',
        }),
      );
    });
    expect(call.post).toHaveBeenCalledWith(
      'ury.ury_pos.override.authorise',
      expect.objectContaining({
        claim: expect.objectContaining({ reason: 'Other: Dropped plate in kitchen' }),
      }),
    );
  });

  it('shows empty-approvers banner when no manager has an approval PIN', async () => {
    mockServer({ approvers: [] });

    render(
      <ManagerApproval
        open
        posProfile="Main Till"
        privilege="void_sent_item"
        summaryLines={['Void item']}
        reference="POS-INV-004"
        claim={{ amount: 15 }}
        onApproved={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/no manager/i);
    expect(screen.getByRole('button', { name: /approve/i })).toBeDisabled();
  });

  it('clears PIN buffer and shows neutral localized messages for POS-AUTH-PIN and POS-AUTH-LOCKED', async () => {
    const user = userEvent.setup();
    let attempt = 0;
    mockServer({
      approvers: [APPROVERS[0]],
      authoriseImpl: async () => {
        attempt += 1;
        if (attempt === 1) {
          throw new Error('That approval PIN did not match. [POS-AUTH-PIN]');
        }
        throw new Error('Approval PIN is locked after too many wrong attempts. [POS-AUTH-LOCKED]');
      },
    });

    render(
      <ManagerApproval
        open
        posProfile="Main Till"
        privilege="void_sent_item"
        summaryLines={['Void item']}
        reference="POS-INV-005"
        claim={{ amount: 15 }}
        onApproved={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect((screen.getByRole('combobox', { name: /manager/i }) as HTMLSelectElement).value).toBe('mgr.a@example.com');
    });

    for (const d of ['9', '9', '9', '9']) await user.click(screen.getByRole('button', { name: d }));
    await user.click(screen.getByRole('button', { name: /approve/i }));

    const wrongAlert = await screen.findByRole('alert');
    expect(wrongAlert.textContent).toMatch(/PIN/i);
    expect(wrongAlert.textContent).not.toMatch(/URY|HUF|Kamra/i);
    expect(screen.getByTestId('manager-pin-display').textContent).toBe('');

    for (const d of ['9', '9', '9', '9']) await user.click(screen.getByRole('button', { name: d }));
    await user.click(screen.getByRole('button', { name: /approve/i }));

    const lockedAlert = await screen.findByRole('alert');
    expect(lockedAlert.textContent).toMatch(/locked/i);
    expect(screen.getByTestId('manager-pin-display').textContent).toBe('');
  });

  it('auto-cancels after 60s idle timeout without altering cashier session', async () => {
    vi.useFakeTimers();
    mockServer({ approvers: [APPROVERS[0]] });
    const onCancel = vi.fn();

    render(
      <ManagerApproval
        open
        posProfile="Main Till"
        privilege="void_sent_item"
        summaryLines={['Void item']}
        reference="POS-INV-006"
        claim={{ amount: 15 }}
        onApproved={vi.fn()}
        onCancel={onCancel}
      />,
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_500);
    });

    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('uses compact landscape two-column layout at lg so all 4 keypad rows fit at 1280x720 without scrolling', async () => {
    mockServer({ approvers: APPROVERS, requiresReason: true });

    render(
      <ManagerApproval
        open
        posProfile="Main Till"
        privilege="void_sent_item"
        summaryLines={['Void Grilled Fish (2 -> 1)', 'Amount: $25.00']}
        reference="POS-INV-008"
        claim={{ amount: 25 }}
        onApproved={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    await screen.findByRole('combobox', { name: /manager/i });
    const dialog = screen.getByRole('dialog');
    expect(dialog.innerHTML).toMatch(/lg:max-w-2xl/);
    expect(dialog.innerHTML).toMatch(/lg:grid-cols-2/);
  });

  it.each(['en', 'fr', 'es', 'ar'] as const)('renders cleanly in locale %s without vendor names or English dismiss fallback', async (lang) => {
    await initI18n(lang);
    mockServer({ approvers: [APPROVERS[0]] });

    render(
      <ManagerApproval
        open
        posProfile="Main Till"
        privilege="void_sent_item"
        summaryLines={['Void Grilled Fish']}
        reference="POS-INV-007"
        claim={{ amount: 25 }}
        onApproved={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    await screen.findByRole('combobox');
    const text = screen.getByRole('dialog').textContent || '';
    expect(text).not.toMatch(/URY|HUF|Kamra|manager_approval\./i);
    if (lang !== 'en') {
      expect(text).not.toContain('Dismiss');
    }
  });
});
