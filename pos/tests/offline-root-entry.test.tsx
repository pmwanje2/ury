// Drill 3 at ace37466: a signed-in opted-in till opens the cached PWA root
// /pos during an outage and is sent to /pos/orders, which shows
// Access Denied / Failed to fetch POS invoices and hides the till.
/**
 * @vitest-environment jsdom
 * @vitest-environment-options {"url":"http://127.0.0.1:18765/pos/"}
 */
import http from 'node:http';
import type { AxiosInstance } from 'axios';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { IDBFactory } from 'fake-indexeddb';
import { afterAll, afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest';
import { transport } from '../../packages/core/src/frappe/client';
import { initI18n } from '../src/i18n';
import { testLocks } from './offline-locks';
import { usePOSStore } from '../src/store/pos-store';
import { useRootStore } from '../src/store/root-store';
import App from '../src/App';

const axios = transport as AxiosInstance;
const cashier = 'alice@example.test';
const profile = {
  name: 'Salama POS',
  company: 'Salama',
  currency: 'UGX',
  ury_offline_capture: 1,
  branch: '',
  paid_limit: 20,
  role_allowed_for_billing: [{ role: 'URY Cashier' }],
};
const gatewayStatuses = [502, 503, 504, 520, 521, 522, 523, 524] as const;

const origin = 'http://127.0.0.1:18765';
let server: http.Server;
let mode: 'network' | 'gateway' | 'denied' = 'gateway';
let gatewayStatus: number = 502;
let deniedStatus = 401;
let hits: string[] = [];
let root: Root;
let container: HTMLDivElement;
let savedAdapter: AxiosInstance['defaults']['adapter'];
let savedBaseURL: string | undefined;

function json(response: http.ServerResponse, status: number, body: unknown) {
  response.statusCode = status;
  response.setHeader('content-type', 'application/json');
  response.end(JSON.stringify(body));
}

beforeAll(async () => {
  server = http.createServer((request, response) => {
    hits.push(mode + ' ' + (request.url || ''));
    if (mode === 'network') {
      request.socket.destroy();
      return;
    }
    if (mode === 'gateway') {
      response.statusCode = gatewayStatus;
      response.setHeader('content-type', 'text/html');
      response.end('<html><h1>' + gatewayStatus + ' Bad Gateway</h1></html>');
      return;
    }
    const url = request.url || '';
    if (url.includes('get_logged_user')) {
      json(response, 200, { message: cashier });
      return;
    }
    if (url.includes('check_opening_entry')) {
      json(response, 200, { message: [{ name: 'OPEN-1', company: 'Salama', pos_profile: profile.name }] });
      return;
    }
    if (url.includes('get_checklist')) {
      json(response, 200, { message: { items: [], log_name: 'LOG-1', log_status: 'Complete' } });
      return;
    }
    if (url.includes('getPosInvoice')) {
      json(response, deniedStatus, {
        exc_type: deniedStatus === 401 ? 'AuthenticationError' : 'PermissionError',
        message: 'Not permitted',
      });
      return;
    }
    json(response, 200, { message: [] });
  });
  await new Promise<void>((done) => server.listen(18765, '127.0.0.1', done));
  savedAdapter = axios.defaults.adapter;
  savedBaseURL = axios.defaults.baseURL;
  axios.defaults.adapter = 'fetch';
  axios.defaults.baseURL = origin;
});

afterAll(async () => {
  axios.defaults.adapter = savedAdapter;
  axios.defaults.baseURL = savedBaseURL;
  await new Promise<void>((done) => server.close(() => done()));
});

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('indexedDB', new IDBFactory());
  hits = [];
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: {
      controller: {
        scriptURL: origin + '/sw.js',
        postMessage(_message: unknown, transfer?: Transferable[]) {
          const port = transfer?.[0] as MessagePort | undefined;
          queueMicrotask(() => port?.postMessage({ ok: true }));
        },
      },
    },
  });
  Object.defineProperty(navigator, 'locks', { configurable: true, value: testLocks() });
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 });
  (window as unknown as { frappe: unknown }).frappe = {
    boot: { user: { roles: ['URY Cashier'], full_name: 'Alice' } },
  };
  document.cookie = 'user_id=' + encodeURIComponent(cashier) + '; Path=/';
  sessionStorage.clear();
  localStorage.clear();
  sessionStorage.setItem('posProfile:' + cashier, JSON.stringify(profile));
  sessionStorage.setItem('menuCategories', JSON.stringify([]));
  sessionStorage.setItem('payment_modes:' + cashier, JSON.stringify(['Cash']));
  sessionStorage.setItem('ury_offline_opening:' + cashier, JSON.stringify({
    name: 'OPEN-1', company: 'Salama', pos_profile: profile.name,
  }));
  localStorage.setItem('currencySymbol', 'USh');
  useRootStore.setState({
    user: null, isLoading: false, error: null, posProfile: null, hasAccess: false,
    allowedRoles: [], orders: [], orderLoading: false,
  });
  usePOSStore.setState({ posProfile: null, isInitializing: true, error: null, paymentModes: [], loading: false } as never);
  const { OfflineOutbox } = await import('../src/offline/outbox');
  const queue = new OfflineOutbox({
    profile, user: { name: cashier, roles: ['URY Cashier'] }, print: async () => true,
  });
  await queue.capture({
    pos_profile: profile.name, customer: 'Walk In',
    items: [{ item: 'Tea', item_name: 'Tea', qty: 1, rate: 1000 }],
    no_of_pax: 1, invoice: null, last_invoice: null, order_type: 'Take Away',
  }, 1000);
  await initI18n('en');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  document.cookie = 'user_id=; Max-Age=0; Path=/';
  sessionStorage.clear();
  localStorage.clear();
  window.history.replaceState({}, '', '/');
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function openRoot(path: string, settled: (text: string) => boolean) {
  window.history.replaceState({}, '', path);
  await act(async () => { root.render(<App />); });
  await vi.waitFor(() => {
    const text = container.textContent || '';
    expect(settled(text), text + ' @ ' + window.location.pathname).toBe(true);
  }, { timeout: 8000 });
}

