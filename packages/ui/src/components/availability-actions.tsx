import { useState } from 'react';
import { Ellipsis } from 'lucide-react';

export interface AvailabilityActions {
  canRestore: boolean;
  update: (change: { status?: 'Available' | 'Sold Out'; count?: number; reset_policy?: 'Never' | 'Start of business day' }) => Promise<void>;
}

export interface AvailabilityActionLabels {
  manage: string;
  soldOut: string;
  restore: string;
  dailyCount: string;
  setCount: string;
  reset: string;
  error: string;
}

export function AvailabilityActionMenu({
  actions, labels, manualSoldOut, dailyLimit, autoReset,
}: {
  actions: AvailabilityActions;
  labels: AvailabilityActionLabels;
  manualSoldOut: boolean;
  dailyLimit?: number | null;
  autoReset?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button type="button" aria-expanded={open} aria-label={labels.manage} title={labels.manage}
        className="min-h-11 min-w-11 rounded border bg-white px-2 text-gray-800"
        onClick={() => setOpen(value => !value)}>
        <Ellipsis aria-hidden="true" className="mx-auto h-5 w-5" />
      </button>
      {open && <AvailabilityActionForm {...{ actions, labels, manualSoldOut, dailyLimit, autoReset }} />}
    </div>
  );
}

function AvailabilityActionForm({
  actions, labels, manualSoldOut, dailyLimit, autoReset,
}: {
  actions: AvailabilityActions;
  labels: AvailabilityActionLabels;
  manualSoldOut: boolean;
  dailyLimit?: number | null;
  autoReset?: boolean;
}) {
  const [count, setCount] = useState(dailyLimit?.toString() ?? '');
  const [reset, setReset] = useState(autoReset ?? true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(false);
  const [localStatus, setLocalStatus] = useState<boolean>();
  const soldOut = localStatus ?? manualSoldOut;
  const send = async (change: Parameters<AvailabilityActions['update']>[0]) => {
    setPending(true);
    setError(false);
    try {
      await actions.update(change);
      if (change.status) setLocalStatus(change.status === 'Sold Out');
    } catch {
      setError(true);
    } finally {
      setPending(false);
    }
  };
  const buttonStyle = 'min-h-11 rounded border bg-white px-2 text-sm disabled:opacity-50';
  return (
    <div className="grid gap-2 rounded border bg-white p-2 text-sm text-gray-900">
      {soldOut ? actions.canRestore &&
        <button type="button" disabled={pending} className={buttonStyle}
          onClick={() => void send({ status: 'Available' })}>{labels.restore}</button>
        : <button type="button" disabled={pending} className={buttonStyle}
          onClick={() => void send({ status: 'Sold Out' })}>{labels.soldOut}</button>}
      {actions.canRestore && <form className="grid gap-2" onSubmit={event => {
        event.preventDefault();
        if (!pending && count !== '' && Number.isInteger(Number(count)) && Number(count) >= 0) {
          void send({ count: Number(count), reset_policy: reset ? 'Start of business day' : 'Never' });
        }
      }}>
        <label className="grid gap-1">
          {labels.dailyCount}
          <input type="number" min={0} max={2147483647} step={1} required value={count}
            onChange={event => setCount(event.target.value)} className="min-h-11 w-full rounded border px-2" />
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={reset} onChange={event => setReset(event.target.checked)} />
          {labels.reset}
        </label>
        <button type="submit" disabled={pending || count === ''} className={buttonStyle}>{labels.setCount}</button>
      </form>}
      {error && <p role="alert" className="text-red-700">{labels.error}</p>}
    </div>
  );
}
