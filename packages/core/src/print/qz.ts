import { loadQz } from './qz-runtime';
import axios from 'axios';
import { call } from '../frappe/client';
import { configureQzSignature } from './qz-signing';

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

export async function loadQzPrinter(host: string): Promise<void> {
  const qz = await loadQz();
  qz.security.setCertificatePromise((resolve: (data: string) => void, reject: (err?: string) => void) => {
    axios.get('/assets/ury/files/cert.pem')
      .then(({ data }) => resolve(data))
      .catch((err) => reject('Error fetching certificate: ' + String(err)));
  });
  if (!qz.websocket.isActive()) {
    await qz.websocket.connect({ host, usingSecure: false });
  }
}

export function disconnectQzPrinter(): void {
  const qz = globalThis.window?.qz;
  if (qz?.websocket.isActive()) qz.websocket.disconnect();
}

export async function printWithQz(host: string, htmlToPrint: string): Promise<void> {
  const qz = await loadQz();

  configureQzSignature(qz, call.post);

  const printing = async () => {
    const printer = await qz.printers.getDefault();
    const data = [{ type: 'html', format: 'plain', data: htmlToPrint }];
    const config = qz.configs.create(printer);
    await qz.print(config, data);
  };

  if (qz.websocket.isActive()) {
    await printing();
  } else {
    await loadQzPrinter(host);
    await printing();
  }
}

export async function kickDrawerQz(
  host: string,
  printerName?: QzPrinterTarget | null,
  hexCommand?: string | null,
  pin?: DrawerPin,
): Promise<KickDrawerQzResult> {
  const hex = resolveDrawerKickHex({ hex: hexCommand, pin });
  const qz = await loadQz();

  configureQzSignature(qz, call.post);

  const sendPulse = async (): Promise<KickDrawerQzResult> => {
    const target: QzPrinterTarget = printerName || (await qz.printers.getDefault());
    const config = qz.configs.create(target);
    await qz.print(config, [{ type: 'raw', format: 'command', flavor: 'hex', data: hex }]);
    return { status: 'Sent', hex, printer: target };
  };

  if (qz.websocket.isActive()) {
    return sendPulse();
  }
  await loadQzPrinter(host);
  return sendPulse();
}
