export interface Brand {
  name: string;
  logo?: string;
  favicon?: string;
}

/** Only explicit Website Settings branding may override the app defaults. */
export function resolveBrand<T extends Brand>(defaults: T): T {
  const brand = typeof window === 'undefined' ? null : (
    window as Window & { frappe?: { boot?: { ury_brand?: Partial<Brand> | null } } }
  ).frappe?.boot?.ury_brand;

  return {
    ...defaults,
    name: brand?.name || defaults.name,
    logo: brand?.logo || defaults.logo,
    favicon: brand?.favicon || defaults.favicon,
  };
}

/** Use only at display boundaries: callers still inspect the original refusal IDs. */
export function clientMessage(message: string): string {
  return message
    .replace(/\bfrappe\.exceptions\.\w+:\s*/gi, '')
    .replace(/\[URY[-_][A-Z0-9_-]+\]\s*(?=\S)/g, '')
    .replace(/\bURY[-_]([A-Z0-9_-]+)/g, '$1')
    .replace(/\b(?:URY|Frappe|ERPNext|Kamra)\b/gi, 'Maximus');
}
