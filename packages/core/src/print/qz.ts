import { loadQz } from './qz-runtime';
import { connectQz, withQzDeadline } from '../../../../ury/public/js/qz-signing.js';
import axios from 'axios';
import { call } from '../frappe/client';
import { runSignedQz } from './qz-signing';

export const ESC_POS_DRAWER_KICK_PIN_2 = '1b700019fa';
export const ESC_POS_DRAWER_KICK_PIN_5 = '1b700119fa';

export type DrawerPin = 2 | 5;
export type QzPrinterTarget = string | { host: string; port: number };

export interface KickDrawerQzResult {
  status: 'Sent';
  hex: string;
  printer: QzPrinterTarget;
}

export function resolveDrawerKickHex(opts?: {
  pin?: DrawerPin | number | string | null;
  hex?: string | null;
}): string {
  const rawHex = opts?.hex;
  if (rawHex !== undefined && rawHex !== null && String(rawHex).trim() !== '') {
    let cleaned = String(rawHex).trim().replace(/[\s:,-]+/g, '').toLowerCase();
    if (cleaned.startsWith('0x')) {
      cleaned = cleaned.slice(2);
    }
    if (!cleaned || cleaned.length % 2 !== 0 || cleaned.length > 64 || !/^[0-9a-f]+$/.test(cleaned)) {
      throw new Error('Drawer kick hex command must be an even-length hexadecimal string (2-64 chars).');
    }
    return cleaned;
  }

  const rawPin = opts?.pin;
  const pin = rawPin === undefined || rawPin === null || String(rawPin).trim() === '' ? 2 : Number(rawPin);
  if (pin === 2) return ESC_POS_DRAWER_KICK_PIN_2;
  if (pin === 5) return ESC_POS_DRAWER_KICK_PIN_5;
  throw new Error(`Unsupported cash drawer pin "${String(rawPin)}". Use Pin 2 or Pin 5.`);
}

export async function loadQzPrinter(host: string, signal?: AbortSignal): Promise<void> {
  const qz = await loadQz();
  await connectQz(qz, async requestSignal => {
    const endpoint = '/api/method/ury.ury.api.ury_print.qz_certificate';
    const { data } = signal
      ? await axios.get(endpoint, { signal: requestSignal })
      : await axios.get(endpoint);
    return data.message;
  }, host, signal);
}

export function disconnectQzPrinter(): void {
  const qz = globalThis.window?.qz;
  if (qz?.websocket.isActive()) qz.websocket.disconnect();
}

export async function printWithQz(host: string, htmlToPrint: string, queue?: string | null): Promise<void> {
  return withQzDeadline(async (signal, startPrinting) => {
    const qz = await loadQz();
    return runSignedQz(qz, call.post, async () => {
      if (!qz.websocket.isActive()) await loadQzPrinter(host, signal);
      signal.throwIfAborted();
      startPrinting();
      const printer = queue?.trim() || await qz.printers.getDefault();
      signal.throwIfAborted();
      await qz.print(qz.configs.create(printer), [{ type: 'html', format: 'plain', data: htmlToPrint }]);
    }, signal);
  });
}

export async function kickDrawerQz(
  host: string,
  printerName?: QzPrinterTarget | null,
  hexCommand?: string | null,
  pin?: DrawerPin,
): Promise<KickDrawerQzResult> {
  const hex = resolveDrawerKickHex({ hex: hexCommand, pin });
  const qz = await loadQz();

  return runSignedQz(qz, call.post, async (): Promise<KickDrawerQzResult> => {
    if (!qz.websocket.isActive()) await loadQzPrinter(host);
    const target = printerName || (await qz.printers.getDefault());
    const config = qz.configs.create(target);
    await qz.print(config, [{ type: 'raw', format: 'command', flavor: 'hex', data: hex }]);
    return { status: 'Sent', hex, printer: target };
  });
}
