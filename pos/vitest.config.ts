import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['pos/tests/**/*.test.{ts,tsx}', 'pos/src/**/*.test.{ts,tsx}', 'packages/core/src/**/*.test.ts'],
    maxWorkers: 1,
    fileParallelism: false,
  },
});
