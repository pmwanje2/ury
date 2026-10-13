import { loadLocale } from './loader.ts'
import {
  DEFAULT_LANGUAGE,
  GUEST_LANGUAGE_STORAGE_KEY,
  matchSupportedLanguage,
  preferredLanguageCookie,
  resolveGuestLanguage,
} from './resolve-language.ts'
import { translate } from './translate.ts'

type TranslationMap = Record<string, unknown>

export interface GuestI18nEnv {
  storage?: { getItem(key: string): string | null }
  navigator?: { languages?: readonly string[]; language?: string }
  bootLang?: string | null
  setCookie?: (value: string) => void
  documentElement?: { lang: string; dir: string }
}

let activeLocale: TranslationMap = {}
let englishLocale: TranslationMap = {}
let activeLanguage: string = DEFAULT_LANGUAGE

function browserLanguages(nav: GuestI18nEnv['navigator']): string[] {
  if (!nav) return []
  if (nav.languages && nav.languages.length > 0) return [...nav.languages]
  return nav.language ? [nav.language] : []
}

function liveEnv(): GuestI18nEnv {
  const win = globalThis.window as
    | (Window & { frappe?: { boot?: { lang?: string } } })
    | undefined
  if (!win) return {}
  return {
    storage: win.localStorage,
    navigator: win.navigator,
    bootLang: win.frappe?.boot?.lang ?? null,
    setCookie(value: string) {
      win.document.cookie = value
    },
    documentElement: win.document.documentElement,
  }
}

function defaultSetCookie(value: string) {
  if (typeof document === 'undefined') return
  document.cookie = value
}

export async function initI18n(lang?: string, env?: GuestI18nEnv): Promise<void> {
  const resolvedEnv = env ?? liveEnv()
  const explicit = lang ? matchSupportedLanguage(lang) : null
  const resolved =
    explicit ??
    resolveGuestLanguage({
      stored: resolvedEnv.storage?.getItem(GUEST_LANGUAGE_STORAGE_KEY) ?? null,
      browserLanguages: browserLanguages(resolvedEnv.navigator),
      bootLang: resolvedEnv.bootLang ?? null,
    })
  const [locale, english] = await Promise.all([
    loadLocale(resolved),
    resolved === DEFAULT_LANGUAGE ? Promise.resolve(null) : loadLocale(DEFAULT_LANGUAGE),
  ])
  activeLocale = locale
  englishLocale = english ?? locale
  activeLanguage = resolved
  ;(resolvedEnv.setCookie ?? defaultSetCookie)(preferredLanguageCookie(resolved))
}

export function t(key: string, params?: Record<string, string | number>): string {
  return translate(activeLocale, key, params, englishLocale)
}

export function getActiveLanguage(): string {
  return activeLanguage
}

export function applyDocumentLocale(env?: { documentElement?: { lang: string; dir: string } }): void {
  const root = env?.documentElement ?? document.documentElement
  root.lang = activeLanguage
  root.dir = 'ltr'
}
