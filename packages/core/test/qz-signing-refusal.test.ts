// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { runInNewContext } from 'node:vm';
import { createHash } from 'node:crypto';
import { afterEach, expect, it, vi } from 'vitest';
import { kickDrawerQz, printWithQz } from '../src/print/qz';
import { printWithQz as legacyPrintWithQz } from '../../../urypos/src/stores/utils/PrintWithQz.js';

const post = vi.hoisted(() => vi.fn());
vi.mock('../src/frappe/client', () => ({ call: { post } }));
vi.mock('../../../urypos/src/stores/frappeSdk.js', () => ({ default: { call: () => ({ post }) } }));
vi.mock('axios', () => ({ default: { get: async () => ({ data: 'test certificate' }) } }));

// The repository ships the upstream 2.2.3 snapshot for Desk. Set this path to
// Frappe's installed qz-tray.js to exercise the pinned 2.2.3 release itself.
const release = process.env.QZ_TRAY_TEST_CLIENT;
const source = readFileSync(
  release || resolve(import.meta.dirname, '../../../ury/public/js/qz-tray.js'), 'utf8',
);
if (release) {
  expect(createHash('sha256').update(source).digest('hex'))
    .toBe('967d43e6b5c203bac8e5874328609f46ee1afbf926ab081db04de03f73642e0a');
}

let bridge: (NonNullable<Window['qz']> & { version: string }) | undefined;

async function connectPrinter() {
  const messages: Array<{ call?: string; uid: string; signature?: string }> = [];

  // Only the wire is simulated. QZ controls hashing, signing and pending calls.
  class PrinterSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    readyState = PrinterSocket.CONNECTING;
    onopen?: () => void;
    onclose?: () => void;
    onmessage?: (event: { data: string }) => void;

    constructor() {
      queueMicrotask(() => {
        this.readyState = PrinterSocket.OPEN;
        this.onopen?.();
      });
    }

    send(data: string) {
      const message: (typeof messages)[number] = JSON.parse(data);
      messages.push(message);
      const result = message.call === 'getVersion' ? '2.2.3'
        : message.call === 'printers.getDefault' ? 'Receipt'
          : null;
      queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ uid: message.uid, result }) }));
    }

    close() {
      this.readyState = PrinterSocket.CLOSING;
      queueMicrotask(() => {
        this.readyState = PrinterSocket.CLOSED;
        this.onclose?.();
      });
    }
  }

  const browser: { qz?: typeof bridge } = {};
  runInNewContext(source, {
    window: browser,
    WebSocket: PrinterSocket,
    console: { log() {}, info() {}, warn() {}, error() {} },
    setTimeout, clearTimeout, setInterval, clearInterval,
  });
  bridge = browser.qz!;
  expect(bridge.version).toBe(release ? '2.2.3' : '2.2.3-SNAPSHOT');
  bridge.security.setCertificatePromise((resolve) => resolve('test certificate'));
  Object.assign(window, { qz: bridge });
  await bridge.websocket.connect({ host: 'localhost', usingSecure: false, keepAlive: 0 } as never);
  return messages;
}

afterEach(async () => {
  if (bridge?.websocket.isActive()) await bridge.websocket.disconnect();
  bridge = undefined;
  Reflect.deleteProperty(window, 'qz');
  post.mockReset();
});

async function expectPromptRefusal(operation: () => Promise<unknown>) {
  let busy = true;
  const attempt = operation()
    .then(() => 'unexpected success', (error: Error) => error.message)
    .finally(() => { busy = false; });
  let timeout: ReturnType<typeof setTimeout> | undefined;
  try {
    const result = await Promise.race([
      attempt,
      new Promise<string>((resolve) => {
        timeout = setTimeout(() => resolve('still waiting for QZ'), 250);
      }),
    ]);
    expect(result).toMatch(/printer signing (?:was )?refused/i);
    expect(busy).toBe(false);
  } finally {
    clearTimeout(timeout);
  }
}

const denied = () => Promise.reject({ httpStatus: 403, message: 'Internal permission traceback' });

for (const [client, print] of [['POS and Serve', printWithQz], ['legacy POS', legacyPrintWithQz]] as const) {
  it(`${client} clears the print action after the print signature is refused`, async () => {
    const messages = await connectPrinter();
    post.mockResolvedValueOnce({ message: 'permitted lookup' }).mockImplementationOnce(denied);
    await expectPromptRefusal(() => print('localhost', '<p>receipt</p>'));
    expect(post).toHaveBeenCalledTimes(2);
    expect(messages.filter(({ call }) => call === 'printers.getDefault')).toHaveLength(1);
    expect(messages.some(({ call }) => call === 'print')).toBe(false);
  });
}

it('rejects a cash drawer pulse when printing is revoked', async () => {
  const messages = await connectPrinter();
  post.mockImplementationOnce(denied);
  await expectPromptRefusal(() => kickDrawerQz('localhost', 'Drawer'));
  expect(post).toHaveBeenCalledOnce();
  expect(messages.some(({ call }) => call === 'print')).toBe(false);
});