test.each(['network', ...gatewayStatuses] as const)(
  'opening /pos during %s lands on the till with navigation and pending controls',
  async (outage) => {
    mode = outage === 'network' ? 'network' : 'gateway';
    if (outage !== 'network') gatewayStatus = outage;
    await openRoot('/pos/', (text) => text.includes('Access Denied') || (window.location.pathname === '/pos/pos' && text.includes('1 pending')));
    expect(hits, hits.join('\n')).toEqual(expect.arrayContaining([expect.stringContaining('get_logged_user')]));
    const text = container.textContent || '';
    expect(text, text).not.toContain('Access Denied');
    expect(text, text).not.toContain('Failed to fetch POS invoices');
    expect(window.location.pathname).toBe('/pos/pos');
    expect(container.querySelector('a[href="/pos/pos"]')?.textContent).toContain('POS');
    expect(container.querySelector('a[href="/pos/tables"]')?.textContent).toContain('Tables');
    expect(container.querySelector('[data-testid="offline-banner"]')).not.toBeNull();
    expect(text).toContain('1 pending');
    expect(container.querySelector('[data-testid="settle-cash"]')).not.toBeNull();
  },
);

test.each(['network', 502] as const)(
  'the orders page during %s shows an offline notice instead of Access Denied',
  async (outage) => {
    mode = outage === 'network' ? 'network' : 'gateway';
    if (outage !== 'network') gatewayStatus = outage;
    await openRoot('/pos/orders', (text) => text.includes('Access Denied') || text.includes('Orders are unavailable while the link is down.'));
    expect(hits, hits.join('\n')).toEqual(expect.arrayContaining([expect.stringContaining('getPosInvoice')]));
    const text = container.textContent || '';
    expect(window.location.pathname).toBe('/pos/orders');
    expect(text, text).not.toContain('Access Denied');
    expect(text).toContain('Orders are unavailable while the link is down.');
    expect(container.querySelector('a[href="/pos/pos"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="offline-banner"]')).not.toBeNull();
  },
);

test.each([401, 403])('HTTP %i on the invoice list is still Access Denied', async (status) => {
  mode = 'denied';
  deniedStatus = status;
  await openRoot('/pos/orders', (text) => text.includes('Access Denied') || text.includes('Orders are unavailable while the link is down.'));
  expect(hits, hits.join('\n')).toEqual(expect.arrayContaining([expect.stringContaining('denied')]));
  expect(hits, hits.join('\n')).toEqual(expect.arrayContaining([expect.stringContaining('getPosInvoice')]));
  const text = container.textContent || '';
  expect(text).toContain('Access Denied');
  expect(text).toContain('Failed to fetch POS invoices');
  expect(text).not.toContain('Orders are unavailable while the link is down.');
});
