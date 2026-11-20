// Ported from fork pos/tests/POSOpeningScreen.test.tsx (ffe5f38c, 90038eea,
// d3cd943d, cb80fde7) and the opening-screen logout cases of pos/tests/logout.test.ts (82a14437).
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as core from '@ury/core';
import { showToast } from '@ury/ui';
import { initI18n } from '../i18n';
import { getPOSOpeningContext, type POSOpeningContext } from '../lib/pos-opening-api';
import POSOpeningScreen from './POSOpeningScreen';

vi.mock('../store/root-store', () => ({ useRootStore: () => ({ user: null }) }));
vi.mock('../lib/pos-opening-api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../lib/pos-opening-api')>(),
  getPOSOpeningContext: vi.fn(),
}));
vi.mock('@ury/core', async (importOriginal) => ({
  ...await importOriginal<typeof import('@ury/core')>(),
  logout: vi.fn(),
}));

type Occupied = { name?: string; joinable?: boolean; user_full_name: string | null; period_start_date: string };
type Context = POSOpeningContext & { occupied_entry?: Occupied | null };

let container: HTMLDivElement;
let root: Root;
const originalLocation = window.location;

const context: Context = {
  company: 'Test Co',
  allowed_profiles: [{ name: 'POS-1' }],
  selected_profile: 'POS-1',
  payment_modes: [{ mode_of_payment: 'Cash', opening_amount: 0 }],
  user: 'cashier@example.com',
  user_full_name: 'Current Cashier',
  branch: 'Branch A',
  restaurant: 'Rest A',
  daily_close_pending: false,
  multi_cashier: { enabled: false, main_cashier_open: false, main_cashier_configured: false },
  permissions: { create: true, submit: true },
  occupied_entry: {
    user_full_name: 'Other Cashier',
    period_start_date: '2026-10-01 08:10:00',
  },
};

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  await initI18n('en');
  vi.mocked(core.logout).mockReset();
  Object.defineProperty(window, 'location', {
    configurable: true, value: { ...originalLocation, href: '/pos/orders' },
  });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  Object.defineProperty(window, 'location', { configurable: true, value: originalLocation });
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function renderContext(data: Context = context) {
  vi.mocked(getPOSOpeningContext).mockResolvedValue(data as POSOpeningContext);
  await act(async () => root.render(<POSOpeningScreen />));
  return container.querySelector('p.text-orange-700')?.textContent || '';
}

