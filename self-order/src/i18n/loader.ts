import { DEFAULT_LANGUAGE } from './resolve-language.ts'

type TranslationMap = Record<string, unknown>

const cache: Record<string, TranslationMap> = {}

export async function loadLocale(lang: string): Promise<TranslationMap> {
  if (cache[lang]) return cache[lang]
  try {
    const module = await import(`./locales/${lang}.json`, { with: { type: 'json' } })
    const locale = (module.default ?? module) as TranslationMap
    cache[lang] = locale
    return locale
  } catch {
    if (lang !== DEFAULT_LANGUAGE) return loadLocale(DEFAULT_LANGUAGE)
    return {}
  }
}
