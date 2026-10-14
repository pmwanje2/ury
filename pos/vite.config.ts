import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'
import { fileURLToPath } from 'url'
import { dirname, resolve } from 'path'
import { precacheModifyURLPrefix, workboxRuntimeCaching } from './src/pwa/runtime-caching'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      injectRegister: false,
      registerType: 'autoUpdate',
      filename: 'sw.js',
      manifest: false,
      workbox: {
        inlineWorkboxRuntime: true,
        // Precache becomes ury-pos-precache-v2-<scope>; purge removes only ury-pos-*.
        cacheId: 'ury-pos',
        clientsClaim: true,
        skipWaiting: true,
        globPatterns: ['**/*.{js,css,ico,png,svg,woff2}'],
        globIgnores: ['**/index.html'],
        // Plugin default serves precached index.html for every navigation.
        // Only the explicit /pos shell route below may be cached.
        navigateFallback: undefined,
        // /sw.js is outside this outDir. Root precache URLs at the asset base.
        modifyURLPrefix: precacheModifyURLPrefix(),
        runtimeCaching: workboxRuntimeCaching(),
        maximumFileSizeToCacheInBytes: 4 * 1024 * 1024,
      },
    }),
  ],
  resolve: {
    alias: {
      "@": resolve(__dirname, "./src"),
    },
    dedupe: ['react', 'react-dom'],
  },
  server: {
    fs: {
      allow: ['..'],
    },
  },
  build: {
    outDir: "../ury/public/pos",
    emptyOutDir: true,
  },
})
