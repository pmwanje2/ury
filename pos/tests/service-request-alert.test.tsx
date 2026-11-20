import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { act, type ComponentType } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { initI18n } from '../src/i18n';

// Call waiter: a guest request must be impossible to miss on every staff
// screen until someone acknowledges it (Oracle Simphony: guest requests stay
// on the floor devices as an alert until acknowledged).

const socket = vi.hoisted(() => {
  const handlers = new Map<string, Array<(payload: unknown) => void>>();
  const emitted: unknown[][] = [];
  return {
    handlers,
    emitted,
    on: (event: string, fn: (payload: unknown) => void) => {
      handlers.set(event, [...(handlers.get(event) ?? []), fn]);
    },
    off: (event: string, fn: (payload: unknown) => void) => {
      handlers.set(event, (handlers.get(event) ?? []).filter(h => h !== fn));
    },
    emit: (...args: unknown[]) => { emitted.push(args); },
    fire(event: string, payload: unknown) { (handlers.get(event) ?? []).forEach(fn => fn(payload)); },
  };
});
vi.mock('../src/lib/realtime', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/lib/realtime')>()),
  getRealtimeSocket: async () => socket,
}));

const plays = vi.hoisted(() => ({ count: 0 }));

let Alert: ComponentType<any> = () => null; // An absent view renders nothing, not an import-error RED.
let root: Root;
let container: HTMLDivElement;
let open: Array<Record<string, string>>;
let settings: { sound: number; escalate_minutes: number };
let acknowledged: string[];

const NOW = new Date('2026-11-20T14:58:00');

async function serverRead(method: string, args: any = {}) {
  if (method === 'ury.ury_pos.api.getBranch') return { message: 'Branch A' };
  if (method === 'ury.ury.api.service_requests.list_open_service_requests') {
    expect(args.branch).toBe('Branch A');
    return { message: open };
  }
  if (method === 'ury.ury.api.service_requests.get_service_request_alert_settings') return { message: settings };
  if (method === 'ury.ury.api.ury_server_time.get_server_time') return { message: '2026-11-20 14:58:00' };
  throw Error(`Unexpected read ${method}`);
}
async function serverWrite(method: string, args: any = {}) {
  if (method === 'ury.ury.api.service_requests.acknowledge_service_request') {
    acknowledged.push(args.name);
    open = open.map(row => row.name === args.name ? { ...row, status: 'Acknowledged' } : row);
    return { message: { name: args.name, status: 'Acknowledged' } };
  }
  throw Error(`Unexpected write ${method}`);
}

beforeAll(async () => {
  if (existsSync(resolve('pos/src/components/ServiceRequestAlert.tsx'))) {
    const modulePath = '../src/components/ServiceRequestAlert';
    Alert = (await import(/* @vite-ignore */ modulePath)).default;
  }
});

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout', 'Date'] });
  vi.setSystemTime(NOW);
  plays.count = 0;
  vi.stubGlobal('Audio', class { play() { plays.count += 1; return Promise.resolve(); } });
  socket.handlers.clear();
  socket.emitted.length = 0;
  await initI18n('en');
  open = [{ name: 'SR-1', table: 'T-2', request_type: 'Assistance', status: 'Open', requested_at: '2026-11-20 14:56:00' }];
  settings = { sound: 1, escalate_minutes: 3 };
  acknowledged = [];
  vi.spyOn(call, 'get').mockImplementation(serverRead);
  vi.spyOn(call, 'post').mockImplementation(serverWrite);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function render() {
  await act(async () => { root.render(<Alert />); });
  await act(async () => { await Promise.resolve(); });
}
const banner = () => container.querySelector('[data-testid="service-request-alert"]');
async function click(label: string) {
  const button = Array.from(container.querySelectorAll('button')).find(b => b.textContent?.includes(label));
  expect(button, `Action ${label} must be available`).toBeDefined();
  await act(async () => button!.click());
  await act(async () => { await Promise.resolve(); });
}

