import { getActiveLanguage, t } from '../i18n'
import { GUEST_LANGUAGES, selectGuestLanguage } from '../i18n/resolve-language'

function LanguageSwitcher() {
  const active = getActiveLanguage()
  return (
    <div role="group" aria-label={t('language.chooser')} className="flex flex-wrap gap-1">
      {GUEST_LANGUAGES.map((language) => (
        <button
          key={language.code}
          type="button"
          aria-pressed={active === language.code}
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
            window.location.reload()
          }}
        >
          {language.label}
        </button>
      ))}
    </div>
  )
}

export default LanguageSwitcher
