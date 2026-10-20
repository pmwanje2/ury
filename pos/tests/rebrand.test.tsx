import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import InitialLoader from '../src/components/InitialLoader';
import ScreenSizeDialog from '../src/components/ScreenSizeDialog';
import AuthGuard from '../src/components/AuthGuard';
import { initI18n } from '../src/i18n';

const authStore = vi.hoisted(() => ({
  checkAuth: vi.fn(),
  user: { name: 'cashier@example.com', roles: ['URY Cashier'] },
  isLoading: false,
  error: null,
  fetchPosProfile: vi.fn(),
  posProfile: {},
  hasAccess: false,
  allowedRoles: ['URY Cashier'],
}));

const browser = window as Window & { frappe?: { boot?: { ury_brand?: unknown } } };

vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector?: (state: typeof authStore) => unknown) =>
    selector ? selector(authStore) : authStore,
}));

function render(element: React.ReactElement) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => root.render(element));
  return { container, root };
}

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  browser.frappe = { boot: { ury_brand: { name: 'Maximus', logo: '/files/maximus.png' } } };
  await initI18n('en');
});

afterEach(() => {
  document.body.replaceChildren();
  delete browser.frappe;
  vi.clearAllMocks();
});

describe('POS rebrand surfaces', () => {
  it('loads the configured brand name', () => {
    const { container, root } = render(<InitialLoader />);
    expect(container.textContent).toContain('Loading Maximus...');
    expect(container.textContent).not.toContain('Loading URY POS...');
    act(() => root.unmount());
  });

  it('describes the mobile fallback as the classic POS', () => {
    const { container, root } = render(<ScreenSizeDialog />);
    expect(container.textContent).toContain('the classic POS');
    expect(container.textContent).not.toContain('URY POS Version 1');
    act(() => root.unmount());
  });

  it('does not expose the raw role list on access denial', () => {
    const { container, root } = render(<AuthGuard><div>content</div></AuthGuard>);
    expect(container.textContent).not.toContain('Required roles:');
    expect(container.textContent).not.toContain('URY Cashier');
    act(() => root.unmount());
  });
});
