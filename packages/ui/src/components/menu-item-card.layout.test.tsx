import { readFileSync } from 'node:fs'
import postcss from 'postcss'
import tailwindcss from 'tailwindcss'
import { beforeAll, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MenuItemCard } from './menu-item-card'

// U-4: serve shows a quantity stepper absolutely positioned over the bottom of
// the card (bottom-2, h-11). Measured in Chrome, a fixed h-60 card let a
// wrapped stock chip run 23px under that stepper. The card must grow with its
// content and keep a stepper zone the content cannot shrink. These tests read
// the real compiled Tailwind styles, not class names.

beforeAll(async () => {
  const { default: preset } = await import('../../tailwind-preset.js' as string)
  const sources = ['./menu-item-card.tsx', './badge.tsx'].map((path) => ({
    raw: readFileSync(new URL(path, import.meta.url), 'utf8'),
    extension: 'tsx',
  }))
  const { css } = await postcss([tailwindcss({ presets: [preset], content: sources })])
    .process('@tailwind utilities;', { from: undefined })
  const style = document.createElement('style')
  style.textContent = css
  document.head.appendChild(style)
})

const longChip = { variant: 'tagDestructive' as const, text: 'Not available today' }

function card() {
  return screen.getByRole('button', { name: 'Artisanal Sourdough Garlic Bread' })
}

/** The block that holds the name, course, price and chip. */
function body() {
  return card().querySelector('h3')!.parentElement!
}

describe('MenuItemCard height', () => {
  it('grows with its content instead of holding a fixed height', () => {
    render(<MenuItemCard name="Artisanal Sourdough Garlic Bread" priceLabel="₹ 14,000" course="Starters"
      availabilityTag={longChip} quantity={2} onIncrement={vi.fn()} onDecrement={vi.fn()} />)
    const style = getComputedStyle(card())
    // jsdom resolves the rem length to px: 15rem is the 240px the card used to be fixed at.
    expect(style.minHeight).toBe('240px')
    expect(style.height).toBe('auto')
  })

  it('keeps a stepper zone under the content that the content cannot shrink', () => {
    render(<MenuItemCard name="Artisanal Sourdough Garlic Bread" priceLabel="₹ 14,000" course="Starters"
      availabilityTag={longChip} quantity={2} onIncrement={vi.fn()} onDecrement={vi.fn()} />)
    const stepper = screen.getByRole('group')
    const zone = body().lastElementChild!
    expect(zone.getAttribute('aria-hidden')).toBe('true')
    expect(zone.textContent).toBe('')
    const zoneStyle = getComputedStyle(zone)
    expect(zoneStyle.flexShrink).toBe('0')
    // The zone is as tall as the stepper's buttons (h-11).
    expect(zoneStyle.height).toBe(getComputedStyle(stepper.querySelector('button')!).height)
  })

  it('reserves no stepper zone on a card that never shows a stepper', () => {
    render(<MenuItemCard name="Artisanal Sourdough Garlic Bread" priceLabel="₹ 14,000" course="Starters"
      availabilityTag={longChip} />)
    expect(body().querySelector('[aria-hidden="true"]:empty')).toBeNull()
  })

  it('lets the stock chip wrap under the price', () => {
    render(<MenuItemCard name="Artisanal Sourdough Garlic Bread" priceLabel="₹ 14,000" course="Starters"
      availabilityTag={longChip} />)
    const row = screen.getByText('₹ 14,000').parentElement!
    expect(row.contains(screen.getByText('Not available today'))).toBe(true)
    expect(getComputedStyle(row).flexWrap).toBe('wrap')
  })

  it('positions promoBadge along the bottom of the media header with truncation clear of top-right menus', () => {
    render(
      <MenuItemCard
        name="Classic Kampalan Burger"
        priceLabel="UGX 16,000"
        originalPriceLabel="UGX 20,000"
        promoBadge="Daypart Special -20%"
        course="Main"
      />
    )
    const badge = screen.getByText('Daypart Special -20%')
    const style = getComputedStyle(badge)
    expect(badge.getAttribute('title')).toBe('Daypart Special -20%')
    expect(style.bottom).not.toBe('auto')
    expect(style.top).toBe('auto')
    expect(style.textOverflow).toBe('ellipsis')
    expect(style.whiteSpace).toBe('nowrap')
  })
})
