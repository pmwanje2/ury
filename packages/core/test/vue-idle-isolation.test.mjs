import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const require = createRequire(new URL('../../../urypos/package.json', import.meta.url));
const root = fileURLToPath(new URL('../../../urypos', import.meta.url));

test('Vue idle wiring reuses one ERP and printing stack without another Frappe singleton', async () => {
  const { build } = await import(resolve(dirname(require.resolve('vite/package.json')), 'dist/node/index.js'));
  const vue = require('@vitejs/plugin-vue');
  const result = await build({
    root,
    configFile: false,
    plugins: [vue()],
    resolve: { alias: { '@': resolve(root, 'src') } },
    logLevel: 'silent',
    build: { write: false, target: 'es2015', minify: false },
  });
  const modules = result.output.filter(file => file.type === 'chunk')
    .flatMap(chunk => Object.keys(chunk.modules));
  assert.equal(modules.some(path => path.endsWith('/packages/core/src/frappe/client.ts')), false,
    'Adding an idle callback must not initialise another ERP client');
  assert.equal(modules.some(path => path.endsWith('/packages/core/src/print/qz.ts')), false,
    'Adding an idle callback must not initialise the printing stack');
});
