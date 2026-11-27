import { useState } from 'react';
import { t } from '../i18n';
import { parsePLU } from '../lib/workstation';

export function PLUKeypad({ onLookup, onQuantity, multiplier, disabled }: {
  onLookup: (code: string, quantity: number) => boolean;
  onQuantity?: (quantity: number) => void;
  disabled?: boolean;
  multiplier: number;
}) {
  const [entry, setEntry] = useState('');
  const [open, setOpen] = useState(false);
  const [error, setError] = useState(false);
  const submit = () => {
    const plu = parsePLU(entry);
    if (!plu || !onLookup(plu.code, plu.quantity * multiplier)) { setError(true); return; }
    setOpen(false);
    setEntry('');
    setError(false);
  };
  return <div className="relative">
    <button type="button" className="min-h-11 rounded border px-2" disabled={disabled} aria-expanded={open && !disabled} onClick={() => setOpen(!open)}>PLU {multiplier > 1 && multiplier + '×'}</button>
    {open && !disabled && <div className="absolute start-0 top-full z-30 grid w-52 grid-cols-3 gap-1 rounded border bg-background p-2 text-foreground shadow">
      <input className="col-span-3 min-h-11 border bg-background px-2" dir="ltr" inputMode="numeric"
        aria-label={t('workstation.plu')} value={entry} onChange={e => setEntry(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter') submit(); if (e.key === 'Escape') setOpen(false); }} />
      {'1234567890'.split('').map(n => <button type="button" key={n} className="min-h-11 rounded border px-2" onClick={() => setEntry(entry + n)}>{n}</button>)}
      {onQuantity && <button type="button" className="min-h-11 rounded border px-2" disabled={!/^[1-9]\d?$/.test(entry)} onClick={() => {
        onQuantity(Number(entry)); setEntry(''); setOpen(false);
      }}>N×</button>}
      <button type="button" className="min-h-11 rounded border px-2" onClick={() => setEntry(entry.slice(0, -1))}>⌫</button>
      <button type="button" className="col-span-3" onClick={submit}>↵</button>
      {error && <span role="alert" className="col-span-3">{t('menu.unavailable')}</span>}
    </div>}
  </div>;
}
