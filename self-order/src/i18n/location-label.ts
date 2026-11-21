import { t } from '.'

/** The guest's location as its own label: "Area · Location", or the location alone; never a hard-coded "Table". */
export function locationLabel(table: string, area?: string | null): string {
  return area ? t('header.area_location', { area, table }) : t('header.location', { table })
}
