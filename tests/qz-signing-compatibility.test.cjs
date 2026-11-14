// Execute the unchanged, Git-bound callback without loading printer transport or private-key modules.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { createRequire } = require('node:module');
const { createHash, generateKeyPairSync, sign, verify } = require('node:crypto');
const vm = require('node:vm');
const root = resolve(__dirname, '..');
const localRequire = createRequire(resolve(root, 'urypos/package.json'));

for (const keyType of ['pkcs8', 'pkcs1']) {
test(`actual QZ RSA-SHA512 callback preserves deterministic signatures with synthetic ${keyType} on jsrsasign 11.1.3`, () => {
  const source = readFileSync(resolve(root, 'urypos/src/stores/utils/PrintWithQz.js'));
  const blob = createHash('sha1').update(`blob ${source.length}\0`).update(source).digest('hex');
  assert.equal(blob, 'e2fda90a4c911f09ba3ff32b195bedb39755cf3d');
  assert.equal(localRequire('jsrsasign/package.json').version, '11.1.3');
  const marker = 'qz.security.setSignaturePromise(';
  const text = source.toString();
  assert.equal(text.split(marker).length, 2);
  const start = text.indexOf(marker) + marker.length;
  const end = text.indexOf('\n        });', start);
  assert.ok(end > start);
  const expression = text.slice(start, end) + '\n        }';
  const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  const syntheticKey = privateKey.export({ type: keyType, format: 'pem' });
  const { KEYUTIL, KJUR, stob64, hextorstr } = localRequire('jsrsasign');
  const callback = vm.runInNewContext(`(${expression})`, {
    KEYUTIL, KJUR, stob64, hextorstr, privateKey: syntheticKey,
    reject(error) { throw error; },
  }, { timeout: 1000 });
  const challenge = 'synthetic QZ challenge: independent RSA-SHA512 verification';
  let encoded;
  callback(challenge)((value) => { encoded = value; });
  assert.equal(typeof encoded, 'string');
  const signature = Buffer.from(encoded, 'base64');
  assert.ok(verify('RSA-SHA512', Buffer.from(challenge), publicKey, signature));
  assert.deepEqual(signature, sign('RSA-SHA512', Buffer.from(challenge), privateKey));
  assert.equal(verify('RSA-SHA512', Buffer.from(challenge + '!'), publicKey, signature), false);
  const tampered = Buffer.from(signature);
  tampered[0] ^= 1;
  assert.equal(verify('RSA-SHA512', Buffer.from(challenge), publicKey, tampered), false);
});
}
