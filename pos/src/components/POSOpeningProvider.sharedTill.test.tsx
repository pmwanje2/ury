// Ported from fork pos/tests/POSOpeningProvider.test.tsx (#6: 82a14437, 40473b54;
// #71: 0dfeecc1, a2f057c9). The v16 provider gates only transactional routes,
// so these tests run on /orders and use the real POSOpeningScreen.
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initI18n } from '../i18n';
import {
  checkPOSOpening, createPOSOpening, getPOSOpeningContext, validatePOSClose, type POSOpeningContext,
} from '../lib/pos-opening-api';
import { getChecklist } from '../lib/checklist-api';
import POSOpeningProvider from './POSOpeningProvider';

const state = vi.hoisted(() => ({
  profile: { name: 'POS-1', company: 'Test Co', custom_daily_pos_close: 1 },
  user: { name: 'operator@example.com', roles: ['URY Cashier'] },
}));
vi.mock('../store/root-store', () => ({ useRootStore: () => ({ user: state.user }) }));
vi.mock('../store/pos-store', () => ({ usePOSStore: () => ({
  posProfile: state.profile, showVoluntaryClosing: false, setShowVoluntaryClosing: vi.fn(),
}) }));
vi.mock('../lib/pos-opening-api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../lib/pos-opening-api')>(),
  checkPOSOpening: vi.fn(), getPOSOpeningContext: vi.fn(),
  createPOSOpening: vi.fn(), validatePOSClose: vi.fn(),
}));
vi.mock('../lib/checklist-api', () => ({ getChecklist: vi.fn() }));
vi.mock('./ChecklistGateDialog', () => ({
  default: ({ onComplete }: { onComplete: () => void }) => (
    <button type="button" onClick={onComplete}>Finish opening checklist</button>
  ),
}));
vi.mock('./POSClosingDialog', () => ({ default: () => null }));

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
const asContext = (value: unknown) => value as POSOpeningContext;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.clearAllMocks();
  await initI18n('en');
  vi.mocked(checkPOSOpening).mockResolvedValue({ message: [] });
  vi.mocked(getPOSOpeningContext).mockResolvedValue(asContext(context));
  vi.mocked(validatePOSClose).mockResolvedValue({ message: 'Success' });
  vi.mocked(getChecklist).mockResolvedValue({ items: [], logName: null, logStatus: 'Complete', blockedBy: null });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

function Billing() {
  const navigate = useNavigate();
  return (
    <main>
      POS workspace
      <button type="button" onClick={() => navigate('/orders')}>Open billing</button>
    </main>
  );
}

async function render(path = '/orders') {
  await act(async () => root.render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <POSOpeningProvider><Billing /></POSOpeningProvider>
    </MemoryRouter>,
  ));
}

function continueButton() {
  const button = Array.from(container.querySelectorAll('button')).find((b) => b.textContent === 'Continue');
  expect(button, 'Eligible operator must have a Continue button').toBeDefined();
  return button!;
}

describe('shared till continuation', () => {
  it('resumes the existing opening through the checklist and daily-close path without creating one', async () => {
    await render();
    expect(container.textContent).toContain('Till opened by Till Opener since');
    expect(container.querySelector('input')).toBeNull();
    expect(container.textContent).not.toContain('POS workspace');
    vi.mocked(getChecklist).mockClear();
    await act(async () => continueButton().click());
    expect(container.textContent).toContain('POS workspace');
    expect(getChecklist).toHaveBeenCalledWith('POS-1', 'Opening');
    expect(validatePOSClose).toHaveBeenCalledWith('POS-1');
    expect(createPOSOpening).not.toHaveBeenCalled();
    expect(checkPOSOpening).toHaveBeenCalledWith('operator@example.com');
  });

  it('does not offer Continue or an opening form on a non-joinable occupied till', async () => {
    vi.mocked(getPOSOpeningContext).mockResolvedValue(asContext({ ...context,
      occupied_entry: { ...context.occupied_entry, joinable: false },
    }));
    await render();
    expect(container.textContent).toContain('must close the shift');
    expect(container.querySelector('input')).toBeNull();
    expect(container.textContent).not.toMatch(/Continue|POS workspace/);
    expect(createPOSOpening).not.toHaveBeenCalled();
  });

  it('can continue without permission to create or submit a new opening', async () => {
    vi.mocked(getPOSOpeningContext).mockResolvedValue(asContext({ ...context, permissions: { create: false, submit: false } }));
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

  it('explains a pending daily close through the existing gate instead of hiding Continue', async () => {
    vi.mocked(getPOSOpeningContext).mockResolvedValue(asContext({ ...context, daily_close_pending: true }));
    await render();
    vi.mocked(validatePOSClose).mockResolvedValue({ message: 'Failed' });
    await act(async () => continueButton().click());
    expect(container.textContent).not.toContain('POS workspace');
    expect(container.textContent).toContain("Yesterday's session was never closed");
    expect(createPOSOpening).not.toHaveBeenCalled();
  });

  it('keeps the opening checklist gate and resumes the same till when completed', async () => {
    await render();
    vi.mocked(getChecklist).mockResolvedValue({ items: [], logName: 'LOG-1', logStatus: 'Pending', blockedBy: null });
    await act(async () => continueButton().click());
    expect(container.textContent).not.toContain('POS workspace');
    const finish = [...container.querySelectorAll('button')].find((b) => b.textContent === 'Finish opening checklist');
    expect(finish).toBeDefined();
    vi.mocked(getChecklist).mockResolvedValue({ items: [], logName: 'LOG-1', logStatus: 'Complete', blockedBy: null });
    await act(async () => finish!.click());
    expect(container.textContent).toContain('POS workspace');
    expect(createPOSOpening).not.toHaveBeenCalled();
  });

  it('does not let a shared entry bypass the existing company/profile guard', async () => {
    vi.mocked(getPOSOpeningContext).mockResolvedValue(asContext({ ...context, company: 'Other Co' }));
    await render();
    vi.mocked(getChecklist).mockClear();
    await act(async () => continueButton().click());
    expect(container.textContent).not.toContain('POS workspace');
    expect(getChecklist).not.toHaveBeenCalled();
  });

  it('keeps own-opening resume working without a Continue screen', async () => {
    vi.mocked(checkPOSOpening).mockResolvedValue({ message: [opening] });
    await render();
    expect(container.textContent).toContain('POS workspace');
    expect(getPOSOpeningContext).not.toHaveBeenCalled();
    expect(getChecklist).toHaveBeenCalledWith('POS-1', 'Opening');
  });
});

describe('floor order routes (#71, upstream route gating)', () => {
  it('admits a waiter on the floor order route without the opening screen', async () => {
    state.user = { name: 'waiter@example.com', roles: ['URY Captain'] };
    try {
      await render('/order');
      expect(container.textContent).toContain('POS workspace');
      expect(container.textContent).not.toContain('Till already open');
    } finally {
      state.user = { name: 'operator@example.com', roles: ['URY Cashier'] };
    }
  });

  it('shows the opening gate again after a router push from /order to a billing route', async () => {
    vi.mocked(getPOSOpeningContext).mockResolvedValue(asContext({ ...context, occupied_entry: null }));
    await render('/order');
    expect(container.textContent).toContain('POS workspace');
    const openBilling = [...container.querySelectorAll('button')].find((b) => b.textContent === 'Open billing');
    await act(async () => openBilling!.click());
    expect(container.textContent).toContain('Open POS Session');
    expect(container.textContent).not.toContain('POS workspace');
  });
});
