// Ported from fork pos/tests/captain-route-guard.test.tsx (#71, 0dfeecc1).
// The fork's "switch waiter" action belongs to the T4 floor-session port.
import { act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initI18n } from '../../i18n';
import CaptainRouteGuard from './CaptainRouteGuard';

const boundary = vi.hoisted(() => ({
  refetch: vi.fn(async () => {}),
  openingState: { pos_open: true } as { pos_open: boolean } | null,
  canTakeTableOrders: true,
}));

vi.mock('../hooks/useCaptainContext', () => ({
  useCaptainContext: () => ({
    context: null,
    branch: 'B1',
    capabilities: { canTakeTableOrders: boundary.canTakeTableOrders },
    openingState: boundary.openingState,
    isLoading: false,
    error: null,
    refetch: boundary.refetch,
  }),
}));
vi.mock('./ServiceRequestPanel', () => ({ default: () => null }));
vi.mock('../../components/ServiceRequestAlert', () => ({ default: () => null }));

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.clearAllMocks();
  boundary.openingState = { pos_open: true };
  boundary.canTakeTableOrders = true;
  await initI18n('en');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function renderGuard(children: ReactNode = 'Captain page') {
  await act(async () => {
    root.render(<CaptainRouteGuard><div>{children}</div></CaptainRouteGuard>);
  });
}

const TILL_CLOSED = 'The till is not open. Ask the cashier to open it.';

describe('captain route guard till', () => {
  it('renders the order pad when the branch till is open', async () => {
    await renderGuard();
    expect(container.textContent).toContain('Captain page');
  });

  it('blocks a closed till and retries the captain context', async () => {
    boundary.openingState = { pos_open: false };
    await renderGuard();
    expect(container.textContent).toContain(TILL_CLOSED);
    expect(container.textContent).not.toContain('Captain page');
    const retry = [...container.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Retry');
    expect(retry).toBeDefined();
    await act(async () => retry!.click());
    expect(boundary.refetch).toHaveBeenCalled();
  });

  it('blocks when the opening state is missing', async () => {
    boundary.openingState = null;
    await renderGuard();
    expect(container.textContent).toContain(TILL_CLOSED);
    expect(container.textContent).not.toContain('Captain page');
  });

  it('still refuses a user who cannot take table orders', async () => {
    boundary.canTakeTableOrders = false;
    await renderGuard();
    expect(container.textContent).toContain('Not permitted');
    expect(container.textContent).not.toContain('Captain page');
    expect(container.textContent).not.toContain(TILL_CLOSED);
  });

  it('names the closed till in Arabic and French', async () => {
    boundary.openingState = { pos_open: false };
    for (const [language, fragment] of [['ar', 'الصندوق غير مفتوح'], ['fr', 'La caisse n’est pas ouverte']] as const) {
      await initI18n(language);
      await renderGuard();
      expect(container.textContent).toContain(fragment);
    }
  });
});
