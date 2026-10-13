export const DEFAULT_LANGUAGE = 'en'

export const GUEST_LANGUAGES = [
  { code: 'en', label: 'EN' },
  { code: 'sw', label: 'Kiswahili' },
  { code: 'lg', label: 'Luganda' },
] as const

export const SUPPORTED_LANGUAGES: Record<string, string> = Object.fromEntries(
  GUEST_LANGUAGES.map((language) => [language.code, language.label]),
)

export const GUEST_LANGUAGE_STORAGE_KEY = 'ury_guest_language'

export function matchSupportedLanguage(raw: string | null | undefined): string | null {
  if (!raw) return null
  const normalized = raw.trim().toLowerCase().replace(/_/g, '-')
  if (SUPPORTED_LANGUAGES[normalized]) return normalized
  const primary = normalized.split('-')[0]
  if (primary && SUPPORTED_LANGUAGES[primary]) return primary
  return null
}

/**
 * Guest choice, then browser language, then the site boot language.
 * English in the browser stays English even when the site boot language is sw or lg.
 */
export function resolveGuestLanguage(input?: {
  stored?: string | null
  browserLanguages?: readonly string[] | null
  bootLang?: string | null
}): string {
  const stored = matchSupportedLanguage(input?.stored)
  if (stored) return stored
  for (const candidate of input?.browserLanguages ?? []) {
    const match = matchSupportedLanguage(candidate)
    if (match) return match
  }
  return matchSupportedLanguage(input?.bootLang) ?? DEFAULT_LANGUAGE
}

export function preferredLanguageCookie(lang: string): string {
  // Frappe reads this cookie for Guest requests, and only if that Language is enabled.
  return `preferred_language=${lang}; Path=/; Max-Age=31536000; SameSite=Lax`
}

export function selectGuestLanguage(
  lang: string,
  deps: {
    storage: { setItem(key: string, value: string): void }
    setCookie: (value: string) => void
  },
): string {
  const resolved = matchSupportedLanguage(lang)
  if (!resolved) {
    throw new Error(`unsupported guest language: ${lang}`)
  }
  deps.storage.setItem(GUEST_LANGUAGE_STORAGE_KEY, resolved)
  deps.setCookie(preferredLanguageCookie(resolved))
  return resolved
}
