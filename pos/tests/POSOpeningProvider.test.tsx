import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initI18n } from '../src/i18n';
import { checkPOSOpening, createPOSOpening, getPOSOpeningContext, validatePOSClose } from '../src/lib/pos-opening-api';
import { getChecklist, submitChecklist } from '../src/lib/checklist-api';
import POSOpeningProvider from '../src/components/POSOpeningProvider';
import { clearAppStorage } from '../../packages/core/src/storage';
import { realSdkHttpFailure, realSdkNetworkFailure } from './helpers/sdk-errors';

const state = vi.hoisted(() => ({
  profile: {
    name: 'POS-1', company: 'Test Co', custom_daily_pos_close: 1,
    role_allowed_for_billing: [{ role: 'URY Cashier' }] as Array<{ role: string }>,
  },
  user: { name: 'operator@example.com', roles: ['URY Cashier'] as string[] },
}));
vi.mock('../src/store/root-store', () => ({ useRootStore: () => ({ user: state.user }) }));
vi.mock('../src/store/pos-store', () => ({ usePOSStore: () => ({
  posProfile: state.profile, showVoluntaryClosing: false,
}) }));
vi.mock('../src/lib/pos-opening-api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../src/lib/pos-opening-api')>(),
  checkPOSOpening: vi.fn(), getPOSOpeningContext: vi.fn(),
  createPOSOpening: vi.fn(), validatePOSClose: vi.fn(),
}));
vi.mock('../src/lib/checklist-api', () => ({ getChecklist: vi.fn(), submitChecklist: vi.fn() }));

let container: HTMLDivElement;
let root: Root;
const opening = {
  name: 'OPEN-1', company: 'Test Co', pos_profile: 'POS-1',
  period_start_date: '2026-10-11 08:00:00',
};
const context = {
  company: 'Test Co', allowed_profiles: [{ name: 'POS-1' }], selected_profile: 'POS-1',
  payment_modes: [{ mode_of_payment: 'Cash', opening_amount: 0 }],
  user: 'operator@example.com', user_full_name: 'Current Operator',
  branch: 'Branch A', restaurant: 'Rest A', daily_close_pending: false,
  multi_cashier: { enabled: false, main_cashier_open: false, main_cashier_configured: false },
  permissions: { create: true, submit: true },
  occupied_entry: { name: 'OPEN-1', user_full_name: 'Till Opener',
    period_start_date: '2026-10-11 08:00:00', joinable: true },
};

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.clearAllMocks();
  await initI18n('en');
  vi.mocked(checkPOSOpening).mockResolvedValue({ message: 1 });
  vi.mocked(getPOSOpeningContext).mockResolvedValue(context);
  vi.mocked(validatePOSClose).mockResolvedValue({ message: 'Success' });
  vi.mocked(getChecklist).mockResolvedValue({ items: [], logName: null, logStatus: 'Complete' });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  state.user = { name: 'operator@example.com', roles: ['URY Cashier'] };
  state.profile.role_allowed_for_billing = [{ role: 'URY Cashier' }];
  window.history.replaceState({}, '', '/');
});

async function render() {
  await act(async () => root.render(<POSOpeningProvider><main>POS workspace</main></POSOpeningProvider>));
}

function continueButton() {
  const button = Array.from(container.querySelectorAll('button')).find((button) => /^Continue/.test(button.textContent || ''));
  expect(button, 'Eligible operator must have a Continue button').toBeDefined();
  return button!;
}