describe('service request staff alert', () => {
  it('shows a full-width alert with the table, the request type and minutes waiting', async () => {
    await render();
    const alert = banner();
    expect(alert, 'No staff alert renders for an open guest request').not.toBeNull();
    expect(alert!.getAttribute('role')).toBe('alert');
    expect(alert!.className).toContain('w-full');
    expect(alert!.textContent).toContain('T-2');
    expect(alert!.textContent).toContain('Call waiter');
    expect(alert!.textContent).toContain('2 min');
  });

  it('clears when the request is acknowledged', async () => {
    await render();
    expect(banner()).not.toBeNull();
    await click('Acknowledge');
    expect(acknowledged).toEqual(['SR-1']);
    expect(banner()).toBeNull();
  });

  it('turns to the escalation colour after the profile minutes', async () => {
    open = [{ ...open[0], requested_at: '2026-11-20 14:54:00' }];
    await render();
    expect(banner()!.querySelector('[data-escalated="true"]'), 'A 4-minute-old request is not escalated at N=3').not.toBeNull();
    settings = { sound: 1, escalate_minutes: 5 };
    await act(async () => root.unmount());
    root = createRoot(container);
    await render();
    expect(banner()!.querySelector('[data-escalated="true"]')).toBeNull();
  });

  it('repeats the chime while a request is open, and not when sound is off on the profile', async () => {
    await render();
    const first = plays.count;
    expect(first).toBeGreaterThan(0);
    await act(async () => { vi.advanceTimersByTime(10_000); });
    expect(plays.count).toBeGreaterThan(first);

    await act(async () => root.unmount());
    root = createRoot(container);
    plays.count = 0;
    settings = { sound: 0, escalate_minutes: 3 };
    await render();
    await act(async () => { vi.advanceTimersByTime(30_000); });
    expect(plays.count).toBe(0);
  });

  it('subscribes to realtime and shows a new request at once, without waiting for the poll', async () => {
    open = [];
    await render();
    expect(banner()).toBeNull();
    expect(socket.emitted).toContainEqual(['doctype_subscribe', 'URY Service Request']);
    open = [{ name: 'SR-9', table: 'T-5', request_type: 'Bill', status: 'Open', requested_at: '2026-11-20 14:58:00' }];
    await act(async () => {
      socket.fire('ury_service_request', { name: 'SR-9', table: 'T-5', branch: 'Branch A', request_type: 'Bill', status: 'Open' });
    });
    await act(async () => { await Promise.resolve(); });
    expect(banner()?.textContent).toContain('T-5');
  });

  it('ignores realtime events for another branch', async () => {
    open = [];
    await render();
    const reads = vi.mocked(call.get).mock.calls.length;
    await act(async () => {
      socket.fire('ury_service_request', { name: 'SR-X', table: 'X-1', branch: 'Branch B', request_type: 'Assistance', status: 'Open' });
    });
    expect(vi.mocked(call.get).mock.calls.length).toBe(reads);
  });

  it('keeps polling as the fallback when realtime is down', async () => {
    open = [];
    await render();
    open = [{ name: 'SR-2', table: 'T-3', request_type: 'Assistance', status: 'Open', requested_at: '2026-11-20 14:58:00' }];
    await act(async () => { vi.advanceTimersByTime(15_000); });
    await act(async () => { await Promise.resolve(); });
    expect(banner()?.textContent).toContain('T-3');
  });

  it('exposes the open tables so the floor plan can pulse their cards', async () => {
    expect(existsSync(resolve('pos/src/components/ServiceRequestAlert.tsx')), 'No staff alert module').toBe(true);
    const modulePath = '../src/components/ServiceRequestAlert';
    const mod = await import(/* @vite-ignore */ modulePath);
    expect(typeof mod.useOpenServiceRequestTables).toBe('function');
  });
});

describe('table card pulse', () => {
  it('pulses a table card that has an open guest request', async () => {
    const { default: TableCard } = await import('../src/components/TableCard');
    const props = {
      table: { name: 'T-2', occupied: 1, latest_invoice_time: null, is_take_away: 0, restaurant_room: 'Grill Room', table_shape: 'Square' },
      menuOpen: false, onMenuOpenChange: () => {}, onMerge: () => {}, onUnmerge: () => {}, onNavigate: () => {},
      onPreview: () => {}, onPrint: () => {}, isPrinting: false,
    } as any;
    await act(async () => { root.render(<TableCard {...props} serviceRequest={{ request_type: 'Assistance', escalated: false }} />); });
    const card = container.querySelector('[data-service-request]');
    expect(card, 'Table card shows no call-waiter state').not.toBeNull();
    expect(card!.className).toContain('animate-pulse');
  });
});
