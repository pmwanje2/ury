/**
 * Cross-tab & optional LAN relay peer synchronization for offline waiter/cashier outboxes (G46).
 *
 * Transport order:
 * 1. Native `BroadcastChannel` (`ury-pos-offline-peer:<branch>:<posProfile>`) when supported.
 * 2. `localStorage` `storage` event fallback for older mobile browsers without BroadcastChannel.
 * 3. Optional LAN relay (`POS Profile.custom_offline_peer_relay_url`) via HTTP POST / GET poll
 *    for cross-device waiter visibility while WAN/ERP is down.
 */

export interface OfflinePeerTableSummary {
  table: string;
  clientOrderId: string;
  waiter: string;
  itemCount: number;
  updatedAt: string;
}

export interface OfflinePeerMessage {
  type: 'outbox_snapshot' | 'outbox_cleared';
  sender: string;
  pendingTables: OfflinePeerTableSummary[];
  sentAt?: string;
}

export interface OfflinePeerSyncOptions {
  branch: string;
  posProfile: string;
  relayUrl?: string | null;
  onPeerUpdate?: (message: OfflinePeerMessage) => void;
}

export interface OfflinePeerSyncHandle {
  channelName: string;
  broadcast: (message: OfflinePeerMessage) => Promise<void>;
  pollRelay: () => Promise<OfflinePeerMessage[]>;
  close: () => void;
}

function isSafeHttpRelayUrl(url?: string | null): string | null {
  const raw = (url || '').trim();
  if (!raw || raw.length > 512) return null;
  try {
    const parsed = new URL(raw);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      return parsed.toString();
    }
  } catch {
    // Ignore malformed relay URLs safely.
  }
  return null;
}

export function createOfflinePeerSync(options: OfflinePeerSyncOptions): OfflinePeerSyncHandle {
  const channelName = `ury-pos-offline-peer:${options.branch || 'default'}:${options.posProfile || 'default'}`;
  const safeRelay = isSafeHttpRelayUrl(options.relayUrl);
  const instanceId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

  let channel: BroadcastChannel | null = null;
  if (typeof BroadcastChannel !== 'undefined') {
    try {
      channel = new BroadcastChannel(channelName);
      channel.onmessage = (event: MessageEvent) => {
        const envelope = event.data as { instanceId?: string; payload?: OfflinePeerMessage } | OfflinePeerMessage;
        if (!envelope || typeof envelope !== 'object') return;
        if ('instanceId' in envelope && envelope.instanceId === instanceId) return;
        const payload = 'payload' in envelope && envelope.payload ? envelope.payload : (envelope as OfflinePeerMessage);
        if (payload && typeof payload === 'object' && 'type' in payload) {
          options.onPeerUpdate?.(payload);
        }
      };
    } catch {
      channel = null;
    }
  }

  const onStorage = (event: StorageEvent) => {
    if (event.key !== channelName || !event.newValue) return;
    try {
      const envelope = JSON.parse(event.newValue) as { instanceId?: string; payload?: OfflinePeerMessage };
      if (envelope?.instanceId === instanceId || !envelope?.payload) return;
      options.onPeerUpdate?.(envelope.payload);
    } catch {
      // Ignore malformed storage updates.
    }
  };

  if (typeof window !== 'undefined') {
    window.addEventListener('storage', onStorage);
  }

  const broadcast = async (message: OfflinePeerMessage): Promise<void> => {
    const payload: OfflinePeerMessage = {
      ...message,
      sentAt: message.sentAt || new Date().toISOString(),
    };
    const envelope = { instanceId, payload };

    if (channel) {
      try {
        channel.postMessage(envelope);
      } catch {
        // Channel closed or unavailable.
      }
    }

    if (typeof localStorage !== 'undefined') {
      try {
        localStorage.setItem(channelName, JSON.stringify(envelope));
      } catch {
        // Storage quota or private browsing; ignore non-fatally.
      }
    }

    if (safeRelay && typeof fetch === 'function') {
      try {
        await fetch(safeRelay, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            branch: options.branch,
            posProfile: options.posProfile,
            ...payload,
          }),
          keepalive: true,
        });
      } catch {
        // Relay is best-effort LAN sync; never block local offline capture.
      }
    }
  };

  const pollRelay = async (): Promise<OfflinePeerMessage[]> => {
    if (!safeRelay || typeof fetch !== 'function') return [];
    try {
      const url = new URL(safeRelay);
      url.searchParams.set('branch', options.branch);
      url.searchParams.set('posProfile', options.posProfile);
      const res = await fetch(url.toString(), { method: 'GET', headers: { Accept: 'application/json' } });
      if (!res.ok) return [];
      const data = (await res.json()) as { peers?: OfflinePeerMessage[] };
      const peers = Array.isArray(data?.peers) ? data.peers : [];
      for (const msg of peers) {
        options.onPeerUpdate?.(msg);
      }
      return peers;
    } catch {
      return [];
    }
  };

  const close = () => {
    if (channel) {
      try {
        channel.close();
      } catch {
        // Ignore close errors.
      }
      channel = null;
    }
    if (typeof window !== 'undefined') {
      window.removeEventListener('storage', onStorage);
    }
  };

  return { channelName, broadcast, pollRelay, close };
}
