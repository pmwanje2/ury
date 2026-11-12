// U-2: the dashboard must not show the "Shift Brief" placeholder card while no
// shift-summary source is connected. SAP Fiori overview pages: each card is
// bound to a data source (help.sap.com/docs/ABAP_PLATFORM_NEW/468a97775123488ab3345a0c48cadd8f/c64ef8c6c65d4effbfd512e9c9aa5044.html).
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import Dashboard from '../src/pages/Dashboard';

vi.mock('../src/store/root-store', () => ({ useRootStore: (selector?: any) => {
  const state = { user: { name: 'manager@example.com', roles: ['URY Manager'] } };
  return selector ? selector(state) : state;
} }));
vi.mock('../src/store/pos-store', () => ({ usePOSStore: () => ({ posProfile: { branch: 'Branch A' } }) }));
vi.mock('../src/lib/realtime', () => ({ getRealtimeSocket: async () => ({ on: vi.fn(), off: vi.fn() }) }));

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ message: [] }) })));
  vi.spyOn(call, 'get').mockResolvedValue({ message: [] });
  container = document.createElement('div'); document.body.append(container); root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount()); container.remove();
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

it('shows no Shift Brief placeholder when no shift-summary source is connected', async () => {
  await act(async () => root.render(<Dashboard />));
  expect(container.textContent).toContain('Floor Load');
  expect(container.textContent).not.toContain('Shift Brief');
  expect(container.textContent).not.toContain('not yet connected');
});
