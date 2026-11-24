import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { initI18n } from '../i18n';
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

    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('Approve sent item void')).toBeInTheDocument();
    expect(screen.getByText('Void Grilled Fish (2 -> 1)')).toBeInTheDocument();
    expect(screen.getByText('Amount: $25.00')).toBeInTheDocument();
    expect(screen.getByText('Requested by Grace K.')).toBeInTheDocument();

    const select = await screen.findByLabelText(/manager/i);
    await user.selectOptions(select, 'mgr.a@example.com');

    // Touch keypad keys have >= 48px height classes (min-h-12 or h-14)
    const key4 = screen.getByRole('button', { name: '4' });
    expect(key4.className).toMatch(/min-h-12|h-12|h-14/);

    await user.click(key4);
    await user.click(screen.getByRole('button', { name: '8' }));
    await user.click(screen.getByRole('button', { name: '2' }));
    await user.click(screen.getByRole('button', { name: '6' }));

    // PIN must be masked (4 bullets) and raw PIN digits never leaked in text
    const display = screen.getByTestId('manager-pin-display');
    expect(display.textContent).toBe('••••');
    expect(screen.getByRole('dialog').textContent).not.toContain('4826');

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
        pin: '4826',
        reference_doctype: 'POS Invoice',
        reference_name: 'POS-INV-001',
      }),
    );
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('supports hardware keyboard digits, Backspace, Enter, and Escape', async () => {
    mockServer({ approvers: [APPROVERS[0]], requiresReason: false });
    const onApproved = vi.fn();
    const onCancel = vi.fn();

    render(
      <ManagerApproval
        open
        posProfile="Main Till"
        privilege="void_sent_item"
        summaryLines={['Void item']}
        reference={{ doctype: 'POS Invoice', name: 'POS-INV-002' }}
        claim={{ amount: 10 }}
        onApproved={onApproved}
        onCancel={onCancel}
      />,
    );

    // Single approver auto-selected
    await waitFor(() => {
      expect((screen.getByLabelText(/manager/i) as HTMLSelectElement).value).toBe('mgr.a@example.com');
    });

    fireEvent.keyDown(window, { key: '1' });
    fireEvent.keyDown(window, { key: '2' });
    fireEvent.keyDown(window, { key: '3' });
    fireEvent.keyDown(window, { key: '9' });
    fireEvent.keyDown(window, { key: 'Backspace' });
    fireEvent.keyDown(window, { key: '4' });
    expect(screen.getByTestId('manager-pin-display').textContent).toBe('••••');

    fireEvent.keyDown(window, { key: 'Enter' });

    await waitFor(() => {
      expect(onApproved).toHaveBeenCalledWith(expect.objectContaining({ token: 'tok-signed-123' }));
    });
    expect(call.post).toHaveBeenCalledWith(
      'ury.ury_pos.override.authorise',
      expect.objectContaining({ pin: '1234' }),
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

    // Disabled until reason is picked
    expect(screen.getByRole('button', { name: /approve/i })).toBeDisabled();

    // Pick OTHER (requires_note=1)
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
      expect((screen.getByLabelText(/manager/i) as HTMLSelectElement).value).toBe('mgr.a@example.com');
    });

    for (const d of ['9', '9', '9', '9']) await user.click(screen.getByRole('button', { name: d }));
    await user.click(screen.getByRole('button', { name: /approve/i }));

    const wrongAlert = await screen.findByRole('alert');
    expect(wrongAlert.textContent).toMatch(/PIN/i);
    expect(wrongAlert.textContent).not.toMatch(/URY|HUF|Kamra/i);
    // PIN cleared automatically after wrong PIN
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

  it.each(['en', 'fr', 'es', 'ar'] as const)('renders cleanly in locale %s without vendor names', async (lang) => {
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

    await screen.findByLabelText(/.+/);
    const text = screen.getByRole('dialog').textContent || '';
    expect(text).not.toMatch(/URY|HUF|Kamra|manager_approval\./i);
  });
});
