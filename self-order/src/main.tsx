import { applyDocumentLocale, applyStaticGuestCopy, initI18n } from './i18n'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

const colorScheme = window.matchMedia?.('(prefers-color-scheme: dark)')
if (colorScheme && !document.documentElement.classList.contains('dark')) {
  const applyColorScheme = () => document.documentElement.classList.toggle('dark', colorScheme.matches)
  applyColorScheme()
  colorScheme.addEventListener?.('change', applyColorScheme)
}

initI18n().then(() => {
  applyDocumentLocale()
  applyStaticGuestCopy()
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
})
