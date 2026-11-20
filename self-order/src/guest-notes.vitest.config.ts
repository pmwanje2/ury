import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['self-order/src/guest-notes.test.tsx', 'self-order/src/guest-feedback.test.tsx', 'self-order/src/guest-location-header.test.tsx', 'self-order/src/guest-menu-categories.test.tsx'],
    maxWorkers: 1,
    fileParallelism: false,
  },
})
