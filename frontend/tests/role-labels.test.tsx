import { act, createElement, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ROLE_LABELS, roleLabel, ROLE_OPTIONS } from '../src/lib/roleLabels';
import { QuickActions } from '../src/pages/Dashboard/QuickActions';

vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }));
vi.mock('../src/context/BranchContext', () => ({
  useBranchContext: () => ({ refreshDashboard: vi.fn() }),
}));

const EXPECTED: Record<string, string> = {
  'URY Manager': 'Restaurant POS Manager',
  'URY Captain': 'Restaurant POS Captain',
  'URY Cashier': 'Restaurant POS Cashier',
  'URY Admin': 'Restaurant POS Admin',
};

function mount(element: ReactNode) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => root.render(element));
  return { container, root };
}

beforeEach(() => vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true));
afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

describe('shared restaurant role labels', () => {
  it('maps the real URY role names to the SiteOps Restaurant POS labels', () => {
    expect(ROLE_LABELS).toEqual(EXPECTED);
    for (const [value, label] of Object.entries(EXPECTED)) expect(roleLabel(value)).toBe(label);
    expect(roleLabel('System Manager')).toBe('System Manager');
  });

  it('offers unique labels while keeping role values unchanged', () => {
    const values = ROLE_OPTIONS.map((option) => option.value);
    const labels = ROLE_OPTIONS.map((option) => option.label);
    expect(values).toEqual(Object.keys(EXPECTED));
    expect(new Set(labels).size).toBe(labels.length);
    expect(labels).not.toContain('POS Manager');
  });

  it('shows the display label, not the raw role, after adding a user', () => {
    const { container, root } = mount(createElement(QuickActions));
    const card = [...container.querySelectorAll('h3')].find((h) => h.textContent === 'Add User / Staff');
    act(() => (card?.closest('[class*="cursor-pointer"]') as HTMLElement).click());
    const inputs = [...document.querySelectorAll('form input')] as HTMLInputElement[];
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    act(() => {
      setValue.call(inputs[0], 'Jane Doe');
      inputs[0].dispatchEvent(new Event('input', { bubbles: true }));
      setValue.call(inputs[1], 'jane@example.test');
      inputs[1].dispatchEvent(new Event('input', { bubbles: true }));
    });
    act(() => (document.querySelector('form') as HTMLFormElement).requestSubmit());
    expect(document.body.textContent).toContain('as Restaurant POS Cashier.');
    expect(document.body.textContent).not.toContain('as URY Cashier.');
    act(() => root.unmount());
  });
});
