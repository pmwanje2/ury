type TranslationMap = Record<string, unknown>

function lookup(map: TranslationMap, key: string): string | undefined {
  let value: unknown = map
  for (const part of key.split('.')) {
    if (value && typeof value === 'object') {
      value = (value as Record<string, unknown>)[part]
    } else {
      return undefined
    }
  }
  return typeof value === 'string' ? value : undefined
}

export function translate(
  map: TranslationMap,
  key: string,
  params?: Record<string, string | number>,
  fallback?: TranslationMap,
): string {
  let value = lookup(map, key)
  if (value === undefined && fallback) value = lookup(fallback, key)
  if (value === undefined) return key
  if (!params) return value
  return value.replace(/\{\{(\w+)\}\}/g, (_, token: string) => String(params[token] ?? `{{${token}}}`))
}
