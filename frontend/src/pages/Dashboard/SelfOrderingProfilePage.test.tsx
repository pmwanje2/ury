import { describe, expect, it, vi, beforeEach } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { call } from '@ury/core';
import SelfOrderingProfilePage from './SelfOrderingProfilePage';
import { dashboardService } from '../../services/dashboard';

vi.mock('../../context/BranchContext', () => ({
  useBranchContext: () => ({
    activeBranchId: 'Kozhikode',
  }),
}));

vi.mock('../../services/dashboard', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/dashboard')>();
  return {
    ...actual,
    dashboardService: {
      getModuleRecords: vi.fn(),
    },
  };
});

vi.mock('@ury/core', async (importOriginal) => {
  const actual = await importOriginal<any>();
  return {
    ...actual,
    call: vi.fn(),
  };
});

vi.mock('@ury/ui', async (importOriginal) => {
  const actual = await importOriginal<any>();
  return {
    ...actual,
    showToast: {
      error: vi.fn(),
      success: vi.fn(),
      warning: vi.fn(),
    },
  };
});

const profiles = [
  {
    name: 'SOP-0001',
    profile_name: 'Main Branch QR',
    restaurant: 'URY Main',
    branch: 'Kozhikode',
    pos_profile: 'POS-001',
    enabled: 1,
  },
];

describe('SelfOrderingProfilePage', () => {
  beforeEach(() => {
    cleanup();
    vi.mocked(dashboardService.getModuleRecords).mockReset();
  });

  it('shows empty state when no profiles exist', async () => {
    vi.mocked(dashboardService.getModuleRecords).mockResolvedValue([]);

    render(<SelfOrderingProfilePage />);
    expect(await screen.findByText('No profiles configured yet')).toBeInTheDocument();
  });

  it('renders a table of profiles', async () => {
    vi.mocked(dashboardService.getModuleRecords).mockImplementation(async (doctype) => {
      if (doctype === 'URY Self Ordering Profile') {
        return profiles;
      }
      return [];
    });

    render(<SelfOrderingProfilePage />);
    expect(await screen.findByText('Main Branch QR')).toBeInTheDocument();
  });

  it('displays profile branch information', async () => {
    vi.mocked(dashboardService.getModuleRecords).mockImplementation(async (doctype) => {
      if (doctype === 'URY Self Ordering Profile') {
        return profiles;
      }
      return [];
    });

    render(<SelfOrderingProfilePage />);
    expect(await screen.findByText('Kozhikode')).toBeInTheDocument();
  });

  it('shows profile status badge', async () => {
    vi.mocked(dashboardService.getModuleRecords).mockImplementation(async (doctype) => {
      if (doctype === 'URY Self Ordering Profile') {
        return profiles;
      }
      return [];
    });

    render(<SelfOrderingProfilePage />);
    expect(await screen.findByText('Enabled')).toBeInTheDocument();
  });
  it.each(['Courtyard', ''])('edits or clears the optional guest name (%s) without renaming the profile', async (value) => {
    vi.mocked(dashboardService.getModuleRecords).mockImplementation(async (doctype) =>
      doctype === 'URY Self Ordering Profile' ? [{ ...profiles[0], guest_display_name: 'Garden', session_idle_timeout_minutes: 30 }] : []);
    render(<SelfOrderingProfilePage />);
    fireEvent.click((await screen.findByText('Main Branch QR')).closest('tr')!.querySelector('button')!);
    const input = screen.getByRole('textbox', { name: 'Guest Display Name' });
    expect(input).toHaveValue('Garden');
    fireEvent.change(input, { target: { value } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Profile' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('frappe.client.set_value', {
      doctype: 'URY Self Ordering Profile', name: 'SOP-0001',
      fieldname: expect.objectContaining({ guest_display_name: value, profile_name: 'Main Branch QR' }),
    }));
    expect(await screen.findByText('Main Branch QR')).toBeInTheDocument();
  });

  it('creates a profile with a public guest name through the native insert contract', async () => {
    vi.mocked(dashboardService.getModuleRecords).mockImplementation(async (doctype) =>
      doctype === 'URY Self Ordering Profile' ? [] : [{ name: doctype + ' A' }]);
    render(<SelfOrderingProfilePage />);
    fireEvent.click(await screen.findByRole('button', { name: 'New Profile' }));
    fireEvent.change(screen.getByPlaceholderText('e.g., Main Branch QR'), { target: { value: 'Staff QR' } });
    fireEvent.change(screen.getByLabelText('Guest Display Name'), { target: { value: 'Courtyard' } });
    for (const [id, label] of [['restaurant', 'URY Restaurant A'], ['branch', 'Branch A'], ['pos_profile', 'POS Profile A']]) {
      fireEvent.focus(document.getElementById(id)!);
      fireEvent.click(await screen.findByText(label));
    }
    fireEvent.click(screen.getByRole('button', { name: 'Save Profile' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('frappe.client.insert', {
      doc: expect.objectContaining({ doctype: 'URY Self Ordering Profile', name: 'Staff QR', profile_name: 'Staff QR', guest_display_name: 'Courtyard' }),
    }));
    expect(await screen.findByText('No profiles configured yet')).toBeInTheDocument();
  });
});
