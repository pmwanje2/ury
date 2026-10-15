import { defineConfig } from 'vitest/config';
import vue from '@vitejs/plugin-vue';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const source = fileURLToPath(new URL('./src', import.meta.url));
export default defineConfig({
  root: fileURLToPath(new URL('..', import.meta.url)),
  plugins: [vue()],
  resolve: {
    alias: {
      '@': source,
      // One Vue instance across the standalone app and root test runner.
      vue: require.resolve('vue/dist/vue.esm-bundler.js'),
      pinia: resolve(dirname(require.resolve('pinia')), 'dist/pinia.mjs'),
    },
  },
  test: {
    environment: 'jsdom',
    include: ['urypos/tests/**/*.test.js', 'urypos/src/stores/*.test.js', 'urypos/src/components/*.test.js'],
    maxWorkers: 1,
    fileParallelism: false,
  },
});
