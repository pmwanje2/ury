export interface CoversProfile {
  max_covers?: number;
  order_type_cover_limits?: Record<string, number>;
}

const MAX_STORED_COVERS = 2_147_483_647;

export function normalizeCovers(value: unknown): number {
  const text = typeof value === 'string' ? value.replace(/^[ \t\r\n\v\f]+|[ \t\r\n\v\f]+$/g, '') : null;
  const numeric = text !== null && text.length <= 64 && /^\+?[0-9]+(?:\.0+)?$/.test(text) ? Number(text) : value;
  return typeof numeric === 'number' && Number.isInteger(numeric)
    && numeric >= 1 && numeric <= MAX_STORED_COVERS ? numeric : 1;
}

export function coversLimit(profile: CoversProfile | null | undefined, orderType: string): number {
  const limit = profile?.order_type_cover_limits?.[orderType] ?? profile?.max_covers;
  return typeof limit === 'number' && limit > 0 ? limit : Infinity;
}