describe('shared till continuation', () => {
  it('resumes the existing opening through the same checklist and daily-close path without creating one', async () => {
    await render();
    expect(container.textContent).toContain('Till opened by Till Opener since');
    expect(container.querySelector('input')).toBeNull();
    expect(container.textContent).not.toContain('Open POS Session');
    await act(async () => continueButton().click());
    expect(container.textContent).toContain('POS workspace');
    expect(getChecklist).toHaveBeenCalledWith('POS-1', 'Opening');
    expect(validatePOSClose).toHaveBeenCalledWith('POS-1');
    expect(createPOSOpening).not.toHaveBeenCalled();
    expect(checkPOSOpening).toHaveBeenCalledWith('operator@example.com');
  });

  it('does not offer Continue or an opening form on a non-joinable occupied till', async () => {
    vi.mocked(getPOSOpeningContext).mockResolvedValue({ ...context,
      occupied_entry: { ...context.occupied_entry, joinable: false },
    });
    await render();
    expect(container.textContent).toContain('must close the shift');
    expect(container.querySelector('input')).toBeNull();
    expect(container.textContent).not.toMatch(/Continue|Open POS Session|POS workspace/);
    expect(createPOSOpening).not.toHaveBeenCalled();
  });

  it('can continue without permission to create or submit a new opening', async () => {
    vi.mocked(getPOSOpeningContext).mockResolvedValue({ ...context, permissions: { create: false, submit: false } });
    await render();
    await act(async () => continueButton().click());
    expect(container.textContent).toContain('POS workspace');
    expect(createPOSOpening).not.toHaveBeenCalled();
  });

  it('keeps the daily-close gate on continuation', async () => {
    await render();
    vi.mocked(validatePOSClose).mockResolvedValue({ message: 'Failed' });
    await act(async () => continueButton().click());
    expect(container.textContent).not.toContain('POS workspace');
    expect(container.textContent).toContain("Yesterday's session was never closed");
  });

  it('keeps the opening checklist gate and resumes the same till when completed', async () => {
    vi.mocked(getChecklist).mockResolvedValue({ items: [{ item_label: 'Count float', is_mandatory: true }], logName: 'LOG-1', logStatus: 'Pending' });
    await render();
    await act(async () => continueButton().click());
    expect(container.textContent).not.toContain('POS workspace');
    expect(container.textContent).toContain('Count float');
    vi.mocked(getChecklist).mockResolvedValue({ items: [], logName: 'LOG-1', logStatus: 'Complete' });
    vi.mocked(submitChecklist).mockResolvedValue({ status: 'Complete', name: 'LOG-1' });
    await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
    await act(async () => container.querySelector<HTMLButtonElement>('button')!.click());
    expect(container.textContent).toContain('POS workspace');
    expect(createPOSOpening).not.toHaveBeenCalled();
  });

  it('does not let a shared entry bypass the existing company/profile guard', async () => {
    vi.mocked(getPOSOpeningContext).mockResolvedValue({ ...context, company: 'Other Co' });
    await render();
    await act(async () => continueButton().click());
    expect(container.textContent).not.toContain('POS workspace');
    expect(getChecklist).not.toHaveBeenCalled();
  });

  it('explains a pending daily close through the existing gate instead of silently hiding Continue', async () => {
    vi.mocked(getPOSOpeningContext).mockResolvedValue({ ...context, daily_close_pending: true });
    await render();
    vi.mocked(validatePOSClose).mockResolvedValue({ message: 'Failed' });
    await act(async () => continueButton().click());
    expect(container.textContent).not.toContain('POS workspace');
    expect(container.textContent).toContain("Yesterday's session was never closed");
    expect(createPOSOpening).not.toHaveBeenCalled();
  });

  it('keeps own-opening resume working without a Continue screen', async () => {
    vi.mocked(checkPOSOpening).mockResolvedValue({ message: [opening] });
    await render();
    expect(container.textContent).toContain('POS workspace');
    expect(getPOSOpeningContext).not.toHaveBeenCalled();
    expect(getChecklist).toHaveBeenCalledWith('POS-1', 'Opening');
  });
});

