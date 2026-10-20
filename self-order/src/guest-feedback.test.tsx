import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { call } from '@ury/core'
import App from './App'
import Mobile from './layouts/MobileQRLayout'
import { initI18n } from './i18n'
import type { CustomerOrder, OrderingContext } from './lib/api'

const feedbackContext: OrderingContext = {
  session: 'SESSION-1', restaurant: 'Salama test', table: 'T-12', source: 'QR Table', layout: 'Mobile',
  capabilities: {
    product_detail_enabled: false, show_item_images: false, show_item_descriptions: false,
    item_notes_enabled: false, request_bill_enabled: false, customer_payment_enabled: false,
    payment_link_enabled: false, pay_at_counter_enabled: false, add_to_running_table_enabled: false,
    feedback_enabled: true,
  },
}

const billedLine = { item_code: 'Meal', item_name: 'Meal', qty: 1, rate: 12000, amount: 12000, comment: null }
const billedOrder: CustomerOrder = { invoice: 'POS-1', items: [billedLine], grand_total: 12000, billed: true }
const openOrder: CustomerOrder = { invoice: 'POS-1', items: [billedLine], grand_total: 12000, billed: false }

let root: Root
let host: HTMLDivElement
let getCalls: { path: string; params?: Record<string, unknown> }[]
let postCalls: { path: string; params?: Record<string, unknown> }[]
let feedbackResponse: { status: string; parameters: { parameter: string; label: string }[] }
let feedbackError: Error | null
let currentOrder: CustomerOrder

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  sessionStorage.clear()
  localStorage.clear()
  window.history.replaceState(null, '', '/order/feedback#r=T')
  getCalls = []
  postCalls = []
  feedbackResponse = {
    status: 'open',
    parameters: [
      { parameter: 'Food', label: 'Food label' },
      { parameter: 'Service', label: 'Service label' },
    ],
  }
  feedbackError = null
  currentOrder = billedOrder
  vi.spyOn(call, 'get').mockImplementation(async (path: string, params?: Record<string, unknown>) => {
    getCalls.push({ path, params })
    if (path.endsWith('get_customer_menu')) return { message: { items: [], name: 'Test menu', modified_time: null } }
    if (path.endsWith('get_customer_order')) return { message: currentOrder }
    throw new Error(`Unexpected GET ${path}`)
  })
  vi.spyOn(call, 'post').mockImplementation(async (path: string, params?: Record<string, unknown>) => {
    postCalls.push({ path, params })
    if (path.endsWith('get_customer_menu')) return { message: { items: [], name: 'Test menu', modified_time: null } }
    if (path.endsWith('get_customer_order')) return { message: currentOrder }
    if (path.endsWith('get_feedback_form')) {
      if (feedbackError) throw feedbackError
      return { message: feedbackResponse }
    }
    if (path.endsWith('submit_feedback')) return { message: { status: 'Received' } }
    throw new Error(`Unexpected POST ${path}`)
  })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  await initI18n('en', { setCookie() {} })
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  await initI18n('en', { setCookie() {} })
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function render(node: React.ReactNode) {
  await act(async () => { root.render(node) })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

function exactButton(text: string): HTMLButtonElement {
  const found = [...host.querySelectorAll<HTMLButtonElement>('button')].find(button =>
    button.textContent?.trim() === text || button.getAttribute('aria-label') === text,
  )
  expect(found, `button ${text}`).toBeDefined()
  return found!
}

async function click(button: HTMLElement) {
  await act(async () => button.click())
}

describe('guest feedback route', () => {
  it('renders server labels from the fragment route without ordering bootstrap', async () => {
    await render(<App />)
    expect(host.textContent).toContain('Food label')
    expect(host.textContent).toContain('Service label')
    expect(getCalls.some(call => call.path.endsWith('get_ordering_context'))).toBe(false)
    expect(postCalls.find(call => call.path.endsWith('get_feedback_form'))?.params).toEqual({ receipt_token: 'T' })
  })

  it('keeps submit disabled until every label is rated and posts only ratings plus the receipt token', async () => {
    await render(<App />)
    const submit = exactButton('Submit feedback')
    expect(submit.disabled).toBe(true)
    await click(exactButton('Food label 5 out of 5'))
    expect(submit.disabled).toBe(true)
    await click(exactButton('Service label 3 out of 5'))
    expect(submit.disabled).toBe(false)
    await click(submit)
    const request = postCalls.find(call => call.path.endsWith('submit_feedback'))
    expect(request?.params && Object.keys(request.params).sort()).toEqual(['ratings', 'receipt_token'])
    expect(JSON.parse(String(request?.params?.ratings))).toEqual([
      { parameter: 'Food', rating: 5 },
      { parameter: 'Service', rating: 3 },
    ])
    expect(request?.params?.receipt_token).toBe('T')
    expect(host.textContent).toContain('Thank you')
    expect(host.querySelector('textarea')).toBeNull()
  })

  it('shows already received and only offers retry for the stable retry refusal', async () => {
    feedbackResponse = { status: 'received', parameters: [] }
    await render(<App />)
    expect(host.textContent).toContain('Already received')
    expect([...host.querySelectorAll('button')].some(button => button.textContent?.trim() === 'Retry')).toBe(false)

    feedbackResponse = { status: 'open', parameters: [] }
    feedbackError = new Error('URY_FEEDBACK_RETRY: try again')
    await act(async () => root.unmount())
    root = createRoot(host)
    await render(<App />)
    expect(host.textContent).toContain('Another request is finishing. Please try again.')
    expect(host.textContent).not.toContain('URY_FEEDBACK_')
    expect(exactButton('Retry')).toBeDefined()
  })

  it('shows translated guest messages for stable refusal codes, never the raw code', async () => {
    const cases = {
      en: ['Feedback is not available right now.', 'Feedback is not available for this bill.'],
      sw: ['Maoni hayapatikani kwa sasa.', 'Maoni hayapatikani kwa bili hii.'],
      lg: ['Okuteesa tekuli kati.', 'Okuteesa tekuli ku bili eno.'],
    } as const
    for (const [lang, [unavailable, notEligible]] of Object.entries(cases)) {
      await initI18n(lang, { setCookie() {} })
      for (const [code, expected] of [
        ['URY_FEEDBACK_DISABLED', unavailable],
        ['URY_FEEDBACK_TEMPLATE', unavailable],
        ['URY_FEEDBACK_BILL_LINK_MISSING', unavailable],
        ['URY_FEEDBACK_NOT_ELIGIBLE', notEligible],
        ['URY_FEEDBACK_RATINGS', unavailable],
      ]) {
        feedbackError = new Error(code)
        await act(async () => root.unmount())
        root = createRoot(host)
        await render(<App />)
        expect(host.textContent, `${lang} ${code}`).toContain(expected)
        expect(host.textContent, `${lang} ${code}`).not.toContain('URY_FEEDBACK_')
        expect([...host.querySelectorAll('button')].some(button => button.textContent?.trim() === 'Retry')).toBe(false)
      }
    }
  })

  it('hides the in-session feedback button unless feedback is enabled and the order is billed', async () => {
    const disabled = { ...feedbackContext, capabilities: { ...feedbackContext.capabilities, feedback_enabled: false } }
    await render(<Mobile initialContext={disabled} />)
    expect(host.textContent).not.toContain('Rate your visit')
    currentOrder = billedOrder
    await act(async () => root.unmount())
    root = createRoot(host)
    await render(<Mobile initialContext={feedbackContext} />)
    expect(host.textContent).toContain('Rate your visit')
    currentOrder = openOrder
    await act(async () => root.unmount())
    root = createRoot(host)
    await render(<Mobile initialContext={feedbackContext} />)
    expect(host.textContent).not.toContain('Rate your visit')
  })

  it('reads the token from the fragment only and never renders it', async () => {
    sessionStorage.setItem('ury_order_context', JSON.stringify({ ...feedbackContext, qrToken: undefined }))
    window.history.replaceState(null, '', '/order/feedback?r=QUERY-TOKEN')
    feedbackResponse = { status: 'open', parameters: [] }
    await render(<App />)
    const request = postCalls.find(call => call.path.endsWith('get_feedback_form'))
    expect(request?.params).toEqual({ session: 'SESSION-1' })
    expect(host.textContent).not.toContain('QUERY-TOKEN')
    expect(host.textContent).not.toContain('#r=')
  })

  it('keeps feedback source copy free of hard-coded template labels', async () => {
    const fs = await import('node:fs/promises')
    const path = await import('node:path')
    const sourceRoot = path.resolve(process.cwd(), 'self-order/src')
    const files = await fs.readdir(sourceRoot, { recursive: true })
    for (const file of files.filter(name => /\.(ts|tsx|mjs|json)$/.test(name) && !name.includes('test'))) {
      const source = await fs.readFile(path.join(sourceRoot, file), 'utf8')
      expect(source).not.toMatch(/\b(Food|Service|Ambience)\b/)
    }
  })

  it('keeps new feedback keys present in every guest locale', async () => {
    const fs = await import('node:fs/promises')
    const path = await import('node:path')
    const localeRoot = path.resolve(process.cwd(), 'self-order/src/i18n/locales')
    const keys = ['title', 'submit', 'submitting', 'thanks', 'already_received', 'not_eligible', 'retry', 'load_error', 'rating', 'unavailable', 'retry_later']
    const locales = await Promise.all(['en', 'sw', 'lg'].map(async locale => {
      const content = JSON.parse(await fs.readFile(path.join(localeRoot, `${locale}.json`), 'utf8'))
      return { locale, feedback: content.feedback ?? {} }
    }))
    for (const key of keys) {
      expect(locales.map(locale => typeof locale.feedback[key])).toEqual(['string', 'string', 'string'])
    }
  })
})
