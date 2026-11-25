import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TimeclockButton from './TimeclockButton';

const getStatusMock = vi.fn();
const punchMock = vi.fn();

vi.mock('../lib/timeclock-api', () => ({
  getMyTimeclockStatus: (...args: unknown[]) => getStatusMock(...args),
  punchTimeclock: (...args: unknown[]) => punchMock(...args),
}));

vi.mock('../i18n', () => ({
  t: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}:${JSON.stringify(params)}` : key,
}));

vi.mock('@ury/ui', async () => {
  const actual = await vi.importActual('@ury/ui');
  return {
    ...actual,
    showToast: {
      success: vi.fn(),
      error: vi.fn(),
    },
  };
});

const VENDOR_RE = /\b(?:URY|Frappe|ERPNext|Kamra|HUF|Tridz|HeyKoala|BuildSuite)\b/i;

describe('TimeclockButton (G36)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('hides completely when HR attendance module is unavailable', async () => {
    getStatusMock.mockResolvedValue({ available: false });
    const { container } = render(<TimeclockButton posProfile="Main Grill POS" />);
    await waitFor(() => expect(getStatusMock).toHaveBeenCalledWith('Main Grill POS'));
    expect(container.firstChild).toBeNull();
  });

  it('clocks in via confirmation dialog and transitions to clocked-in pill', async () => {
    const user = userEvent.setup();
    getStatusMock.mockResolvedValue({
      available: true,
      has_employee: true,
      employee: 'HR-EMP-0001',
      employee_name: 'Grace Namono',
      is_clocked_in: false,
      last_log_type: null,
      last_punch_time: null,
      clocked_in_since: null,
      elapsed_minutes: 0,
      shift: null,
      open_checks_count: 0,
      open_checks: [],
      has_open_shift: false,
      open_shift: null,
      block_clock_out_with_open_work: true,
      require_clock_in_to_order: true,
    });
    punchMock.mockResolvedValue({
      available: true,
      has_employee: true,
      employee: 'HR-EMP-0001',
      employee_name: 'Grace Namono',
      is_clocked_in: true,
      last_log_type: 'IN',
      last_punch_time: '2026-11-24 08:04:00',
      clocked_in_since: '2026-11-24 08:04:00',
      elapsed_minutes: 0,
      shift: 'Morning Shift',
      open_checks_count: 0,
      open_checks: [],
      has_open_shift: false,
      open_shift: null,
      block_clock_out_with_open_work: true,
      require_clock_in_to_order: true,
    });

    render(<TimeclockButton posProfile="Main Grill POS" />);

    const clockInTrigger = await screen.findByRole('button', { name: /timeclock\.clock_in/i });
    expect(clockInTrigger).toBeInTheDocument();
    expect(document.body.textContent || '').not.toMatch(VENDOR_RE);

    await user.click(clockInTrigger);
    expect(screen.getByText('Grace Namono')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'timeclock.confirm_clock_in' }));
    await waitFor(() => expect(punchMock).toHaveBeenCalledWith('IN', 'Main Grill POS'));

    expect(await screen.findByRole('button', { name: /timeclock\.clock_out_pill/i })).toBeInTheDocument();
  });

  it('shows open-work warning when clocked in with open checks/shift and displays server refusal cleanly', async () => {
    const user = userEvent.setup();
    getStatusMock.mockResolvedValue({
      available: true,
      has_employee: true,
      employee: 'HR-EMP-0001',
      employee_name: 'Grace Namono',
      is_clocked_in: true,
      last_log_type: 'IN',
      last_punch_time: '2026-11-24 08:04:00',
      clocked_in_since: '2026-11-24 08:04:00',
      elapsed_minutes: 135,
      shift: 'Morning Shift',
      open_checks_count: 2,
      open_checks: ['POS-INV-001', 'POS-INV-002'],
      has_open_shift: true,
      open_shift: 'POS-OPE-001',
      block_clock_out_with_open_work: true,
      require_clock_in_to_order: false,
    });
    punchMock.mockRejectedValue(new Error('[POS-TIMECARD-OPEN-WORK] Close open checks before clocking out.'));

    render(<TimeclockButton posProfile="Main Grill POS" />);

    const trigger = await screen.findByRole('button', { name: /timeclock\.clock_out_pill/i });
    await user.click(trigger);

    expect(screen.getByText(/timeclock\.open_work_warning/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'timeclock.confirm_clock_out' }));

    expect(await screen.findByText(/POS-TIMECARD-OPEN-WORK/i)).toBeInTheDocument();
    expect(document.body.textContent || '').not.toMatch(VENDOR_RE);
  });
});
