import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { BellRing } from 'lucide-react';
import { call } from '@ury/core';
import { acknowledgeServiceRequest, listOpenServiceRequests, type ServiceRequest } from '../lib/service-request-api';
import { getRealtimeSocket } from '../lib/realtime';
import { t } from '../i18n';

// Guest calls stay on every open staff screen until someone acknowledges them
// (Oracle Simphony guest-request alerts). Realtime refreshes at once; the 15 s
// poll is the fallback. Sound and escalation minutes come from the POS Profile.

const POLL_MS = 15_000;
const CHIME_MS = 10_000;
const CHIME = '/assets/frappe/sounds/alert.mp3';
const EVENT = 'ury_service_request';
const DOCTYPE = 'URY Service Request';

interface AlertSettings { sound: number; escalate_minutes: number }
interface RealtimeRequest { branch?: string | null }

let openTables: ReadonlyMap<string, { request_type: string; escalated: boolean }> = new Map();
const listeners = new Set<() => void>();
function publishOpenTables(next: typeof openTables) {
  openTables = next;
  listeners.forEach(listener => listener());
}

/** Tables with an unacknowledged guest request, for pulsing floor-plan cards. */
export function useOpenServiceRequestTables() {
  return useSyncExternalStore(
    listener => { listeners.add(listener); return () => listeners.delete(listener); },
    () => openTables,
  );
}

function minutesWaiting(requestedAt: string, offset: number) {
  const at = new Date(requestedAt.replace(' ', 'T')).getTime();
  if (!Number.isFinite(at)) return 0;
  return Math.max(0, Math.floor((Date.now() + offset - at) / 60000));
}

export default function ServiceRequestAlert() {
  const [requests, setRequests] = useState<ServiceRequest[]>([]);
  const [settings, setSettings] = useState<AlertSettings>({ sound: 1, escalate_minutes: 3 });
  const [offset, setOffset] = useState(0);
  const [, setTick] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const branch = useRef('');
  const active = useRef(false);
  const refreshId = useRef(0);

  const refresh = useCallback(async () => {
    const id = ++refreshId.current;
    try {
      if (!branch.current) {
        const till = await call.get<{ message: string }>('ury.ury_pos.api.getBranch');
        branch.current = till.message || '';
        if (!branch.current) return;
        void call.get<{ message: AlertSettings }>('ury.ury.api.service_requests.get_service_request_alert_settings',
          { branch: branch.current }).then(r => { if (active.current && r.message) setSettings(r.message); }).catch(() => {});
        void call.get<{ message: string }>('ury.ury.api.ury_server_time.get_server_time').then(r => {
          const serverNow = new Date(r.message.replace(' ', 'T')).getTime();
          if (active.current && Number.isFinite(serverNow)) setOffset(serverNow - Date.now());
        }).catch(() => {});
      }
      const rows = await listOpenServiceRequests(branch.current);
      if (active.current && id === refreshId.current) setRequests(rows);
    } catch {
      // Keep the last list; the next poll or event retries.
    }
  }, []);

  useEffect(() => {
    active.current = true;
    void refresh();
    const poll = setInterval(() => void refresh(), POLL_MS);
    const clock = setInterval(() => setTick(n => n + 1), 30_000);
    let socket: Awaited<ReturnType<typeof getRealtimeSocket>> | undefined;
    const onEvent = (payload: RealtimeRequest) => {
      if (payload?.branch && branch.current && payload.branch !== branch.current) return;
      void refresh();
    };
    const join = () => socket?.emit('doctype_subscribe', DOCTYPE);
    const onConnect = () => { join(); void refresh(); };
    getRealtimeSocket().then(s => {
      if (!active.current) return;
      socket = s;
      s.on(EVENT, onEvent);
      s.on('connect', onConnect);
      join();
    }).catch(() => {});
    return () => {
      active.current = false;
      ++refreshId.current;
      clearInterval(poll);
      clearInterval(clock);
      socket?.off(EVENT, onEvent);
      socket?.off('connect', onConnect);
      publishOpenTables(new Map());
    };
  }, [refresh]);

  const open = requests.filter(request => request.status === 'Open');
  const escalated = (request: ServiceRequest) =>
    minutesWaiting(request.requested_at, offset) >= settings.escalate_minutes;

  useEffect(() => {
    publishOpenTables(new Map(open.map(request => [request.table, {
      request_type: request.request_type, escalated: escalated(request),
    }])));
  });

  const ringing = open.length > 0 && Number(settings.sound) === 1;
  useEffect(() => {
    if (!ringing) return;
    const chime = () => { void new Audio(CHIME).play()?.catch?.(() => {}); };
    chime();
    const timer = setInterval(chime, CHIME_MS);
    return () => clearInterval(timer);
  }, [ringing]);

  async function acknowledge(name: string) {
    setBusy(name);
    try {
      await acknowledgeServiceRequest(name);
      if (active.current) setRequests(rows => rows.map(row => row.name === name ? { ...row, status: 'Acknowledged' } : row));
      void refresh();
    } catch {
      // The banner stays until the server records the acknowledgement.
    } finally {
      if (active.current) setBusy(null);
    }
  }

  if (open.length === 0) return null;
  return (
    <div data-testid="service-request-alert" role="alert"
      className="w-full shrink-0 border-b-4 border-red-800 bg-red-600 px-4 py-2 text-white shadow-lg">
      <ul className="flex flex-col gap-2">
        {open.map(request => {
          const late = escalated(request);
          return (
            <li key={request.name} data-escalated={late ? 'true' : 'false'}
              className={`flex flex-wrap items-center gap-3 rounded-md px-3 py-2 text-xl font-bold ${late
                ? 'animate-pulse bg-red-900 ring-4 ring-yellow-300' : 'bg-amber-500 text-gray-950'}`}>
              <BellRing className="h-7 w-7 shrink-0" aria-hidden />
              <span>{request.table}</span>
              <span>{request.request_type === 'Bill' ? t('service_requests.bill') : t('service_requests.call_waiter')}</span>
              <span>{t('service_requests.minutes_waiting', { minutes: minutesWaiting(request.requested_at, offset) })}</span>
              <button type="button" disabled={busy === request.name} onClick={() => void acknowledge(request.name)}
                className="ms-auto min-h-11 rounded-md bg-white px-4 text-lg font-bold text-red-700 disabled:opacity-60">
                {t('service_requests.acknowledge')}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
