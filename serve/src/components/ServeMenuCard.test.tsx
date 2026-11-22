import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import ServeMenuCard from './ServeMenuCard'
import type { ItemAvailability } from '../lib/availability-api'

vi.mock('@ury/core', () => ({ formatCurrency: (n: number) => `Rs. ${n}` }))

const baseProps = { name: 'Redbull', price: 100, imageUrl: null }
const entry = (fields: Partial<ItemAvailability>): ItemAvailability => ({
  item_code: 'REDBULL', sellable: true, available_qty: null, reason_code: 'AVAILABLE',
  stock_policy_mode: 'RESERVATION_CONTROLLED', source: 'PRODUCTION', production_policy: null,
  company: 'C1', branch: 'B1', department: null, production_unit: null, warehouse: null,
  plan_qty: null, plan_remaining: null, fg_available: null, max_producible: null,
  blocking_component: null, as_of: '', ...fields,
})

describe('ServeMenuCard availability badge', () => {
  it('keeps manager availability controls reachable for a sold-out dish', async () => {
    const update = vi.fn().mockResolvedValue(undefined)
    render(<ServeMenuCard {...baseProps} availability={entry({ sellable: false, reason_code: 'SOLD_OUT_MANUAL' })}
      availabilityActions={{ canRestore: true, update }} />)
    await userEvent.click(screen.getByRole('button', { name: 'Manage availability' }))
    await userEvent.click(screen.getByRole('button', { name: 'Available again' }))
    expect(update).toHaveBeenCalledWith({ status: 'Available' })
    expect(document.body.textContent).not.toMatch(/\b(?:URY|Frappe|ERPNext|Kamra)\b/i)
  })
  it('shows “N left” when available_qty is low', () => {
    render(<ServeMenuCard {...baseProps} availability={entry({ available_qty: 2 })} />)
    expect(screen.getByText('2 left')).toBeInTheDocument()
  })

  it('keeps unknown availability clickable without a badge', async () => {
    const onClick = vi.fn()
    render(<ServeMenuCard {...baseProps} onClick={onClick} />)
    expect(screen.queryByText(/left|available/i)).not.toBeInTheDocument()
    await userEvent.click(screen.getByText('Redbull'))
    expect(onClick).toHaveBeenCalledOnce()
  })

  it('does not show an Available chip for native catalog items', () => {
    render(<ServeMenuCard {...baseProps} availability={entry({ stock_policy_mode: 'NATIVE', source: 'CATALOG' })} />)
    expect(screen.queryByText('Available')).not.toBeInTheDocument()
  })

  it('shows an unavailable catalog entry and refuses the tap', async () => {
    const onClick = vi.fn()
    render(<ServeMenuCard {...baseProps} onClick={onClick} availability={entry({
      stock_policy_mode: 'NATIVE', source: 'CATALOG', sellable: false, available_qty: 0, reason_code: 'ITEM_DISABLED',
    })} />)
    expect(screen.getAllByText('Currently unavailable').length).toBeGreaterThan(0)
    await userEvent.click(screen.getByText('Redbull'))
    expect(onClick).not.toHaveBeenCalled()
  })
})
