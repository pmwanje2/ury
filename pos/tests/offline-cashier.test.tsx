import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { db } from '@ury/core';
import { initI18n } from '../src/i18n';
import { useRootStore } from '../src/store/root-store';
import Header from '../src/components/Header';
import { createPosClosingEntry, submitPosClosingEntry, createSubPosClosing, submitSubPosClosing } from '../src/lib/pos-closing-api';

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('indexedDB', new IDBFactory());
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: false });
  await initI18n('en');
  document.cookie = 'user_id=cashier%40example.com';
  useRootStore.setState({ user: { name: 'cashier@example.com', full_name: 'Cashier', roles: ['Cashier'] } });
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals();
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true }); });

const button = (text: RegExp) => Array.from(document.querySelectorAll('button')).find((b) => text.test(b.textContent || ''))!;

test('till close is disabled offline in the real header', async () => {
  await act(async () => root.render(<MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><Header /></MemoryRouter>));
  await act(async () => button(/^Cashier/).click());
  expect(button(/Close Shift/i).disabled).toBe(true);
});

test.each([
  ['main draft', () => createPosClosingEntry({} as any)], ['main submit', () => submitPosClosingEntry('C1')],
  ['sub draft', () => createSubPosClosing({} as any)], ['sub submit', () => submitSubPosClosing('C1')],
])('%s cannot bypass offline close through its API wrapper', async (_name, close) => {
  vi.spyOn(db, 'createDoc').mockResolvedValue({ name: 'C1' });
  vi.spyOn(db, 'updateDoc').mockResolvedValue({ name: 'C1' });
  await expect(close()).rejects.toThrow(/offline|outbox/i);
  expect(db.createDoc).not.toHaveBeenCalled(); expect(db.updateDoc).not.toHaveBeenCalled();
});
