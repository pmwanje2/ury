// Guest item notes and server-priced add-ons (fork U2 #52), re-authored on the
// version-16 self-order seams: useOrderingSession owns the cart, ProductDetail
// is the per-item surface, CartPage/CartPanel render lines, MenuGrid badges.
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getMenuMock = vi.fn()
const getCurrentOrderMock = vi.fn()
const addItemsMock = vi.fn()
const getCustomerProductMock = vi.fn()

vi.mock('./lib/api', async () => {
  const actual = await vi.importActual<typeof import('./lib/api')>('./lib/api')
  return {
    ...actual,
    getMenu: (...args: unknown[]) => getMenuMock(...args),
    getCurrentOrder: (...args: unknown[]) => getCurrentOrderMock(...args),
    addItems: (...args: unknown[]) => addItemsMock(...args),
    getCustomerProduct: (...args: unknown[]) => getCustomerProductMock(...args),
    getStoredContext: () => null,
  }
})

vi.mock('./lib/availability', () => ({
  getItemAvailability: vi.fn(async () => ({ sellable: true, reason_code: 'AVAILABLE' })),
  getAvailabilityMessage: vi.fn(() => ''),
}))

import { useOrderingSession, type CartEntry } from './hooks/useOrderingSession'
import ProductDetail from './layouts/shared/ProductDetail'
import CartPage from './layouts/shared/CartPage'
import MenuGrid from './layouts/shared/MenuGrid'
import type { CustomerOrder, MenuItem, OrderingContext } from './lib/api'

const item = (code: string, rate: number | null = 6000, disabled = 0): MenuItem => ({
  item: code, item_name: code, rate: rate as number, disabled, special_dish: 0,
  item_image: null, course: null, course_label: null,
})

const context = (notes = true): OrderingContext => ({
  session: 'SESSION-1', source: 'QR Table', restaurant: 'Restaurant A', company: 'Company A',
  table: 'T-12', layout: 'Mobile', session_idle_timeout_minutes: 30,
  capabilities: {
    product_detail_enabled: true, show_item_images: false, show_item_descriptions: false,
    item_notes_enabled: notes, request_bill_enabled: true, customer_payment_enabled: false,
    payment_link_enabled: false, pay_at_counter_enabled: true, add_to_running_table_enabled: true,
  },
})

const emptyOrder: CustomerOrder = { invoice: null, items: [], grand_total: 0, billed: false }
const fish = item('Fish', 12000)
const menu = [fish, item('Sauce', 2000), item('Free', 0), item('NoPrice', null)]

async function readyHook(notes = true, items: MenuItem[] = menu) {
  getMenuMock.mockResolvedValueOnce({ items, name: 'Menu', modified_time: null })
  getCurrentOrderMock.mockResolvedValueOnce(emptyOrder)
  const ctx = context(notes)
  const view = renderHook(() => useOrderingSession(ctx))
  await waitFor(() => expect(view.result.current.loading).toBe(false))
  return view
}

async function submit(view: Awaited<ReturnType<typeof readyHook>>) {
  addItemsMock.mockResolvedValueOnce(emptyOrder)
  await act(async () => { await view.result.current.submitCart() })
  const calls = addItemsMock.mock.calls
  return calls[calls.length - 1]?.[1]
}

beforeEach(() => {
  getMenuMock.mockReset()
  getCurrentOrderMock.mockReset()
  addItemsMock.mockReset()
  getCustomerProductMock.mockReset()
  sessionStorage.clear()
})

describe('guest notes in the cart (useOrderingSession)', () => {
  it('keeps differently noted lines separate, merges equal notes, adjusts the right line, and sends no price', async () => {
    const view = await readyHook()
    act(() => {
      view.result.current.addToCart(fish, { comment: 'no onions' })
      view.result.current.addToCart(fish, { comment: 'extra chili' })
      view.result.current.addToCart(fish, { comment: 'no onions' })
    })
    const lines = view.result.current.cartItems
    expect(lines.map(line => [line.comment, line.qty])).toEqual([['no onions', 2], ['extra chili', 1]])
    const [noOnions, chili] = lines
    act(() => view.result.current.decrementCart(noOnions.key!))
    act(() => view.result.current.addToCart(chili.item, { comment: chili.comment }))
    expect(await submit(view)).toEqual([
      { item: 'Fish', qty: 1, comment: 'no onions' },
      { item: 'Fish', qty: 2, comment: 'extra chili' },
    ])
  })

  it('trims notes and caps them at 140 characters (the native comment field width)', async () => {
    const view = await readyHook()
    act(() => view.result.current.addToCart(fish, { comment: `  ${'x'.repeat(141)}  ` }))
    expect(await submit(view)).toEqual([{ item: 'Fish', qty: 1, comment: 'x'.repeat(140) }])
  })

  it('never sends a note when the profile disables notes, and groups plain repeats', async () => {
    const view = await readyHook(false)
    act(() => {
      view.result.current.addToCart(fish, { comment: 'should not leave the device' })
      view.result.current.addToCart(fish)
    })
    expect(await submit(view)).toEqual([{ item: 'Fish', qty: 2 }])
  })

  it('adds selected add-ons as their own lines billed by the server menu, never as a hidden extra', async () => {
    const view = await readyHook()
    act(() => view.result.current.addToCart(fish, { addons: ['Sauce', 'Free', 'Offmenu', 'NoPrice'] }))
    expect(view.result.current.cartItems.map(line => [line.item.item, line.qty])).toEqual([
      ['Fish', 1], ['Sauce', 1], ['Free', 1],
    ])
    expect(view.result.current.cartTotal).toBe(14000)
    const payload = await submit(view)
    expect(payload).toEqual([{ item: 'Fish', qty: 1 }, { item: 'Sauce', qty: 1 }, { item: 'Free', qty: 1 }])
    expect(JSON.stringify(payload)).not.toMatch(/rate|price|amount/)
  })

  it('does not put an item with an unknown price into the cart', async () => {
    const view = await readyHook()
    act(() => view.result.current.addToCart(item('NoPrice', null)))
    expect(view.result.current.cartItems).toEqual([])
    expect(view.result.current.cartCount).toBe(0)
  })
})

