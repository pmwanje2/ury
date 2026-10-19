import { useEffect, useState } from 'react'
import { parseFrappeError } from '@ury/core'
import LanguageSwitcher, { useGuestLanguage } from './LanguageSwitcher'
import { getFeedbackForm, getStoredContext, submitFeedback, type FeedbackForm, type FeedbackRating } from '../lib/api'
import { t } from '../i18n'

type FeedbackCredential = { session?: string; receiptToken?: string }
type FeedbackOutcome = 'thanks' | 'received' | 'not_eligible'

function readCredential(): FeedbackCredential | null {
  const token = new URLSearchParams(window.location.hash.slice(1)).get('r')
  if (token) return { receiptToken: token }
  const session = getStoredContext()?.session
  return session ? { session } : null
}

// Stable server refusal ids become guest copy; the raw id is never shown.
const REFUSALS: Record<string, string> = {
  URY_FEEDBACK_RETRY: 'feedback.retry_later',
  URY_FEEDBACK_NOT_ELIGIBLE: 'feedback.not_eligible',
  URY_FEEDBACK_DISABLED: 'feedback.unavailable',
  URY_FEEDBACK_TEMPLATE: 'feedback.unavailable',
  URY_FEEDBACK_BILL_LINK_MISSING: 'feedback.unavailable',
  URY_FEEDBACK_RATINGS: 'feedback.unavailable',
}

type ShownError = { text: string; retryable: boolean }

function errorText(error: unknown): ShownError {
  const parsed = parseFrappeError(error, t('feedback.load_error'))
  const code = parsed.match(/URY_FEEDBACK_[A-Z_]+/)?.[0]
  if (!code) return { text: parsed, retryable: false }
  return { text: t(REFUSALS[code] ?? 'feedback.unavailable'), retryable: code === 'URY_FEEDBACK_RETRY' }
}

function GuestFeedback() {
  useGuestLanguage()
  const [credential] = useState<FeedbackCredential | null>(readCredential)
  const [form, setForm] = useState<FeedbackForm | null>(null)
  const [ratings, setRatings] = useState<Record<string, number>>({})
  const [outcome, setOutcome] = useState<FeedbackOutcome | null>(null)
  const [error, setError] = useState<ShownError | null>(null)
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [retry, setRetry] = useState(0)

  useEffect(() => {
    let active = true
    async function load() {
      setLoading(true)
      setError(null)
      setForm(null)
      setOutcome(null)
      if (!credential) {
        setError({ text: t('errors.missing_code'), retryable: false })
        setLoading(false)
        return
      }
      try {
        const response = await getFeedbackForm(credential.session, credential.receiptToken)
        if (!active) return
        setForm(response)
        setRatings({})
        if (response.status === 'received') setOutcome('received')
        if (response.status === 'not_eligible') setOutcome('not_eligible')
      } catch (loadError) {
        if (active) setError(errorText(loadError))
      } finally {
        if (active) setLoading(false)
      }
    }
    void load()
    return () => { active = false }
  }, [credential, retry])

  async function handleSubmit() {
    if (!form || !credential || form.status !== 'open') return
    const complete = form.parameters.every(row => ratings[row.parameter] !== undefined)
    if (!complete) return
    setSubmitting(true)
    setError(null)
    const payload: FeedbackRating[] = form.parameters.map(row => ({ parameter: row.parameter, rating: ratings[row.parameter]! }))
    try {
      const response = await submitFeedback(payload, credential.session, credential.receiptToken)
      setOutcome(response.status === 'Already Received' ? 'received' : 'thanks')
    } catch (submitError) {
      setError(errorText(submitError))
    } finally {
      setSubmitting(false)
    }
  }

  const retryable = error?.retryable ?? false
  const complete = form?.status === 'open' && form.parameters.length > 0 && form.parameters.every(row => ratings[row.parameter] !== undefined)

  return (
    <main className="flex min-h-screen flex-col gap-4 p-6">
      <LanguageSwitcher />
      <h1 className="text-2xl font-semibold">{t('feedback.title')}</h1>
      {loading && <p role="status">{t('feedback.loading')}</p>}
      {error && (
        <div className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
          <p role="status">{error.text}</p>
          {retryable && <button className="mt-3 rounded-md border px-3 py-2" onClick={() => setRetry(value => value + 1)}>{t('feedback.retry')}</button>}
        </div>
      )}
      {!loading && !error && outcome === 'thanks' && <p role="status">{t('feedback.thanks')}</p>}
      {!loading && !error && outcome === 'received' && <p role="status">{t('feedback.already_received')}</p>}
      {!loading && !error && outcome === 'not_eligible' && <p role="status">{t('feedback.not_eligible')}</p>}
      {!loading && !error && !outcome && form?.status === 'open' && form.parameters.map(row => (
        <fieldset key={row.parameter} className="rounded-lg border p-4">
          <legend className="px-1 font-medium">{row.label}</legend>
          <div className="flex gap-2" role="radiogroup" aria-label={row.label}>
            {[1, 2, 3, 4, 5].map(rating => (
              <button
                key={rating}
                type="button"
                aria-label={t('feedback.rating', { parameter: row.label, rating })}
                aria-pressed={ratings[row.parameter] === rating}
                className="min-h-11 min-w-11 rounded-md border px-3 py-2 aria-[pressed=true]:bg-primary aria-[pressed=true]:text-primary-foreground"
                onClick={() => setRatings(previous => ({ ...previous, [row.parameter]: rating }))}
              >
                {rating}
              </button>
            ))}
          </div>
        </fieldset>
      ))}
      {!loading && !error && !outcome && form?.status === 'open' && (
        <button
          type="button"
          disabled={!complete || submitting}
          onClick={() => void handleSubmit()}
          className="rounded-md bg-primary py-3 font-medium text-primary-foreground disabled:opacity-50"
        >
          {submitting ? t('feedback.submitting') : t('feedback.submit')}
        </button>
      )}
    </main>
  )
}

export default GuestFeedback
