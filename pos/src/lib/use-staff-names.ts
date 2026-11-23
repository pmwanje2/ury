import { useEffect, useRef, useState } from 'react';
import { getUserFullNames } from '../captain/lib/captain-table-api';

/**
 * Real names (User.full_name) for staff user ids, read once per id for the
 * life of the caller with the existing bulk resolver. An id whose lookup is
 * refused (no User read permission) or fails stays unresolved, and callers
 * fall back to formatStaffDisplayName's reading of the id.
 */
export function useStaffNames(userIds: Array<string | null | undefined>): ReadonlyMap<string, string> {
  const key = Array.from(new Set(userIds.filter((id): id is string => Boolean(id)))).sort().join('\n');
  const [names, setNames] = useState<ReadonlyMap<string, string>>(() => new Map());
  const requested = useRef(new Set<string>());

  useEffect(() => {
    const missing = key ? key.split('\n').filter((id) => !requested.current.has(id)) : [];
    if (missing.length === 0) return;
    for (const id of missing) requested.current.add(id);
    getUserFullNames(missing)
      .then((found) => {
        if (found.size) setNames((current) => new Map([...current, ...found]));
      })
      .catch(() => { /* Keep the fallback names. */ });
  }, [key]);

  return names;
}
