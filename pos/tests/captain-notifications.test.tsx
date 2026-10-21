import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Button } from '@ury/ui';
import { initI18n } from '../src/i18n';
import Notifications from '../src/components/Notifications';
import CaptainTables from '../src/captain/pages/CaptainTables';

type NotificationRow = {
  name: string;
  for_user: string;
  subject: string;
  email_content: string;
  creation: string;
  read: number;
  document_type: string;
  document_name: string;
};

const boundary = vi.hoisted(() => ({
  session: 'waiter@example.com' as string | null,
  rows: [] as NotificationRow[],
  fetchAlerts: vi.fn(),
  get: vi.fn(),
  toastInfo: vi.fn(),
  socketHandlers: new Map<string, Set<() => void>>(),
}));

vi.mock('@ury/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@ury/core')>();
  return {
    ...actual,
    getSessionUser: () => boundary.session,
    call: { ...actual.call, get: boundary.get, post: vi.fn() },
  };
});

vi.mock('@ury/ui', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@ury/ui')>();
  return { ...actual, showToast: { ...actual.showToast, info: boundary.toastInfo } };
});

vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector?: (state: unknown) => unknown) => {
    const state = { user: boundary.session ? { name: boundary.session, roles: ['URY Captain'] } : null };
    return selector ? selector(state) : state;
  },
}));

vi.mock('../src/lib/realtime', () => ({
  getRealtimeSocket: async () => ({
    on: (event: string, handler: () => void) => {
      if (!boundary.socketHandlers.has(event)) boundary.socketHandlers.set(event, new Set());
      boundary.socketHandlers.get(event)!.add(handler);
    },
    off: (event: string, handler: () => void) => boundary.socketHandlers.get(event)?.delete(handler),
  }),
}));

vi.mock('../src/captain/hooks/useCaptainContext', () => ({
  useCaptainContext: () => ({
    context: { user: 'waiter@example.com', branch: 'Branch A', rooms: [], roles: [], pos_profile: null },
    capabilities: { canAccessOtherCaptainsTables: false },
    branch: 'Branch A',
    rooms: [],
    isLoading: false,
    error: null,
  }),
}));

vi.mock('../src/lib/table-api', () => ({
  getRooms: vi.fn(async () => [{ name: 'Dining', branch: 'Branch A' }]),
  getTables: vi.fn(async () => [{ name: 'T-05', occupied: 0, latest_invoice_time: null, is_take_away: 0, restaurant_room: 'Dining', table_shape: 'Square' }]),
}));

vi.mock('../src/captain/lib/captain-table-api', () => ({
  getActiveTableOrders: vi.fn(async () => new Map()),
  getUpcomingBookings: vi.fn(async () => new Map()),
  getUserFullNames: vi.fn(async () => new Map()),
}));

vi.mock('../src/captain/components/CaptainTableCard', () => ({
  default: ({ table, onTap }: { table: { name: string }; onTap: () => void }) => (
    <Button onClick={onTap}>{table.name}</Button>
  ),
}));
vi.mock('../src/lib/floor-session', () => ({ floorSignOut: vi.fn(async () => {}) }));

let container: HTMLDivElement;
let root: Root;

const notification = (changes: Partial<NotificationRow> = {}): NotificationRow => ({
  name: 'KOT-NOTIF-1',
  for_user: 'waiter@example.com',
  subject: 'Food ready: Table T-05',
  email_content: 'Collect the dishes',
  creation: '2026-10-20 12:00:00',
  read: 0,
  document_type: 'URY KOT',
  document_name: 'KOT-1',
  ...changes,
});

function panel() {
  return container.querySelector('[aria-label="Kitchen"]') ?? container;
}

function LocationProbe() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

async function renderNotifications(props: { onNewUnread?: () => void } = {}) {
  await act(async () => root.render(<Notifications title="Kitchen" {...props} />));
}

async function renderTables() {
  await act(async () => root.render(
    <MemoryRouter initialEntries={['/order']}>
      <Routes>
        <Route path="/order" element={<CaptainTables />} />
        <Route path="/order/table/:table" element={<div>Order screen</div>} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  ));
}

async function emit(event: string) {
  await act(async () => {
    for (const handler of boundary.socketHandlers.get(event) ?? []) handler();
  });
}

async function waitForRealtimeHandler() {
  await vi.waitFor(() => expect(boundary.socketHandlers.get('notification')?.size).toBe(1));
}

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  await initI18n('en');
  boundary.session = 'waiter@example.com';
  boundary.rows = [notification()];
  boundary.fetchAlerts.mockReset().mockImplementation(async () => ({
    ok: true,
    json: async () => ({ message: boundary.rows }),
  }));
  boundary.get.mockReset().mockImplementation(async (method: string) => {
    if (method === 'ury.ury.api.ury_server_time.get_server_time') return { message: '2026-10-20 12:05:00' };
    if (method === 'frappe.client.get') return { message: { restaurant_table: 'T-05' } };
    throw new Error(`Unexpected method ${method}`);
  });
  boundary.toastInfo.mockReset();
  boundary.socketHandlers.clear();
  vi.stubGlobal('fetch', boundary.fetchAlerts);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

