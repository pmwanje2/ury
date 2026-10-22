import { act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initI18n } from '../src/i18n';
import CaptainRouteGuard from '../src/captain/components/CaptainRouteGuard';

const boundary = vi.hoisted(() => ({
  refetch: vi.fn(async () => {}),
  floorSignOut: vi.fn(async () => {}),
  openingState: { pos_open: true } as { pos_open: boolean } | null,
  canTakeTableOrders: true,
  isLoading: false,
  error: null as string | null,
}));

vi.mock('../src/captain/hooks/useCaptainContext', () => ({
  useCaptainContext: () => ({
    context: null,
    capabilities: { canTakeTableOrders: boundary.canTakeTableOrders },
    openingState: boundary.openingState,
    isLoading: boundary.isLoading,
    error: boundary.error,
    refetch: boundary.refetch,
  }),
}));
vi.mock('../src/lib/floor-session', () => ({ floorSignOut: (...args: unknown[]) => boundary.floorSignOut(...args) }));

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.clearAllMocks();
  boundary.openingState = { pos_open: true };
  boundary.canTakeTableOrders = true;
  boundary.isLoading = false;
  boundary.error = null;
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

describe('captain route guard till', () => {
  it('renders the order pad when the branch till is open', async () => {
    await renderGuard();
    expect(container.textContent).toContain('Captain page');
  });

  it('blocks a closed till and retries the captain context', async () => {
    boundary.openingState = { pos_open: false };
    await renderGuard();
    expect(container.textContent).toContain('The till is not open. Ask the cashier to open it.');
    expect(container.textContent).not.toContain('Captain page');
    const retry = [...container.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Retry');
    expect(retry).toBeDefined();
    await act(async () => retry!.click());
    expect(boundary.refetch).toHaveBeenCalled();
  });

  it('blocks when the opening state is missing', async () => {
    boundary.openingState = null;
    await renderGuard();
    expect(container.textContent).toContain('The till is not open. Ask the cashier to open it.');
    expect(container.textContent).not.toContain('Captain page');
  });

  it('still refuses a user who cannot take table orders', async () => {
    boundary.canTakeTableOrders = false;
    boundary.openingState = { pos_open: true };
    await renderGuard();
    expect(container.textContent).toContain('Not permitted');
    expect(container.textContent).not.toContain('Captain page');
    expect(container.textContent).not.toContain('The till is not open. Ask the cashier to open it.');
  });
});
