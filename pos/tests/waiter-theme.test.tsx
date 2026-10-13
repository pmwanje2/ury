import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import CaptainOrder from '../src/captain/pages/CaptainOrder';

vi.mock('../src/captain/hooks/useTableOrderContext', () => ({ useTableOrderContext: () => ({
  context: {}, permissions: { view: true, modify: true }, isContextLoading: false,
  contextError: null, isOrderReady: true, alreadyOrderedLines: [], newOrChangedLines: [], reductionPendingLines: [],
}) }));
vi.mock('../src/captain/components/CaptainMenu', () => ({ default: () => null }));

it('the real mobile waiter Menu header uses the same theme and contrast tokens', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<MemoryRouter initialEntries={['/order/table/MD-03']}>
      <Routes><Route path="/order/table/:table" element={<CaptainOrder />} /></Routes>
    </MemoryRouter>));
    const heading = container.querySelector('h1')!;
    expect(heading.textContent).toBe('Table MD-03');
    const header = heading.parentElement!.parentElement!.parentElement!;
    expect(header.classList.contains('bg-header')).toBe(true);
    expect(header.classList.contains('text-header-foreground')).toBe(true);
    expect(heading.classList.contains('text-header-title')).toBe(true);
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});
