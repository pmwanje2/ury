// Guest feedback form and receipt-link route (fork U6b #58) on the version-16
// self-order app. Only the HTTP boundary is doubled.
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { call } from '@ury/core'

vi.mock('./hooks/useDeviceBootstrap', () => ({
  useDeviceBootstrap: () => ({ isDevice: false, deviceContext: null, deviceLoading: false, deviceError: null }),
}))

import App from './App'
import type { CustomerOrder, OrderingContext } from './lib/api'

const M = 'ury.ury.api.self_ordering'

const feedbackContext = {
  session: 'SESSION-1', source: 'QR Table', restaurant: 'Restaurant A', company: 'Company A',
  table: 'T-12', layout: 'Mobile', session_idle_timeout_minutes: 30,
  capabilities: {
    product_detail_enabled: false, show_item_images: false, show_item_descriptions: false,
    item_notes_enabled: false, request_bill_enabled: false, customer_payment_enabled: false,
    payment_link_enabled: false, pay_at_counter_enabled: false, add_to_running_table_enabled: false,
    feedback_enabled: true,
  },
} as OrderingContext

const billedLine = { item_code: 'Meal', item_name: 'Meal', qty: 1, rate: 12000, amount: 12000, comment: null }
const billedOrder: CustomerOrder = { invoice: 'POS-1', items: [billedLine], grand_total: 12000, billed: true }
const openOrder: CustomerOrder = { invoice: 'POS-1', items: [billedLine], grand_total: 12000, billed: false }

let getCalls: { path: string; params?: Record<string, unknown> }[]
let postCalls: { path: string; params?: Record<string, unknown> }[]
let feedbackResponse: { status: string; parameters: { parameter: string; label: string }[] }
let feedbackError: Error | null
let currentOrder: CustomerOrder

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  window.history.replaceState(null, '', '/order/feedback#r=T')
  getCalls = []
  postCalls = []
  feedbackResponse = {
    status: 'open',
    parameters: [
      { parameter: 'P-1', label: 'First label' },
      { parameter: 'P-2', label: 'Second label' },
    ],
  }
  feedbackError = null
  currentOrder = billedOrder
  vi.spyOn(call, 'get').mockImplementation(async (path: string, params?: Record<string, unknown>) => {
    getCalls.push({ path, params })
    throw new Error(`Unexpected GET ${path}`)
  })
  vi.spyOn(call, 'post').mockImplementation(async (path: string, params?: Record<string, unknown>) => {
    postCalls.push({ path, params })
    if (path.endsWith('get_customer_menu')) return { message: { items: [], name: 'Menu', modified_time: null } }
    if (path.endsWith('get_customer_order')) return { message: currentOrder }
    if (path.endsWith('get_feedback_form')) {
      if (feedbackError) throw feedbackError
      return { message: feedbackResponse }
    }
    if (path.endsWith('submit_feedback')) return { message: { status: 'Received' } }
    throw new Error(`Unexpected POST ${path}`)
  })
})

afterEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

