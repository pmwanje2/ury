import fr from './locales/fr.json'
import es from './locales/es.json'
import ar from './locales/ar.json'
import en from './locales/en.json'
import sw from './locales/sw.json'
import lg from './locales/lg.json'
import { DEFAULT_LANGUAGE } from './resolve-language'

type TranslationMap = Record<string, unknown>

export const bundledLocales: Record<string, TranslationMap> = { en, sw, lg, fr, es, ar }
const bundled = bundledLocales

const cache: Record<string, TranslationMap> = {}

export async function loadLocale(lang: string): Promise<TranslationMap> {
  if (cache[lang]) return cache[lang]
  const locale = bundled[lang]
  if (locale) {
    cache[lang] = locale
    return locale
  }
  if (lang !== DEFAULT_LANGUAGE) return loadLocale(DEFAULT_LANGUAGE)
  return {}
}
