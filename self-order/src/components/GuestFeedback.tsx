import { useEffect, useState } from 'react'
import { parseFrappeError } from '@ury/core'
import { getFeedbackForm, getStoredContext, submitFeedback, type FeedbackForm, type FeedbackRating } from '../lib/api'
import { t } from '../i18n'
import { useGuestLanguage } from './LanguageSwitcher'

type FeedbackCredential = { session?: string; receiptToken?: string }
type FeedbackOutcome = 'thanks' | 'received' | 'not_eligible'
// `key` is a catalog key; the sentence is looked up when shown.
type ShownError = { key: string; retryable: boolean }

// Catalog keys, read with t() at render so a language switch applies at once.
const COPY = {
  title: 'feedback.title',
  loading: 'feedback.loading',
  submit: 'feedback.submit',
  submitting: 'feedback.submitting',
  thanks: 'feedback.thanks',
  alreadyReceived: 'feedback.already_received',
  notEligible: 'feedback.not_eligible',
  unavailable: 'feedback.unavailable',
  invalidLink: 'feedback.invalid_link',
  retryLater: 'feedback.retry_later',
  ratingsAgain: 'feedback.ratings_again',
  rateLimited: 'feedback.rate_limited',
  loadError: 'feedback.load_error',
  retry: 'feedback.retry',
}

// Stable server refusal keys become guest copy; the raw id is never shown.
const REFUSALS: Record<string, string> = {
  'FEEDBACK-RETRY': COPY.retryLater,
  'FEEDBACK-NOT-ELIGIBLE': COPY.notEligible,
  'FEEDBACK-LINK-INVALID': COPY.invalidLink,
  'FEEDBACK-DISABLED': COPY.unavailable,
  'FEEDBACK-TEMPLATE': COPY.unavailable,
  'FEEDBACK-BILL-LINK-MISSING': COPY.unavailable,
  'FEEDBACK-RATINGS': COPY.ratingsAgain,
  'FEEDBACK-RATE-LIMITED': COPY.rateLimited,
}

// Retry reloads the form: a lock conflict clears, and a ratings refusal starts the form again.
const RETRYABLE = new Set(['FEEDBACK-RETRY', 'FEEDBACK-RATINGS'])

/** The receipt token travels only in the URL fragment, which is never sent to the server. */
function readCredential(): FeedbackCredential | null {
  const token = new URLSearchParams(window.location.hash.slice(1)).get('r')
  if (token) return { receiptToken: token }
  const session = getStoredContext()?.session
  return session ? { session } : null
}

function errorText(error: unknown): ShownError {
  const parsed = parseFrappeError(error, '')
  const code = parsed.match(/\[[A-Z][A-Z0-9]{1,9}-(FEEDBACK-[A-Z-]+)\]/)?.[1]
  if (!code) return { key: COPY.loadError, retryable: false }
  return { key: REFUSALS[code] ?? COPY.unavailable, retryable: RETRYABLE.has(code) }
}

function ratingLabel(label: string, rating: number) {
  return t('feedback.rating', { parameter: label, rating })
}

/** Guest feedback for one bill, reached from the receipt QR or the in-session link. */
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
        setError({ key: COPY.invalidLink, retryable: false })
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
    return () => {
      active = false
    }
  }, [credential, retry])

  const complete =
    form?.status === 'open' &&
    form.parameters.length > 0 &&
    form.parameters.every((row) => ratings[row.parameter] !== undefined)

  async function handleSubmit() {
    if (!form || !credential || !complete) return
    setSubmitting(true)
    setError(null)
    const payload: FeedbackRating[] = form.parameters.map((row) => ({
      parameter: row.parameter,
      rating: ratings[row.parameter]!,
    }))
    try {
      const response = await submitFeedback(payload, credential.session, credential.receiptToken)
      setOutcome(response.status === 'Already Received' ? 'received' : 'thanks')
    } catch (submitError) {
      setError(errorText(submitError))
    } finally {
      setSubmitting(false)
    }
  }

  const showForm = !loading && !error && !outcome && form?.status === 'open'

  return (
    <main className="flex min-h-screen flex-col gap-4 p-6">
      <h1 className="text-2xl font-semibold">{t(COPY.title)}</h1>
      {loading && <p role="status">{t(COPY.loading)}</p>}
      {error && (
        <div className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
          <p role="status">{t(error.key)}</p>
          {error.retryable && (
            <button className="mt-3 rounded-md border px-3 py-2" onClick={() => setRetry((value) => value + 1)}>
              {t(COPY.retry)}
            </button>
          )}
        </div>
      )}
      {!loading && !error && outcome === 'thanks' && <p role="status">{t(COPY.thanks)}</p>}
      {!loading && !error && outcome === 'received' && <p role="status">{t(COPY.alreadyReceived)}</p>}
      {!loading && !error && outcome === 'not_eligible' && <p role="status">{t(COPY.notEligible)}</p>}
      {showForm &&
        form.parameters.map((row) => (
          <fieldset key={row.parameter} className="rounded-lg border p-4">
            <legend className="px-1 font-medium">{row.label}</legend>
            <div className="flex gap-2" role="radiogroup" aria-label={row.label}>
              {[1, 2, 3, 4, 5].map((rating) => (
                <button
                  key={rating}
                  type="button"
                  aria-label={ratingLabel(row.label, rating)}
                  aria-pressed={ratings[row.parameter] === rating}
                  className="min-h-11 min-w-11 rounded-md border px-3 py-2 aria-[pressed=true]:bg-primary aria-[pressed=true]:text-primary-foreground"
                  onClick={() => setRatings((previous) => ({ ...previous, [row.parameter]: rating }))}
                >
                  {rating}
                </button>
              ))}
            </div>
          </fieldset>
        ))}
      {showForm && (
        <button
          type="button"
          disabled={!complete || submitting}
          onClick={() => void handleSubmit()}
          className="rounded-md bg-primary py-3 font-medium text-primary-foreground disabled:opacity-50"
        >
          {submitting ? t(COPY.submitting) : t(COPY.submit)}
        </button>
      )}
    </main>
  )
}

export default GuestFeedback
