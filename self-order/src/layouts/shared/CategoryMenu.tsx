import { useMemo, useState, type ReactNode } from 'react'
import type { MenuItem } from '../../lib/api'
import { menuCategories } from '../../lib/menu-categories'
import CategoryTabs from './CategoryTabs'

const OTHER = '__other__'

interface CategoryMenuProps {
  menu: MenuItem[]
  allLabel: string
  otherLabel: string
  /** Renders one category's dishes; each layout keeps its own cards and grid. */
  renderItems: (items: MenuItem[]) => ReactNode
  variant?: 'row' | 'rail'
  className?: string
  /** Height of a sticky header above the chip row, in px. */
  stickyTop?: number
}

/**
 * Category chips over the guest menu. "All" lists every category under its
 * heading in the configured order; a chip shows that category only.
 */
function CategoryMenu({ menu, allLabel, otherLabel, renderItems, variant = 'row', className, stickyTop }: CategoryMenuProps) {
  const groups = useMemo(() => menuCategories(menu), [menu])
  const [active, setActive] = useState<string | null>(null)
  const keyOf = (course: string | null) => course ?? OTHER
  const chosen = groups.filter((group) => keyOf(group.course) === active)
  const visible = active === null || chosen.length === 0 ? groups : chosen

  return (
    <div className={className}>
      <CategoryTabs
        categories={groups.map((group) => ({ course: keyOf(group.course), course_label: group.label ?? otherLabel }))}
        activeCourse={active}
        onSelect={setActive}
        allLabel={allLabel}
        variant={variant}
        stickyTop={stickyTop}
      />
      <div className="min-w-0 flex-1">
        {visible.map((group) => (
          <section key={keyOf(group.course)} className="mb-6">
            {active === null && <h2 className="mb-2 mt-2 text-base font-semibold">{group.label ?? otherLabel}</h2>}
            {renderItems(group.items)}
          </section>
        ))}
      </div>
    </div>
  )
}

export default CategoryMenu
