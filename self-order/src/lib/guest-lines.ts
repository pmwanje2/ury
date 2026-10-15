export const GUEST_NOTE_LIMIT = 140

export interface GuestMenuItem {
  item: string
  item_name: string
  rate: number | null
  special_dish?: number
  disabled?: number
  item_image?: string | null
  course?: string | null
  course_label?: string | null
}

export interface GuestLine {
  key: string
  item: GuestMenuItem
  qty: number
  note: string
}

export type GuestCart = Record<string, GuestLine>

export interface AddonChoice {
  item_code: string
  item_name: string
  rate: number | null
}

export function guestNote(raw: unknown, notesEnabled: boolean): string {
  if (!notesEnabled || typeof raw !== 'string') return ''
  return raw.trim().slice(0, GUEST_NOTE_LIMIT)
}

export function guestLineKey(itemCode: string, note: string): string {
  return `${itemCode}\u0000${note}`
}

export function addGuestLine(
  cart: GuestCart,
  item: GuestMenuItem,
  note: string,
  notesEnabled: boolean,
): GuestCart {
  const clean = guestNote(note, notesEnabled)
  const key = guestLineKey(item.item, clean)
  const existing = cart[key]
  if (existing) {
    return { ...cart, [key]: { ...existing, item, qty: existing.qty + 1, note: clean } }
  }
  return { ...cart, [key]: { key, item, qty: 1, note: clean } }
}

export function decrementGuestLine(cart: GuestCart, key: string): GuestCart {
  const existing = cart[key]
  if (!existing) return cart
  if (existing.qty <= 1) {
    const rest = { ...cart }
    delete rest[key]
    return rest
  }
  return { ...cart, [key]: { ...existing, qty: existing.qty - 1 } }
}

export function guestOrderPayload(cart: GuestCart): { item: string; qty: number; comment?: string }[] {
  return Object.values(cart).map((line) => {
    const row: { item: string; qty: number; comment?: string } = { item: line.item.item, qty: line.qty }
    if (line.note) row.comment = line.note
    return row
  })
}

export function qtyForItem(cart: GuestCart, itemCode: string): number {
  return Object.values(cart).reduce((sum, line) => (line.item.item === itemCode ? sum + line.qty : sum), 0)
}

export function linePrice(line: GuestLine): number | null {
  const rate = line.item.rate
  if (typeof rate !== 'number' || !Number.isFinite(rate)) return null
  return line.qty * rate
}

function finiteRate(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function addonChoices(addons: unknown, menu: GuestMenuItem[]): AddonChoice[] {
  if (!Array.isArray(addons)) return []
  const choices: AddonChoice[] = []
  for (const raw of addons) {
    if (!raw || typeof raw !== 'object') continue
    const row = raw as { item_code?: unknown; item_name?: unknown; rate?: unknown }
    if (typeof row.item_code !== 'string' || row.item_code === '') continue
    const menuItem = menu.find((item) => item.item === row.item_code)
    const explicit = finiteRate(row.rate)
    const rate = explicit !== null ? explicit : menuItem ? finiteRate(menuItem.rate) : null
    const itemName = typeof row.item_name === 'string' && row.item_name
      ? row.item_name
      : menuItem?.item_name || row.item_code
    choices.push({ item_code: row.item_code, item_name: itemName, rate })
  }
  return choices
}

export function addonLineItem(choice: AddonChoice, menu: GuestMenuItem[]): GuestMenuItem {
  const fromMenu = menu.find((item) => item.item === choice.item_code)
  return {
    item: choice.item_code,
    item_name: choice.item_name,
    rate: choice.rate,
    special_dish: fromMenu?.special_dish ?? 0,
    disabled: fromMenu?.disabled ?? 0,
    item_image: fromMenu?.item_image ?? null,
    course: fromMenu?.course ?? null,
    course_label: fromMenu?.course_label ?? null,
  }
}
