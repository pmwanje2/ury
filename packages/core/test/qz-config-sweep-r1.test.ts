// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, expect, it, vi } from 'vitest';
import { printWithQz } from '../src/print/qz';
import { printWithQz as legacyPrintWithQz } from '../../../urypos/src/stores/utils/PrintWithQz.js';

const post = vi.hoisted(() => vi.fn());
vi.mock('../src/frappe/client', () => ({ call: { post } }));
vi.mock('../../../urypos/src/stores/frappeSdk.js', () => ({ default: { call: () => ({ post }) } }));
vi.mock('../../../urypos/privateKey.js', () => ({ privateKey: '' }));

const SIGN_RPC = 'ury.ury.api.ury_print.signature_promise';

afterEach(() => {
  Reflect.deleteProperty(window, 'qz');
  post.mockReset();
});

for (const [name, print] of [['shared POS and Serve', printWithQz], ['legacy POS', legacyPrintWithQz]] as const) {
  it(`${name} requests a fresh guarded signature for each print and propagates revocation`, async () => {
    let signer: (payload: string) => (resolve: (signature: string) => void, reject: (error: unknown) => void) => void;
    const submitted = vi.fn();
    const call = vi.fn().mockResolvedValue('Receipt');
    const bridge = {
      security: {
        setCertificatePromise: vi.fn(),
        setSignatureAlgorithm: vi.fn(),
        setSignaturePromise(callback: typeof signer) { signer = callback; },
      },
      websocket: { isActive: () => true, disconnect: vi.fn() },
      printers: { getDefault: call },
      configs: { create: vi.fn() },
      print: vi.fn(async () => submitted(await new Promise(signer('sample QZ challenge')))),
    };
    Object.assign(window, { qz: bridge });

    post.mockResolvedValueOnce({ message: 'site-signature' });
    await print('localhost', '<p>receipt</p>');
    expect(post).toHaveBeenLastCalledWith(SIGN_RPC, { toSign: 'sample QZ challenge' });
    expect(submitted).toHaveBeenCalledExactlyOnceWith('site-signature');

    post.mockRejectedValueOnce(new Error('Not permitted'));
    await expect(print('localhost', '<p>receipt</p>')).rejects.toBeDefined();
    expect(post).toHaveBeenCalledTimes(2);
    expect(submitted).toHaveBeenCalledTimes(1);
  });
}

it('Serve boots without importing or receiving a signing key', () => {
  const source = readFileSync(resolve(import.meta.dirname, '../../../serve/src/main.tsx'), 'utf8');
  expect(source).not.toMatch(/VITE_QZ_SIGN_KEY|privateKey|signKey/);
});