describe('occupied till message', () => {
  it('blocks the opening form and names the holder and profile', async () => {
    const message = await renderContext();
    expect(container.textContent).toContain('Till already open');
    expect(message).toContain('POS-1');
    expect(message).toContain('Other Cashier');
    expect(message).toContain('must close the shift');
    expect(container.querySelector('input')).toBeNull();
    expect(container.textContent).not.toContain('Open POS Session');
  });

  it.each([true, false])('shows the occupied opening time in the Session Start badge (joinable=%s)', async (joinable) => {
    await renderContext({
      ...context,
      session_start: '2026-10-01 12:00:00',
      occupied_entry: { ...context.occupied_entry!, joinable },
    });
    const badge = Array.from(container.querySelectorAll('div'))
      .find((element) => element.textContent?.startsWith('Session Start:') && element.children.length === 0);
    expect(badge?.textContent).toBe(`Session Start: ${new Date('2026-10-01T08:10:00').toLocaleString()}`);
  });

  it('formats the start time for humans rather than showing the raw database timestamp', async () => {
    const formatDate = vi.spyOn(Date.prototype, 'toLocaleString');
    const message = await renderContext();
    expect(message).toMatch(/since .+;/);
    expect(message).not.toContain('2026-10-01 08:10:00');
    expect(formatDate).toHaveBeenCalledWith('en');
  });

  it('formats the start time using the active French language', async () => {
    await initI18n('fr');
    const formatDate = vi.spyOn(Date.prototype, 'toLocaleString');
    const message = await renderContext();
    expect(message).toMatch(/depuis .+ ;/);
    expect(formatDate).toHaveBeenCalledWith('fr');
  });

  it('never displays an email when the holder has no full name', async () => {
    const message = await renderContext({
      ...context,
      occupied_entry: { ...context.occupied_entry!, user_full_name: 'other@example.com' },
    });
    expect(message).toContain('POS-1');
    expect(message).toContain('another cashier');
    expect(container.textContent).not.toContain('other@example.com');
  });

  it.each([
    ['en', 'another cashier'],
    ['fr', 'un autre caissier'],
    ['ar', 'أمين صندوق آخر'],
  ])('renders a translated holder fallback in %s when the full name is missing', async (language, holder) => {
    await initI18n(language);
    const message = await renderContext({
      ...context,
      occupied_entry: { ...context.occupied_entry!, user_full_name: null },
    });
    expect(message).toContain(holder);
    expect(message).toContain('POS-1');
    expect(message).not.toContain('{{');
  });

  it('renders an Arabic occupied-till message with the profile and holder', async () => {
    await initI18n('ar');
    const formatDate = vi.spyOn(Date.prototype, 'toLocaleString');
    const message = await renderContext();
    expect(message).toMatch(/[\u0600-\u06ff]/);
    expect(message).toContain('POS-1');
    expect(message).toContain('Other Cashier');
    expect(message).not.toContain('{{');
    expect(formatDate).toHaveBeenCalledWith('ar');
  });

  it('offers Continue only for a joinable occupied till and passes that opening back', async () => {
    const onContinue = vi.fn();
    vi.mocked(getPOSOpeningContext).mockResolvedValue({
      ...context,
      occupied_entry: { ...context.occupied_entry!, name: 'OPEN-1', joinable: true },
    } as POSOpeningContext);
    await act(async () => root.render(<POSOpeningScreen onContinue={onContinue} />));
    expect(container.textContent).toContain('Till opened by Other Cashier since');
    const continueButton = [...container.querySelectorAll('button')].find((button) => button.textContent === 'Continue');
    expect(continueButton).toBeDefined();
    await act(async () => continueButton!.click());
    expect(onContinue).toHaveBeenCalledExactlyOnceWith({
      name: 'OPEN-1', company: 'Test Co', pos_profile: 'POS-1', period_start_date: '2026-10-01 08:10:00',
    });
  });

  it('does not offer Continue on a non-joinable occupied till', async () => {
    vi.mocked(getPOSOpeningContext).mockResolvedValue({
      ...context,
      occupied_entry: { ...context.occupied_entry!, name: 'OPEN-1', joinable: false },
    } as POSOpeningContext);
    await act(async () => root.render(<POSOpeningScreen onContinue={vi.fn()} />));
    expect([...container.querySelectorAll('button')].some((button) => button.textContent === 'Continue')).toBe(false);
  });
});

describe('opening screen logout', () => {
  const logoutButton = () => [...container.querySelectorAll('button')].find((button) => button.textContent === 'Logout');

  it.each([false, true])('logs out once from the opening screen (occupied=%s)', async (occupied) => {
    let finish!: () => void;
    vi.mocked(core.logout).mockImplementation(() => new Promise<false | void>((resolve) => { finish = () => resolve(); }));
    await renderContext({ ...context, occupied_entry: occupied ? context.occupied_entry : null });
    const button = logoutButton();
    expect(button, 'Opening and occupied screens must expose Log out').toBeDefined();
    await act(async () => { button!.click(); });
    expect(logoutButton()!.disabled).toBe(true);
    await act(async () => { logoutButton()!.click(); });
    expect(core.logout).toHaveBeenCalledTimes(1);
    await act(async () => finish());
    expect(window.location.href).toBe('/login?redirect-to=%2Fpos');
  });

  it('stays put and allows a retry when logout fails', async () => {
    vi.mocked(core.logout).mockRejectedValue(new Error('Offline'));
    const error = vi.spyOn(showToast, 'error').mockImplementation(() => '' as never);
    await renderContext();
    expect(logoutButton(), 'The occupied screen must expose Log out').toBeDefined();
    await act(async () => { logoutButton()!.click(); });
    expect(window.location.href).toBe('/pos/orders');
    expect(error).toHaveBeenCalledWith('Failed to logout. Please try again.');
    expect(logoutButton()!.disabled).toBe(false);
  });
});
