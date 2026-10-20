import { act, createElement, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Header } from '../src/components/layout/Header';
import { WizardLayout } from '../src/components/setup/WizardLayout';

const resolveBrand = vi.hoisted(() =>
  vi.fn((defaults: { name: string; logo?: string }) => ({
    ...defaults,
    name: 'Maximus',
    logo: '/files/maximus.png',
  }))
);

vi.mock('@ury/core', () => ({
  resolveBrand,
  logoutSession: vi.fn(),
  clearAppStorage: vi.fn(),
  call: vi.fn(async () => ({ message: [] })),
  getLoggedUser: vi.fn(async () => null),
  getUserRoles: vi.fn(async () => ({ roles: [] })),
}));

vi.mock('@ury/ui', () => ({
  Button: (props: any) => createElement('button', props, props.children),
  showToast: { error: vi.fn() },
}));

vi.mock('react-router-dom', () => ({
  Link: ({ to, children, ...props }: { to: string; children?: ReactNode }) =>
    createElement('a', { ...props, href: to }, children),
  useNavigate: () => vi.fn(),
}));

vi.mock('../src/context/BranchContext', () => ({
  useBranchContext: () => ({
    activeBranchId: 'all',
    setActiveBranchId: vi.fn(),
    branches: [],
    activeBranch: null,
    filterContext: {},
  }),
}));

function mount(element: ReactNode) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => root.render(element));
  return { container, root };
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
});

afterEach(() => {
  document.body.replaceChildren();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('dashboard rendered brand surfaces', () => {
  it('renders the configured header logo and accessible brand name', () => {
    const { container, root } = mount(<Header />);
    const logo = container.querySelector('header img');
    expect(logo?.getAttribute('src')).toBe('/files/maximus.png');
    expect(logo?.getAttribute('alt')).toBe('Maximus');
    expect(logo?.getAttribute('alt')).not.toBe('URY Logo');
    act(() => root.unmount());
  });

  it('renders the configured wizard logo and footer brand', () => {
    const { container, root } = mount(
      <WizardLayout step={1}>
        <div>Setup</div>
      </WizardLayout>
    );
    const logo = container.querySelector('header img');
    expect(logo?.getAttribute('src')).toBe('/files/maximus.png');
    expect(logo?.getAttribute('alt')).toBe('Maximus');
    expect(container.textContent).toContain('Maximus · v3.2.0');
    act(() => root.unmount());
  });
});
