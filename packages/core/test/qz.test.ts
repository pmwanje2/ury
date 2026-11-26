// @vitest-environment jsdom
import { generateKeyPairSync, sign, verify } from 'node:crypto';
import { afterEach, expect, it, vi } from 'vitest';
import axios from 'axios';
import { disconnectQzPrinter, loadQzPrinter, printWithQz } from '../src/print/qz';

const post = vi.hoisted(() => vi.fn());
vi.mock('../src/frappe/client', () => ({ call: { post } }));

const src = '/assets/frappe/node_modules/qz-tray/qz-tray.js';
const scripts = () => document.head.querySelectorAll<HTMLScriptElement>(`script[src="${src}"]`);

afterEach(() => {
  Reflect.deleteProperty(window, 'qz');
  scripts().forEach(script => script.remove());
  vi.restoreAllMocks();
});

it('loads one Frappe bridge, connects, signs and prints to the default printer', async () => {
  const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  post.mockImplementation(async (_method, { toSign }) => ({
    message: sign('RSA-SHA512', Buffer.from(toSign), privateKey).toString('base64'),
  }));
  vi.spyOn(axios, 'get').mockResolvedValue({ data: { message: '-----BEGIN CERTIFICATE-----\ndGVzdA==\n-----END CERTIFICATE-----' } });
  const bridge = {
    security: { setCertificatePromise: vi.fn(), setSignatureAlgorithm: vi.fn(), setSignaturePromise: vi.fn() },
    websocket: { isActive: vi.fn(() => true), connect: vi.fn(), disconnect: vi.fn() },
    printers: { getDefault: vi.fn(async () => 'Receipt') },
    configs: { create: vi.fn(() => ({ printer: 'Receipt' })) },
    print: vi.fn(),
  };
  disconnectQzPrinter();
  expect(scripts()).toHaveLength(0);
  bridge.websocket.isActive.mockReturnValueOnce(false);
  const connect = loadQzPrinter('localhost');
  const print = printWithQz('localhost', '<p>receipt</p>');
  expect(scripts()).toHaveLength(1);
  Object.assign(window, { qz: bridge });
  scripts()[0].dispatchEvent(new Event('load'));
  await Promise.all([connect, print]);
  expect(bridge.websocket.connect).toHaveBeenCalledWith({ host: 'localhost', usingSecure: true });
  await expect(new Promise(bridge.security.setCertificatePromise.mock.calls[0][0])).resolves.toContain('BEGIN CERTIFICATE');
  expect(axios.get).toHaveBeenCalledWith('/api/method/ury.ury.api.ury_print.qz_certificate');
  expect(bridge.security.setSignatureAlgorithm).toHaveBeenCalledWith('SHA512');
  const callback = bridge.security.setSignaturePromise.mock.calls[0][0];
  const sig = await new Promise<string>(callback('challenge'));
  expect(post).toHaveBeenCalledWith('ury.ury.api.ury_print.signature_promise', { toSign: 'challenge' });
  expect(verify('RSA-SHA512', Buffer.from('challenge'), publicKey, Buffer.from(sig, 'base64'))).toBe(true);
  expect(bridge.configs.create).toHaveBeenCalledWith('Receipt');
  expect(bridge.print).toHaveBeenCalledWith({ printer: 'Receipt' }, [{ type: 'html', format: 'plain', data: '<p>receipt</p>' }]);
  disconnectQzPrinter();
  expect(bridge.websocket.disconnect).toHaveBeenCalledOnce();
});

it('reports failed or empty script loads and permits retry', async () => {
  for (const event of ['error', 'load']) {
    const attempt = loadQzPrinter('localhost');
    expect(scripts()).toHaveLength(1);
    scripts()[0].dispatchEvent(new Event(event));
    await expect(attempt).rejects.toThrow(/QZ_UNAVAILABLE/);
    expect(scripts()).toHaveLength(0);
  }
});
