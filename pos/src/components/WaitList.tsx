import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { Badge, Button, Input } from '@ury/ui';
import { parseFrappeError } from '@ury/core';
import {
  addWalkIn, estimateWait, getTables, listWaitlist, notifyWaitlistParty, seatWaitlistParty,
  type Room, type Table, type WaitlistParty,
} from '../lib/table-api';
import { t } from '../i18n';

const SELECT = 'min-h-11 min-w-0 w-full rounded-md border border-input bg-background px-2 text-foreground';

export function WaitList({ branch, rooms, onSeated }: {
  branch: string;
  rooms: Room[];
  onSeated?: () => void;
}) {
  const [parties, setParties] = useState<WaitlistParty[]>([]);
  const [ready, setReady] = useState(false);
  const [tables, setTables] = useState<Table[]>([]);
  const [seatChoice, setSeatChoice] = useState<Record<string, string>>({});
  const [room, setRoom] = useState('');
  const [name, setName] = useState('');
  const [phone, setPhone] = useState('');
  const [pax, setPax] = useState('2');
  const [quote, setQuote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [loadedAt, setLoadedAt] = useState(Date.now);
  const [now, setNow] = useState(Date.now);

  const refresh = useCallback(async () => {
    const [rows, roomTables] = await Promise.all([
      listWaitlist(branch), Promise.all(rooms.map(({ name }) => getTables(name))),
    ]);
    setParties(rows);
    setTables(roomTables.flat());
    setLoadedAt(Date.now());
    setReady(true);
  }, [branch, rooms]);

  useEffect(() => {
    const load = () => { void refresh().catch((err) => setError(parseFrappeError(err, t('floor.waitlist_error')))); };
    load();
    const poll = window.setInterval(load, 30000);
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => { window.clearInterval(poll); window.clearInterval(tick); };
  }, [refresh]);

  const perform = async (operation: () => Promise<void>) => {
    setBusy(true);
    setError('');
    try {
      await operation();
    } catch (err) {
      setError(parseFrappeError(err, t('floor.waitlist_error')));
    } finally {
      setBusy(false);
    }
  };
  const addParty = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void perform(async () => {
      await addWalkIn({
        branch, customer_name: name.trim(), customer_phone: phone.trim(), no_of_pax: Number(pax),
        requested_room: room || undefined, quoted_wait_minutes: quote === '' ? undefined : Number(quote),
      });
      setName('');
      setPhone('');
      setQuote('');
      await refresh();
    });
  };
  const estimate = () => void perform(async () => {
    const wait = await estimateWait(branch, Number(pax), room || undefined);
    if (wait === null) setError(t('floor.no_fit'));
    setQuote(wait === null ? '' : String(wait));
  });

  return (
    <div className="space-y-4 bg-background text-foreground">
      <form onSubmit={addParty} className="grid gap-3 rounded-lg border border-border p-3 sm:grid-cols-2">
        <label className="grid gap-1 text-sm font-semibold">
          {t('floor.party_name')}<Input required maxLength={140} value={name} className="min-h-11"
            onChange={(event) => setName(event.target.value)} />
        </label>
        <label className="grid gap-1 text-sm font-semibold">
          {t('floor.party_phone')}<Input required type="tel" maxLength={64} value={phone} className="min-h-11"
            onChange={(event) => setPhone(event.target.value)} />
        </label>
        <label className="grid gap-1 text-sm font-semibold">
          {t('floor.party_size')}<Input required type="number" min={1} step={1} value={pax} className="min-h-11"
            onChange={(event) => { setPax(event.target.value); setQuote(''); }} />
        </label>
        <label className="grid gap-1 text-sm font-semibold">
          {t('floor.preferred_room')}
          <select className={SELECT} value={room} onChange={(event) => { setRoom(event.target.value); setQuote(''); }}>
            <option value="">{t('floor.any_room')}</option>
            {rooms.map((room) => <option key={room.name}>{room.name}</option>)}
          </select>
        </label>
        <label className="grid gap-1 text-sm font-semibold">
          {t('floor.quote')}<Input type="number" min={0} step={1} value={quote} className="min-h-11"
            onChange={(event) => setQuote(event.target.value)} />
        </label>
        <div className="flex flex-wrap items-end gap-2">
          <Button type="button" variant="outline" disabled={busy || !(Number(pax) > 0)} className="min-h-11" onClick={estimate}>
            {t('floor.estimate')}
          </Button>
          <Button type="submit" disabled={busy} className="min-h-11">{t('floor.add_walkin')}</Button>
        </div>
      </form>
      {error && <p role="alert" className="text-sm font-semibold text-destructive">{error}</p>}
      {ready && parties.length === 0 && <p className="text-sm">{t('floor.waitlist_empty')}</p>}
      <ul className="space-y-2">
        {parties.map((party) => {
          const choices = tables.filter((table) => !table.occupied && !table.is_take_away
            && (!table.custom_table_state || table.custom_table_state === 'Available')
            && (!party.requested_room || table.restaurant_room === party.requested_room)
            && party.no_of_pax >= (table.minimum_seating || 1)
            && (!table.no_of_seats || party.no_of_pax <= table.no_of_seats));
          const selected = seatChoice[party.name];
          const phone = party.customer_phone.replace(/[().\s-]/g, '');
          const dialable = /^\+?[0-9]+$/.test(phone);
          const minutes = party.elapsed_minutes + Math.max(0, Math.floor((now - loadedAt) / 60000));
          return (
            <li key={party.name} className="grid min-w-0 gap-2 rounded-lg border border-border p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div><span className="font-semibold">{party.customer_name}</span> · {party.no_of_pax}</div>
                <Badge variant="outline">{t(party.status === 'Waiting' ? 'floor.waiting' : 'floor.notified')}</Badge>
              </div>
              <p className="flex flex-wrap items-center gap-2 text-sm">
                {dialable
                  ? <a className="inline-flex min-h-11 items-center underline" href={`tel:${phone}`}>{party.customer_phone}</a>
                  : <span>{party.customer_phone}</span>}
                <Badge variant="outline">{t('floor.elapsed', { minutes })}</Badge>
                <span>{t('floor.quoted_badge', { minutes: party.quoted_wait_minutes })}</span>
              </p>
              <div className="flex flex-wrap gap-2">
                {party.status === 'Waiting' && <Button type="button" variant="outline" className="min-h-11" disabled={busy}
                  onClick={() => void perform(async () => { await notifyWaitlistParty(party.name); await refresh(); })}>
                  {t('floor.notify')}
                </Button>}
                <select aria-label={`${t('floor.select_table')} · ${party.customer_name}`} className={`${SELECT} flex-1 basis-36`}
                  value={selected || ''} onChange={(event) => setSeatChoice({ ...seatChoice, [party.name]: event.target.value })}>
                  <option value="">{t('floor.select_table')}</option>
                  {choices.map((table) => <option key={table.name}>{table.name}</option>)}
                </select>
                <Button type="button" className="min-h-11" disabled={busy || !choices.some((table) => table.name === selected)}
                  onClick={() => void perform(async () => {
                    await seatWaitlistParty(party.name, selected);
                    onSeated?.();
                    await refresh();
                  })}>{t('floor.seat_party')}</Button>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
