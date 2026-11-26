import { useCallback, useEffect, useRef, useState } from 'react';
import { getMyPinStatus, type MyPinStatus } from './override-api';

export function isPinManagedExternally(status: MyPinStatus) {
  return !(status.can_manage_here ?? !status.external_verifier);
}

export function useMyPinStatus(active: boolean) {
  const [status, setStatus] = useState<MyPinStatus | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const pending = useRef(0);

  const refresh = useCallback(async () => {
    const request = ++pending.current;
    setLoadFailed(false);
    try {
      const result = await getMyPinStatus();
      if (request === pending.current) setStatus(result);
    } catch {
      if (request === pending.current) {
        setStatus(null);
        setLoadFailed(true);
      }
    }
  }, []);

  useEffect(() => {
    if (active) void refresh();
    else setStatus(null);
    return () => { pending.current++; };
  }, [active, refresh]);

  return { status, loadFailed, refresh, setStatus };
}
