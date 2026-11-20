import type { MenuItem } from './api'

export interface MenuCategory {
  /** URY Menu Course name; null groups items without a course. */
  course: string | null
  label: string | null
  items: MenuItem[]
}

const unset = Number.POSITIVE_INFINITY

/**
 * Groups the menu by course in the menu's configured order, like Simphony's
 * screen look-up groups: course serving priority first, then the course's
 * first row on the menu. Items keep their menu row order. Without either
 * value the server's order is kept. Items without a course come last.
 */
export function menuCategories(menu: MenuItem[]): MenuCategory[] {
  const rows = menu.map((item, position) => ({ item, position, order: item.menu_order ?? unset }))
  rows.sort((a, b) => a.order - b.order || a.position - b.position)

  const groups = new Map<string | null, MenuCategory & { priority: number; first: number }>()
  rows.forEach(({ item }, rank) => {
    const course = item.course || null
    let group = groups.get(course)
    if (!group) {
      group = { course, label: course ? item.course_label || course : null, items: [], priority: unset, first: rank }
      groups.set(course, group)
    }
    if (item.course_priority) group.priority = Math.min(group.priority, item.course_priority)
    group.items.push(item)
  })

  return [...groups.values()]
    .sort((a, b) => Number(a.course === null) - Number(b.course === null) || a.priority - b.priority || a.first - b.first)
    .map(({ course, label, items }) => ({ course, label, items }))
}