describe('offline opening fallback', () => {
  async function confirmOnline() {
    vi.mocked(checkPOSOpening).mockResolvedValue({ message: [opening] });
    await render();
    expect(container.textContent).toContain('POS workspace');
    await act(async () => root.unmount());
    root = createRoot(container);
  }

  afterEach(() => {
    delete (state.profile as Record<string, unknown>).ury_offline_capture;
    sessionStorage.clear();
  });

  it('an opted-in till reloading offline keeps the opening it confirmed online', async () => {
    (state.profile as Record<string, unknown>).ury_offline_capture = 1;
    await confirmOnline();
    vi.mocked(checkPOSOpening).mockRejectedValue(await realSdkNetworkFailure());
    await render();
    expect(container.textContent).toContain('POS workspace');
  });

  it('a till without the flag shows the blocking error on a network failure', async () => {
    await confirmOnline();
    vi.mocked(checkPOSOpening).mockRejectedValue(await realSdkNetworkFailure());
    await render();
    expect(container.textContent).not.toContain('POS workspace');
    expect(container.textContent).toContain('Could not open the POS session');
  });

  it('an opted-in till with no confirmed opening is not admitted offline', async () => {
    (state.profile as Record<string, unknown>).ury_offline_capture = 1;
    vi.mocked(checkPOSOpening).mockRejectedValue(await realSdkNetworkFailure());
    await render();
    expect(container.textContent).not.toContain('POS workspace');
  });

  it('a real 403 on the opening check is never treated as offline', async () => {
    (state.profile as Record<string, unknown>).ury_offline_capture = 1;
    await confirmOnline();
    vi.mocked(checkPOSOpening).mockRejectedValue(await realSdkHttpFailure(403));
    await render();
    expect(container.textContent).not.toContain('POS workspace');
  });


  it.each([502, 503, 504, 520, 524])('HTTP %i on the opening check keeps the confirmed opening', async (code) => {
    (state.profile as Record<string, unknown>).ury_offline_capture = 1;
    await confirmOnline();
    vi.mocked(checkPOSOpening).mockRejectedValue(await realSdkHttpFailure(code));
    await render();
    expect(container.textContent).toContain('POS workspace');
  });

  it('HTTP 500 on the opening check is not treated as offline', async () => {
    (state.profile as Record<string, unknown>).ury_offline_capture = 1;
    await confirmOnline();
    vi.mocked(checkPOSOpening).mockRejectedValue(await realSdkHttpFailure(500));
    await render();
    expect(container.textContent).not.toContain('POS workspace');
  });

  it('a gateway 502 on the opening checklist keeps the confirmed opening', async () => {
    (state.profile as Record<string, unknown>).ury_offline_capture = 1;
    await confirmOnline();
    vi.mocked(checkPOSOpening).mockResolvedValue({ message: [opening] });
    vi.mocked(getChecklist).mockRejectedValue(await realSdkHttpFailure(502));
    await render();
    expect(container.textContent).toContain('POS workspace');
    expect(container.textContent).not.toContain('Please complete the checklist');
  });

  it('sign-out forgets the confirmed opening', async () => {
    (state.profile as Record<string, unknown>).ury_offline_capture = 1;
    await confirmOnline();
    await clearAppStorage();
    vi.mocked(checkPOSOpening).mockRejectedValue(await realSdkNetworkFailure());
    await render();
    expect(container.textContent).not.toContain('POS workspace');
  });
});

describe('floor order routes', () => {
  it('keeps the opening screen for a waiter on the cashier till route', async () => {
    state.user = { name: 'Salama Waiter', roles: ['URY Captain'] };
    vi.mocked(getPOSOpeningContext).mockResolvedValue({
      ...context,
      occupied_entry: { ...context.occupied_entry, user_full_name: 'Salama Cashier', joinable: false },
    });
    window.history.replaceState({}, '', '/pos/pos');
    await render();
    expect(container.textContent).toContain('Till already open');
    expect(container.textContent).not.toContain('POS workspace');
    const continued = [...container.querySelectorAll('button')].some((button) => /^Continue/.test(button.textContent || ''));
    expect(continued).toBe(false);
  });

  it('keeps the opening gate for a cashier on the floor order route', async () => {
    state.user = { name: 'cashier@example.com', roles: ['URY Cashier'] };
    window.history.replaceState({}, '', '/pos/order');
    await render();
    expect(checkPOSOpening).toHaveBeenCalledWith('cashier@example.com');
    expect(container.textContent).toContain('Till already open');
    expect(container.textContent).not.toContain('POS workspace');
  });

  it('keeps the opening gate when the billing role table is empty', async () => {
    state.user = { name: 'Salama Waiter', roles: ['URY Captain'] };
    state.profile.role_allowed_for_billing = [];
    window.history.replaceState({}, '', '/pos/order');
    await render();
    expect(checkPOSOpening).toHaveBeenCalledWith('Salama Waiter');
    expect(container.textContent).toContain('Till already open');
    expect(container.textContent).not.toContain('POS workspace');
  });

  it('keeps the opening gate for Administrator on the floor order route', async () => {
    state.user = { name: 'Administrator', roles: ['System Manager'] };
    window.history.replaceState({}, '', '/pos/order');
    await render();
    expect(checkPOSOpening).toHaveBeenCalledWith('Administrator');
    expect(container.textContent).toContain('Till already open');
    expect(container.textContent).not.toContain('POS workspace');
  });
});
