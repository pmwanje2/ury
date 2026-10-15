import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { call } from '@ury/core'
import App from '../../self-order/src/App'
import { initI18n } from '../../self-order/src/i18n'

let root: Root
let container: HTMLDivElement

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/order')
  localStorage.setItem('ury_device_id', 'device-1')
  localStorage.setItem('ury_device_credential', 'credential-1')
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  await initI18n('en', { setCookie() {} })
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  localStorage.clear()
  await initI18n('en', { setCookie() {} })
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function button(label: string) {
  return [...container.querySelectorAll('button')].find((candidate) => candidate.textContent?.trim() === label)
}

async function render(node: React.ReactNode) {
  await act(async () => {
    root.render(node)
  })
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('device recovery language', () => {
  it('translates a device recovery error that is already on screen', async () => {
    vi.spyOn(call, 'get').mockRejectedValue({
      httpStatus: 503,
      message: 'There was an error.',
      exception: '',
    })
    await render(<App />)
    expect(container.textContent).toContain('Unable to bootstrap this device.')
    expect(container.textContent).not.toContain('There was an error.')
    await act(async () => {
      button('Kiswahili')!.click()
    })
    expect(container.textContent).toContain('Imeshindwa kuanzisha kifaa hiki.')
    expect(container.textContent).not.toContain('Unable to bootstrap this device.')
    await act(async () => {
      button('Luganda')!.click()
    })
    expect(container.textContent).toContain('Ekyuma kino tekinatandika.')
    expect(container.textContent).not.toContain('Imeshindwa kuanzisha kifaa hiki.')
  })

  it('keeps the server device error when the language changes', async () => {
    vi.spyOn(call, 'get').mockRejectedValue({
      httpStatus: 403,
      message: 'There was an error.',
      exception: '',
      _server_messages: JSON.stringify([
        JSON.stringify({ message: 'This device credential is not valid.' }),
      ]),
    })
    await render(<App />)
    expect(container.textContent).toContain('This device credential is not valid.')
    expect(container.textContent).not.toContain('Unable to bootstrap this device.')
    await act(async () => {
      button('Kiswahili')!.click()
    })
    expect(container.textContent).toContain('This device credential is not valid.')
    expect(container.textContent).not.toContain('Imeshindwa kuanzisha kifaa hiki.')
  })
})
