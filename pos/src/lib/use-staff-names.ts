import { useEffect, useRef, useState } from 'react';
import { getUserFullNames } from '../captain/lib/captain-table-api';
import { isPermissionError } from '../components/dashboard/isPermissionError';

/** How long after a failed lookup its ids are asked for again. */
const RETRY_MS = 30_000;

/**
 * Real names (User.full_name) for staff user ids, read once per id for the
 * life of the caller with the existing bulk resolver. An id whose lookup is
 * refused (no User read permission) stays unresolved; one whose lookup fails
 * is asked again after RETRY_MS. Meanwhile callers fall back to
 * formatStaffDisplayName's reading of the id.
 */
export function useStaffNames(userIds: Array<string | null | undefined>): ReadonlyMap<string, string> {
  const key = Array.from(new Set(userIds.filter((id): id is string => Boolean(id)))).sort().join('\n');
  const [names, setNames] = useState<ReadonlyMap<string, string>>(() => new Map());
  const [attempt, setAttempt] = useState(0);
  const requested = useRef(new Set<string>());

  useEffect(() => {
    const missing = key ? key.split('\n').filter((id) => !requested.current.has(id)) : [];
    if (missing.length === 0) return;
    for (const id of missing) requested.current.add(id);
    let retry: ReturnType<typeof setTimeout> | undefined;
    getUserFullNames(missing)
      .then((found) => {
        if (found.size) setNames((current) => new Map([...current, ...found]));
      })
      .catch((error: unknown) => {
        if (isPermissionError(error)) return;
        for (const id of missing) requested.current.delete(id);
        retry = setTimeout(() => setAttempt((count) => count + 1), RETRY_MS);
      });
    return () => clearTimeout(retry);
  }, [key, attempt]);

  return names;
}
