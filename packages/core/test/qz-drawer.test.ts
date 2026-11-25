// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import axios from 'axios';
import * as qzModule from '../src/print/qz';

const post = vi.hoisted(() => vi.fn());
vi.mock('../src/frappe/client', () => ({ call: { post } }));

afterEach(() => {
  Reflect.deleteProperty(window, 'qz');
  vi.restoreAllMocks();
});

describe('QZ cash drawer ESC/POS kick', () => {
  it('exports standard Pin 2 and Pin 5 ESC/POS hex pulses and validates custom hex', () => {
    expect((qzModule as Record<string, unknown>).ESC_POS_DRAWER_KICK_PIN_2).toBe('1b700019fa');
    expect((qzModule as Record<string, unknown>).ESC_POS_DRAWER_KICK_PIN_5).toBe('1b700119fa');

    const resolveHex = (qzModule as Record<string, unknown>).resolveDrawerKickHex as (
      opts?: { pin?: number; hex?: string }
    ) => string;
    expect(typeof resolveHex).toBe('function');
    expect(resolveHex({ pin: 2 })).toBe('1b700019fa');
    expect(resolveHex({ pin: 5 })).toBe('1b700119fa');
    expect(resolveHex({ hex: '1B 70 01 19 FA' })).toBe('1b700119fa');
    expect(() => resolveHex({ pin: 3 })).toThrow();
    expect(() => resolveHex({ hex: 'not-hex' })).toThrow();
  });

  it('sends raw hex ESC/POS pulse through QZ Tray to named or default printer', async () => {
    const kickDrawerQz = (qzModule as Record<string, unknown>).kickDrawerQz as (
      host: string,
      printerName?: string | null,
      hexCommand?: string | null,
      pin?: 2 | 5,
    ) => Promise<{ status: string; hex: string }>;
    expect(typeof kickDrawerQz).toBe('function');

    post.mockResolvedValue({ message: 'site-signature' });
    vi.spyOn(axios, 'get').mockResolvedValue({ data: 'cert' });

    const bridge = {
      security: { setCertificatePromise: vi.fn(), setSignatureAlgorithm: vi.fn(), setSignaturePromise: vi.fn() },
      websocket: { isActive: vi.fn(() => true), connect: vi.fn(), disconnect: vi.fn() },
      printers: { getDefault: vi.fn(async () => 'Default Thermal') },
      configs: { create: vi.fn((p: unknown) => ({ printer: p })) },
      print: vi.fn(async () => undefined),
    };
    Object.assign(window, { qz: bridge });

    const resPin5 = await kickDrawerQz('localhost', 'Till-Drawer-2', undefined, 5);
    expect(resPin5.status).toBe('Sent');
    expect(resPin5.hex).toBe('1b700119fa');
    expect(bridge.configs.create).toHaveBeenCalledWith('Till-Drawer-2');
    expect(bridge.print).toHaveBeenCalledWith(
      { printer: 'Till-Drawer-2' },
      [{ type: 'raw', format: 'command', flavor: 'hex', data: '1b700119fa' }],
    );

    const resDefault = await kickDrawerQz('localhost');
    expect(resDefault.hex).toBe('1b700019fa');
    expect(bridge.configs.create).toHaveBeenCalledWith('Default Thermal');
    const signature = bridge.security.setSignaturePromise.mock.calls[0][0];
    await expect(new Promise(signature('drawer pulse'))).resolves.toBe('site-signature');
    expect(post).toHaveBeenCalledWith('ury.ury.api.ury_print.signature_promise', { toSign: 'drawer pulse' });
  });
});
