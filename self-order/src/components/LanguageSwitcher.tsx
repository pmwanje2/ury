import { useSyncExternalStore } from 'react'
import { applyDocumentLocale, getActiveLanguage, initI18n, subscribeGuestLanguage, t } from '../i18n'
import { GUEST_LANGUAGES, selectGuestLanguage } from '../i18n/resolve-language'

export function useGuestLanguage(): string {
  return useSyncExternalStore(subscribeGuestLanguage, getActiveLanguage, getActiveLanguage)
}

function LanguageSwitcher() {
  const active = useGuestLanguage()
  return (
    <div role="group" aria-label={t('language.chooser')} className="flex flex-wrap gap-1">
      {GUEST_LANGUAGES.map((language) => (
        <button
          key={language.code}
          type="button"
          aria-pressed={active === language.code}
          style={{ minHeight: 44, minWidth: 44 }}
          className={`rounded-md border px-2 py-1 text-xs font-medium ${
            active === language.code ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'
          }`}
          onClick={() => {
            if (active === language.code) return
            selectGuestLanguage(language.code, {
              storage: localStorage,
              setCookie(value: string) {
                document.cookie = value
              },
            })
            return initI18n(language.code).then(() => {
              applyDocumentLocale()
            })
          }}
        >
          {language.label}
        </button>
      ))}
    </div>
  )
}

export default LanguageSwitcher