describe('captain kitchen notifications', () => {
  it('renders only notification rows addressed to the session user', async () => {
    boundary.rows = [notification(), notification({ name: 'OTHER', for_user: 'other@example.com', subject: 'Private other waiter alert' })];
    await renderNotifications();

    expect(panel().textContent).toContain('Food ready: Table T-05');
    expect(panel().textContent).not.toContain('Private other waiter alert');
    const request = new URL(boundary.fetchAlerts.mock.calls[0][0], 'http://localhost');
    expect(JSON.parse(request.searchParams.get('filters')!)).toEqual({ for_user: 'waiter@example.com' });
  });

  it('shows the existing toast when a realtime event raises the unread count', async () => {
    boundary.rows = [notification({ read: 1 })];
    await renderTables();
    await vi.waitFor(() => expect(container.querySelector('[aria-label="Kitchen"]')).not.toBeNull());
    await waitForRealtimeHandler();

    boundary.rows = [notification({ name: 'KOT-NOTIF-2', read: 0 }), notification({ read: 1 })];
    await emit('notification');

    expect(boundary.toastInfo).toHaveBeenCalledWith(expect.stringMatching(/food-ready|kitchen/i));
  });

  it('does not toast on the first load even when rows are unread', async () => {
    await renderNotifications({ onNewUnread: () => boundary.toastInfo('New food-ready') });
    await vi.waitFor(() => expect(panel().textContent).toContain('Food ready: Table T-05'));

    expect(boundary.toastInfo).not.toHaveBeenCalled();
  });

  it('does not toast when the same unread rows are fetched again', async () => {
    await renderNotifications({ onNewUnread: () => boundary.toastInfo('New food-ready') });
    await vi.waitFor(() => expect(panel().textContent).toContain('Food ready: Table T-05'));
    await waitForRealtimeHandler();

    await emit('notification');

    expect(boundary.toastInfo).not.toHaveBeenCalled();
  });

  it('toasts for a new unread row after an existing row is marked read', async () => {
    await renderNotifications({ onNewUnread: () => boundary.toastInfo('New food-ready') });
    await vi.waitFor(() => expect(panel().textContent).toContain('Food ready: Table T-05'));
    await waitForRealtimeHandler();

    const markRead = Array.from(panel().querySelectorAll<HTMLButtonElement>('button'))
      .find((candidate) => candidate.textContent === 'Mark read');
    expect(markRead).toBeDefined();
    await act(async () => markRead!.click());
    await vi.waitFor(() => expect(panel().textContent).toContain('Read'));

    boundary.rows = [
      notification({ name: 'KOT-NOTIF-2', subject: 'Food ready: Table T-06' }),
      notification({ read: 1 }),
    ];
    await emit('notification');

    await vi.waitFor(() => expect(boundary.toastInfo).toHaveBeenCalledWith(expect.stringMatching(/food-ready|kitchen/i)));
  });

  it('toasts when a new unread row replaces one of ten capped rows', async () => {
    const initialRows = Array.from({ length: 10 }, (_, index) => notification({
      name: `KOT-NOTIF-${index + 1}`,
      subject: `Food ready: Table T-${index + 1}`,
    }));
    boundary.rows = initialRows;
    await renderNotifications({ onNewUnread: () => boundary.toastInfo('New food-ready') });
    await vi.waitFor(() => expect(panel().textContent).toContain('Food ready: Table T-10'));
    await waitForRealtimeHandler();

    boundary.rows = [
      ...initialRows.slice(1),
      notification({ name: 'KOT-NOTIF-11', subject: 'Food ready: Table T-11' }),
    ];
    await emit('notification');

    await vi.waitFor(() => expect(boundary.toastInfo).toHaveBeenCalledWith(expect.stringMatching(/food-ready|kitchen/i)));
  });

  it('opens a kitchen notification on the KOT restaurant table', async () => {
    await renderTables();
    await vi.waitFor(() => expect(container.querySelector('[aria-label="Kitchen"]')).not.toBeNull());

    const openCheck = Array.from(panel().querySelectorAll<HTMLButtonElement>('button'))
      .find((candidate) => candidate.textContent === 'Open check');
    expect(openCheck).toBeDefined();
    await act(async () => openCheck!.click());

    expect(boundary.get).toHaveBeenCalledWith('frappe.client.get', { doctype: 'URY KOT', name: 'KOT-1' });
    expect(container.querySelector('[data-testid="location"]')?.textContent).toBe('/order/table/T-05');
  });
});