describe('guest note and add-on choices (ProductDetail)', () => {
  const detail = (addons: { item_code: string; item_name: string; rate: number | null }[] = []) => ({
    item_code: 'Fish', item_name: 'Fish', description: null, image: null, variants: [],
    addons: addons.map(addon => ({ ...addon, image: null })),
  })

  it('offers a 140-character kitchen note only when notes are enabled and passes it to the cart', async () => {
    getCustomerProductMock.mockResolvedValue(detail())
    const onAddToCart = vi.fn()
    const { unmount } = render(
      <ProductDetail session="S" itemCode="Fish" menuItem={fish} menu={menu} itemNotesEnabled
        onAddToCart={onAddToCart} onBack={vi.fn()} />,
    )
    const note = await screen.findByLabelText('Note for the kitchen')
    expect((note as HTMLTextAreaElement).maxLength).toBe(140)
    fireEvent.change(note, { target: { value: 'no onions' } })
    await userEvent.click(screen.getByText('Add to cart'))
    expect(onAddToCart).toHaveBeenCalledWith(fish, expect.objectContaining({ comment: 'no onions' }))
    unmount()

    render(
      <ProductDetail session="S" itemCode="Fish" menuItem={fish} menu={menu} itemNotesEnabled={false}
        onAddToCart={vi.fn()} onBack={vi.fn()} />,
    )
    await screen.findByText('Add to cart')
    expect(screen.queryByLabelText('Note for the kitchen')).toBeNull()
  })

  it('shows add-on prices from the guest menu, blocks unknown prices and hides off-menu add-ons', async () => {
    getCustomerProductMock.mockResolvedValue(detail([
      { item_code: 'Sauce', item_name: 'Sauce', rate: null },
      { item_code: 'Free', item_name: 'Free', rate: 0 },
      { item_code: 'NoPrice', item_name: 'NoPrice', rate: null },
      { item_code: 'Offmenu', item_name: 'Offmenu', rate: 500 },
    ]))
    const onAddToCart = vi.fn()
    render(
      <ProductDetail session="S" itemCode="Fish" menuItem={fish} menu={menu}
        onAddToCart={onAddToCart} onBack={vi.fn()} />,
    )
    const sauce = (await screen.findByText('Sauce')).closest('button')!
    expect(sauce.textContent).toContain('2000')
    const unknown = screen.getByText('NoPrice').closest('button')!
    expect(unknown.disabled).toBe(true)
    expect(unknown.textContent).toContain('Price unavailable')
    expect(screen.queryByText('Offmenu')).toBeNull()
    const free = screen.getByText('Free').closest('button')!
    expect(free.disabled).toBe(false)
    await userEvent.click(unknown)
    await userEvent.click(sauce)
    await userEvent.click(free)
    await userEvent.click(screen.getByText('Add to cart'))
    expect(onAddToCart).toHaveBeenCalledWith(fish, expect.objectContaining({ addons: ['Sauce', 'Free'] }))
  })

  it('lets the guest retry a failed add-on read instead of losing the item', async () => {
    getCustomerProductMock
      .mockRejectedValueOnce(new Error('503 synthetic add-on outage'))
      .mockResolvedValueOnce(detail([{ item_code: 'Sauce', item_name: 'Sauce', rate: 2000 }]))
    render(
      <ProductDetail session="S" itemCode="Fish" menuItem={fish} menu={menu}
        onAddToCart={vi.fn()} onBack={vi.fn()} />,
    )
    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Sauce')).toBeInTheDocument()
    expect(getCustomerProductMock).toHaveBeenCalledTimes(2)
  })
})

describe('noted cart lines (CartPage, MenuGrid)', () => {
  const lines: CartEntry[] = [
    { key: JSON.stringify(['Fish', 'no onions']), item: fish, qty: 2, comment: 'no onions' },
    { key: JSON.stringify(['Fish', '<img src=x onerror=alert(1)>']), item: fish, qty: 1, comment: '<img src=x onerror=alert(1)>' },
  ]

  it('renders each noted line, as text, and adjusts that line only', async () => {
    const onIncrement = vi.fn()
    const onDecrement = vi.fn()
    const { container } = render(
      <CartPage cartItems={lines} cartCount={3} cartTotal={36000} itemNotesEnabled
        onIncrement={onIncrement} onDecrement={onDecrement} onBack={vi.fn()} onCheckout={vi.fn()} />,
    )
    expect(container.querySelectorAll('li')).toHaveLength(2)
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>')
    expect(container.querySelector('img')).toBeNull()
    const [first] = container.querySelectorAll('li')
    fireEvent.click(first.querySelector('button[aria-label^="Add one more"]')!)
    fireEvent.click(first.querySelector('button[aria-label^="Remove one"]')!)
    expect(onIncrement).toHaveBeenCalledWith(fish, { comment: 'no onions' })
    expect(onDecrement).toHaveBeenCalledWith(lines[0].key!)
  })

  it('badges the menu card with every line of that dish', async () => {
    render(
      <MenuGrid menu={[fish]} cart={Object.fromEntries(lines.map(line => [line.key, line]))} showImage={false}
        onItemClick={vi.fn()} gridClassName="grid" cardClassName="card" />,
    )
    expect(screen.getByText('In cart: 3')).toBeInTheDocument()
  })
})
