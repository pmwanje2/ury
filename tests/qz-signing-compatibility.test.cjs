// Exercise the shared POS signer and Desk callback against a synthetic signing RPC.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { generateKeyPairSync, sign, verify } = require('node:crypto');
const vm = require('node:vm');
const root = resolve(__dirname, '..');
const signMethod = 'ury.ury.api.ury_print.signature_promise';

for (const client of ['shared POS', 'Desk']) {
  test(`${client} uses the guarded QZ RPC and preserves RSA-SHA512 signatures`, async () => {
    const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
    let allowed = true;
    let callback;
    let requests = 0;
    const bridge = { security: {
      setSignatureAlgorithm(algorithm) { assert.equal(algorithm, 'SHA512'); },
      setSignaturePromise(signer) { callback = signer; },
    } };
    async function post(method, { toSign }) {
      assert.equal(method, signMethod);
      requests += 1;
      if (!allowed) throw new Error('Not permitted');
      return { message: sign('RSA-SHA512', Buffer.from(toSign), privateKey).toString('base64') };
    }

    if (client === 'shared POS') {
      const { configureQzSignature } = await import('../packages/core/src/print/qz-signing.ts');
      configureQzSignature(bridge, post);
    } else {
      const frappe = { call(options) {
        post(options.method, options.args).then(options.callback, options.error);
      } };
      const source = readFileSync(resolve(root, 'ury/public/js/sign-message.js'), 'utf8');
      vm.runInNewContext(source, { qz: bridge, frappe, console: { error() {} } });
    }

    const challenge = 'synthetic QZ challenge';
    const encoded = await new Promise(callback(challenge));
    assert.equal(verify('RSA-SHA512', Buffer.from(challenge), publicKey, Buffer.from(encoded, 'base64')), true);
    allowed = false;
    await assert.rejects(new Promise(callback(challenge)),
      client === 'shared POS' ? /Printer signing was refused/ : /Not permitted/);
    assert.equal(requests, 2);
  });
}
