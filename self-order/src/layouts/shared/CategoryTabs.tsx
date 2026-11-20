import { t } from '../../i18n'

interface CategoryTabsProps {
  /** Categories in display order. */
  categories: Array<{ course: string; course_label: string }>
  /** Currently active course filter. `null` means show all items. */
  activeCourse: string | null
  /** Called when user clicks a category chip; pass `null` for "All". */
  onSelect: (course: string | null) => void
  allLabel: string
  /** `row` is a sticky horizontal chip row; `rail` is the portrait kiosk's left column. */
  variant?: 'row' | 'rail'
  /** Height of a sticky header above the row, in px. */
  stickyTop?: number
}

/**
 * Ported from ury-erp/ury version-16 (100c43fe) self-order/src/layouts/shared/CategoryTabs.tsx:
 * an "All" chip plus one chip per course. Carrier changes: translated labels
 * and the portrait rail variant.
 */
function CategoryTabs({ categories, activeCourse, onSelect, allLabel, variant = 'row', stickyTop }: CategoryTabsProps) {
  const rail = variant === 'rail'
  const chip = (active: boolean) =>
    rail
      ? `rounded-md px-3 py-3 text-left text-sm font-medium transition ${
          active ? 'bg-primary text-primary-foreground' : 'text-foreground hover:bg-muted'
        }`
      : `whitespace-nowrap rounded-full px-4 py-2 text-sm font-medium transition ${
          active ? 'bg-primary text-primary-foreground' : 'border bg-background text-foreground hover:bg-muted'
        }`

  return (
    <nav
      aria-label={t('menu.categories')}
      style={stickyTop ? { top: `${stickyTop}px` } : undefined}
      className={
        rail
          ? 'flex w-40 shrink-0 flex-col gap-2 self-start rounded-lg border p-2'
          : 'sticky top-0 z-10 flex gap-2 overflow-x-auto bg-background/95 py-3 backdrop-blur'
      }
    >
      <button onClick={() => onSelect(null)} className={chip(activeCourse === null)}>
        {allLabel}
      </button>
      {categories.map((category) => (
        <button
          key={category.course}
          onClick={() => onSelect(category.course)}
          className={chip(activeCourse === category.course)}
        >
          {category.course_label}
        </button>
      ))}
    </nav>
  )
}

export default CategoryTabs