describe('guest feedback route', () => {
  it('renders server labels from the fragment link without an ordering bootstrap', async () => {
    render(<App />)
    expect(await screen.findByText('First label')).toBeInTheDocument()
    expect(screen.getByText('Second label')).toBeInTheDocument()
    expect(getCalls.some(entry => entry.path.endsWith('get_ordering_context'))).toBe(false)
    expect(postCalls.find(entry => entry.path === `${M}.get_feedback_form`)?.params).toEqual({ receipt_token: 'T' })
  })

  it('serves the same form on the canonical /ury/order route', async () => {
    window.history.replaceState(null, '', '/ury/order/feedback#r=T')
    render(<App />)
    expect(await screen.findByText('First label')).toBeInTheDocument()
  })

  it('keeps submit disabled until every label is rated and posts only ratings plus the credential', async () => {
    render(<App />)
    const submit = await screen.findByRole('button', { name: 'Submit feedback' })
    expect(submit).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'First label 5 out of 5' }))
    expect(submit).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Second label 3 out of 5' }))
    expect(submit).toBeEnabled()
    await userEvent.click(submit)
    const request = postCalls.find(entry => entry.path === `${M}.submit_feedback`)
    expect(request?.params && Object.keys(request.params).sort()).toEqual(['ratings', 'receipt_token'])
    expect(JSON.parse(String(request?.params?.ratings))).toEqual([
      { parameter: 'P-1', rating: 5 },
      { parameter: 'P-2', rating: 3 },
    ])
    expect(await screen.findByText('Thank you for your feedback.')).toBeInTheDocument()
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('shows already received and offers retry only for the stable retry refusal', async () => {
    feedbackResponse = { status: 'received', parameters: [] }
    const first = render(<App />)
    expect(await screen.findByText('Already received')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    first.unmount()

    feedbackError = new Error('URY_FEEDBACK_RETRY: try again')
    render(<App />)
    expect(await screen.findByText('Another request is finishing. Please try again.')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('URY_FEEDBACK_')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('maps every stable refusal id to guest copy and never shows the raw id', async () => {
    const cases: [string, string][] = [
      ['URY_FEEDBACK_DISABLED', 'Feedback is not available right now.'],
      ['URY_FEEDBACK_TEMPLATE', 'Feedback is not available right now.'],
      ['URY_FEEDBACK_BILL_LINK_MISSING', 'Feedback is not available right now.'],
      ['URY_FEEDBACK_RATINGS', 'Feedback is not available right now.'],
      ['URY_FEEDBACK_NOT_ELIGIBLE', 'Feedback is not available for this bill.'],
      ['URY_FEEDBACK_LINK_INVALID', 'This feedback link is not valid.'],
    ]
    for (const [code, expected] of cases) {
      feedbackError = new Error(code)
      const view = render(<App />)
      expect(await screen.findByText(expected), code).toBeInTheDocument()
      expect(document.body.textContent, code).not.toContain('URY_FEEDBACK_')
      expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
      view.unmount()
    }
  })

  it('reads the receipt token from the fragment only, falls back to the session, and never renders either', async () => {
    sessionStorage.setItem('ury_order_context', JSON.stringify(feedbackContext))
    window.history.replaceState(null, '', '/order/feedback?r=QUERY-TOKEN')
    feedbackResponse = { status: 'open', parameters: [] }
    render(<App />)
    await waitFor(() => expect(postCalls.some(entry => entry.path === `${M}.get_feedback_form`)).toBe(true))
    expect(postCalls.find(entry => entry.path === `${M}.get_feedback_form`)?.params).toEqual({ session: 'SESSION-1' })
    expect(document.body.textContent).not.toContain('QUERY-TOKEN')
    expect(document.body.textContent).not.toContain('SESSION-1')
  })

  it('refuses without a credential and makes no request', async () => {
    window.history.replaceState(null, '', '/order/feedback')
    render(<App />)
    expect(await screen.findByText('This feedback link is not valid.')).toBeInTheDocument()
    expect(postCalls).toEqual([])
  })
})

describe('in-session feedback entry (MobileQRLayout)', () => {
  async function renderMobile(ctx: OrderingContext) {
    window.history.replaceState(null, '', '/order')
    const { default: Mobile } = await import('./layouts/MobileQRLayout')
    let view: ReturnType<typeof render> | undefined
    await act(async () => { view = render(<Mobile initialContext={ctx} />) })
    await waitFor(() => expect(screen.queryByText('Loading menu…')).toBeNull())
    return view!
  }

  it('shows "Rate your visit" only when feedback is enabled and the bill is printed', async () => {
    const disabled = { ...feedbackContext, capabilities: { ...feedbackContext.capabilities, feedback_enabled: false } }
    let view = await renderMobile(disabled)
    expect(screen.queryByText('Rate your visit')).toBeNull()
    view.unmount()

    view = await renderMobile(feedbackContext)
    const link = await screen.findByText('Rate your visit')
    expect(link.closest('a')?.getAttribute('href')).toBe('/order/feedback')
    view.unmount()

    currentOrder = openOrder
    await renderMobile(feedbackContext)
    expect(screen.queryByText('Rate your visit')).toBeNull()
  })
})
