// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { constants, createContext, runInContext } from 'node:vm';
import { afterEach, expect, it, vi } from 'vitest';
import { createStationPrinter } from '../../../mosaic/src/components/station-print.js';

const assets = resolve(import.meta.dirname, '../../../ury/public/js');
const release = process.env.QZ_TRAY_TEST_CLIENT;
const qzSource = readFileSync(release || resolve(assets, 'qz-tray.js'), 'utf8');
const signMethod = 'ury.ury.api.ury_print.signature_promise';
const markPrinted = 'ury.ury.api.ury_print.qz_print_update';
const denied = { httpStatus: 403, message: 'Internal permission traceback' };

type Bridge = NonNullable<Window['qz']> & { version: string };
const bridges: Bridge[] = [];

function printerWire({ failPrimary = false } = {}) {
  const requests: string[] = [];
  class Socket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    readyState = Socket.CONNECTING;
    onopen?: () => void;
    onmessage?: (event: { data: string }) => void;
    onclose?: () => void;

    constructor() {
      queueMicrotask(() => {
        this.readyState = Socket.OPEN;
        this.onopen?.();
      });
    }

    send(data: string) {
      const { call, uid } = JSON.parse(data);
      requests.push(call);
      const result = call === 'getVersion' ? '2.2.3' : call === 'printers.getDefault' ? 'Receipt' : null;
      const error = failPrimary && call === 'print' ? 'Cannot find printer' : undefined;
      if (error) failPrimary = false;
      queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ uid, result, error }) }));
    }

    close() {
      this.readyState = Socket.CLOSING;
      queueMicrotask(() => {
        this.readyState = Socket.CLOSED;
        this.onclose?.();
      });
    }
  }

  const browser: { qz?: Bridge } = {};
  const sandbox = createContext({
    window: browser, WebSocket: Socket,
    console: { log() {}, info() {}, warn() {}, error() {} },
    setTimeout, clearTimeout, setInterval, clearInterval,
  });
  runInContext(qzSource, sandbox);
  const qz = browser.qz!;
  expect(qz.version).toBe(release ? '2.2.3' : '2.2.3-SNAPSHOT');
  const connect = qz.websocket.connect.bind(qz.websocket);
  qz.websocket.connect = (options) => connect({ ...options, keepAlive: 0 } as never);
  bridges.push(qz);

  function runAsset(name: string) {
    const script = readFileSync(resolve(assets, name), 'utf8')
      .replace(/import\((['"])\/assets\/ury\/js\/([^'"]+)\1\)/g,
        (_, _quote, module) => `import(${JSON.stringify(pathToFileURL(resolve(assets, module)).href)})`);
    runInContext(script, sandbox, { importModuleDynamically: constants.USE_MAIN_CONTEXT_DEFAULT_LOADER });
  }
  return { qz, sandbox, requests, runAsset };
}

afterEach(async () => {
  vi.useRealTimers();
  for (const bridge of bridges.splice(0)) {
    if (bridge.websocket.isActive()) await bridge.websocket.disconnect();
  }
  localStorage.clear();
});

it('Desk unfreezes after an approved printer lookup followed by a refused print', async () => {
  const { qz, sandbox, requests, runAsset } = printerWire();
  qz.security.setCertificatePromise((resolve) => resolve('certificate'));
  await qz.websocket.connect({ host: 'localhost', usingSecure: false });

  const freeze = vi.fn();
  const unfreeze = vi.fn();
  const msgprint = vi.fn();
  const methods: string[] = [];
  let signRequests = 0;
  let denyPrint = true;
  let handlers: { print: (frm: object) => unknown };
  const frm = { doc: { name: 'INV-1', pos_profile: 'Till' }, set_value: vi.fn(), reload_doc: vi.fn() };
  Object.assign(sandbox, {
    __: (message: string) => message,
    fetch: async () => ({ ok: true, text: async () => 'certificate' }),
    cur_frm: frm,
    frappe: {
      require: (path: string, done: () => void) => {
        if (path.endsWith('/sign-message.js')) runAsset('sign-message.js');
        done();
      },
      ui: { form: { on: (_name: string, value: typeof handlers) => { handlers = value; } } },
      db: { get_doc: async (doctype: string) => doctype === 'POS Invoice' ? { invoice_printed: 0 }
        : { qz_print: 1, qz_host: 'localhost', printer_settings: [] } },
      dom: { freeze, unfreeze },
      msgprint,
      show_alert: vi.fn(),
      throw: (error: { message: string }) => { throw new Error(error.message); },
      call: ({
        method, callback, error,
      }: { method: string; callback?: (data: object) => void; error?: (reason: unknown) => void }) => {
        methods.push(method);
        if (method === signMethod && ++signRequests === 2 && denyPrint) {
          if (error) return Promise.resolve().then(() => error(denied));
          return Promise.reject(denied);
        }
        const message = method === signMethod ? 'approved signature'
          : method.endsWith('qz_certificate') ? '-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----'
            : method.endsWith('get_html_and_style') ? { html: '<p>receipt</p>' } : { status: 'Success' };
        return Promise.resolve({ message }).then((response) => { callback?.(response); return response; });
      },
    },
  });
  runAsset('pos_print.js');
  await Promise.resolve();
  void handlers!.print(frm);
  await vi.waitFor(() => expect(signRequests).toBe(2), { timeout: 250 });
  expect(freeze).toHaveBeenCalledOnce();
  await vi.waitFor(() => expect(unfreeze).toHaveBeenCalledOnce(), { timeout: 250 });
  expect(msgprint).toHaveBeenCalledWith(expect.objectContaining({ message: expect.stringMatching(/printer signing was refused/i) }));
  expect(requests.filter((call) => call === 'print')).toHaveLength(0);
  expect(methods).not.toContain(markPrinted);

  denyPrint = false;
  await handlers!.print(frm);
  expect(unfreeze).toHaveBeenCalledTimes(2);
  expect(requests.filter((call) => call === 'print')).toHaveLength(1);
  expect(methods.filter((method) => method === markPrinted)).toHaveLength(1);
});

for (const failPrimary of [false, true]) {
  it(`Mosaic clears and retries a ticket after ${failPrimary ? 'backup' : 'primary'} signing is refused`, async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const { qz, requests } = printerWire({ failPrimary });
    let allowSignatures = false;
    let signRequests = 0;
    const onChange = vi.fn();
    const printer = createStationPrinter({
      production: 'Kitchen',
      user: 'waiter@example.test',
      qz,
      storage: localStorage,
      onChange,
      serverSettings: {
        enabled: true, enabledAt: '2026-11-25T12:00:00Z',
        printer: 'Primary', backupPrinter: 'Backup', printFormat: 'Kitchen Ticket',
      },
      call: {
        get: async (method: string) => ({ message: method.endsWith('qz_certificate')
          ? '-----BEGIN CERTIFICATE-----' : { html: '<p>ticket</p>' } }),
        post: async (method: string) => {
          if (method === signMethod) {
            signRequests += 1;
            if (!allowSignatures && (!failPrimary || signRequests > 1)) throw denied;
          }
          return { message: 'approved signature' };
        },
      },
    });
    let result;
    const attempt = printer.process({
      name: 'KOT-1', production: 'Kitchen', creation: '2026-11-25T12:01:00Z',
    }).then((outcome: unknown) => { result = outcome; });

    await vi.advanceTimersByTimeAsync(250);
    const promptResult = result;
    if (!promptResult) await vi.advanceTimersByTimeAsync(8000);
    expect(promptResult, `ticket state after ${signRequests} signing requests`).toEqual({
      printed: false, code: 'QZ_SIGNING_REFUSED',
    });
    await attempt;
    expect(signRequests).toBe(failPrimary ? 2 : 1);
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ failed: ['KOT-1'], pending: [] }));
    expect(requests.filter((call) => call === 'print')).toHaveLength(failPrimary ? 1 : 0);

    allowSignatures = true;
    const retry = printer.retry();
    await vi.advanceTimersByTimeAsync(0);
    expect(await retry).toEqual([{ printed: true }]);
    expect(printer.failed()).toEqual([]);
    expect(requests.filter((call) => call === 'print')).toHaveLength(failPrimary ? 2 : 1);
  });
}
