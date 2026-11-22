import { call } from '@ury/core';
import { getMenuAvailability, type ItemAvailability } from '../pages/Pos/lib/availability-api';

export interface CatalogItem {
  item_code: string;
  item_name?: string;
}

/** Upper bound on how many catalog items this page checks per load, so a
 * large catalog can't turn one page load into thousands of sequential
 * availability calls. The KPI strip reports the real number checked, so
 * this cap is never silently hidden from the user. */
export const ITEM_CHECK_LIMIT = 200;

export interface AvailabilityCheckResult {
  checked: ItemAvailability[];
  /** item_codes whose availability call errored (network/server error) -- excluded from `checked`. */
  failed: string[];
}

interface FrappeListResponse<T> {
  message?: T[];
}

interface CompanyListRow {
  name: string;
}

interface ItemListRow {
  name: string;
  item_name?: string;
}

const unwrapList = <T>(payload: FrappeListResponse<T> | T[] | undefined): T[] => {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.message)) return payload.message;
  return [];
};

export const menuAvailabilityService = {
  /**
   * Best-effort default company, mirroring the same fallback
   * `ury.ury.api.minimal.business_setup.get_branches` uses server-side
   * (user default company, else the first `Company` record) -- there is no
   * per-branch company field on the `Branch` doctype in this app.
   */
  async resolveDefaultCompany(): Promise<string | null> {
    const res = await call<FrappeListResponse<CompanyListRow>>('frappe.client.get_list', {
      doctype: 'Company',
      fields: ['name'],
      limit_page_length: 1,
    });
    const data = unwrapList(res);
    return data.length > 0 ? String(data[0].name) : null;
  },

  /** Bounded page of enabled catalog items, for checking sellability against. */
  async listCatalogItems(): Promise<CatalogItem[]> {
    const res = await call<FrappeListResponse<ItemListRow>>('frappe.client.get_list', {
      doctype: 'Item',
      fields: ['name', 'item_name'],
      filters: { disabled: 0 },
      limit_page_length: ITEM_CHECK_LIMIT,
      order_by: 'item_name asc',
    });
    return unwrapList(res).map((row) => ({ item_code: String(row.name || ''), item_name: row.item_name }));
  },

  /** Checks availability for every given item, at the given branch/company. */
  async checkAvailability(items: CatalogItem[], branch: string, company: string): Promise<AvailabilityCheckResult> {
    const codes = [...new Set(items.map(item => item.item_code))];
    try {
      const entries = await getMenuAvailability(branch, company, codes);
      return { checked: codes.flatMap(code => entries[code] ? [entries[code]] : []), failed: codes.filter(code => !entries[code]) };
    } catch {
      return { checked: [], failed: codes };
    }
  },
};
