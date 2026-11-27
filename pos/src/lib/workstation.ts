const keys = ['repeat', 'quantity', 'void', 'discount', 'noSale', 'exact', 'cash'] as const;
type FunctionKey = typeof keys[number];
type Page = { label: string; items: string[] };
const denominations: Record<string, number[]> = {
  UGX: [5000, 10000, 20000, 50000],
  USD: [1, 5, 10, 20, 50, 100],
  KWD: [1, 5, 10, 20],
};

export function workstation(raw: string | null | undefined, currency: string) {
  let config;
  try { config = raw ? JSON.parse(raw) : {}; } catch { config = {}; }
  const pages: Page[] = Array.isArray(config?.pages) ? config.pages.filter((p: Page) =>
    typeof p?.label === 'string' && p.label.trim() && Array.isArray(p.items) &&
    p.items.every(code => typeof code === 'string')) : [];
  const enabled: FunctionKey[] = Array.isArray(config?.keys)
    ? [...new Set<FunctionKey>(config.keys.filter((key: FunctionKey) => keys.includes(key)))] : [...keys];
  const values = config?.cash?.[currency];
  const cash = Array.isArray(values)
    ? [...new Set<number>(values.filter(n => typeof n === 'number' && Number.isFinite(n) && n > 0))]
    : denominations[currency] ?? [];
  return { pages, keys: enabled, cash };
}

export function parsePLU(input: string) {
  const match = /^(?:([1-9]\d?)[×x*])?(\d+)$/.exec(input.trim());
  return match ? { code: match[2], quantity: Number(match[1] || 1) } : null;
}
