import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import MenuGrid from './MenuGrid'
import type { MenuItem } from '../../lib/api'
import type { MenuAvailability } from '../../lib/availability'

const sellableItem: MenuItem = {
  item: 'ITEM-SELLABLE',
  item_name: 'Veg Fried Rice',
  rate: 180,
  special_dish: 0,
  disabled: 0,
  item_image: null,
  course: null,
  course_label: null,
}

const unavailableItem: MenuItem = {
  item: 'ITEM-UNAVAILABLE',
  item_name: 'Chicken Curry',
  rate: 220,
  special_dish: 0,
  disabled: 0,
  item_image: null,
  course: null,
  course_label: null,
}

function renderItem(item: MenuItem, availability: MenuAvailability) {
  const onItemClick = vi.fn()
  render(<MenuGrid menu={[item]} cart={{}} availability={availability} showImage={false}
    onItemClick={onItemClick} gridClassName="grid" cardClassName="card" />)
  return onItemClick
}

describe('MenuGrid availability gating (self-order)', () => {
  it.each([null, 4])('keeps a sellable item clickable (available_qty %s)', async (available_qty) => {
    const onItemClick = renderItem(sellableItem, {
      [sellableItem.item]: { item_code: sellableItem.item, sellable: true, available_qty, reason_code: 'AVAILABLE' },
    })
    await userEvent.click(screen.getByText('Veg Fried Rice'))
    expect(screen.queryByText(/not available|sold out|unavailable/i)).not.toBeInTheDocument()
    expect(onItemClick).toHaveBeenCalledWith(sellableItem)
  })

  it('disables an unsellable item and shows its reason-code message', async () => {
    const onItemClick = renderItem(unavailableItem, {
      [unavailableItem.item]: {
        item_code: unavailableItem.item, sellable: false, available_qty: 0, reason_code: 'NOT_PRODUCED',
      },
    })
    expect(screen.getByText('Not available today')).toBeInTheDocument()
    await userEvent.click(screen.getByText('Chicken Curry'))
    expect(onItemClick).not.toHaveBeenCalled()
  })

  it('keeps a card clickable while its availability is unknown', async () => {
    const onItemClick = renderItem(sellableItem, {})
    await userEvent.click(screen.getByText('Veg Fried Rice'))
    expect(onItemClick).toHaveBeenCalledWith(sellableItem)
  })
})
