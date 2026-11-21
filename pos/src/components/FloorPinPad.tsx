import { FormEvent, useRef, useState } from 'react';
import { auth, call, parseFrappeError } from '@ury/core';
import { Button, Input } from '@ury/ui';
import { t } from '../i18n';
import { clearFloorLogoutFailureMarker, setFloorSessionMarker } from '../lib/floor-session';

const DIGITS = ['1', '2', '3', '4', '5', '6', '7', '8', '9'];

// Stable server refusal ids (ury/ury_pos/floor_pin.py) -> localized copy.
const REFUSAL_COPY: Array<[string, string]> = [
  ['URY_FLOOR_PIN_LOCKED', 'floor_pin.locked'],
  ['URY_FLOOR_TILL_CLOSED', 'floor_pin.till_closed'],
  ['URY_FLOOR_DEVICE_NOT_PAIRED', 'floor_pin.device_not_paired'],
  ['URY_FLOOR_PIN_REFUSED', 'floor_pin.refused'],
];

// Without a known id the server never judged the PIN (rate limit, 5xx, network).
function pinError(error: unknown): string {
  const message = parseFrappeError(error, '');
  const match = REFUSAL_COPY.find(([code]) => message.includes(code));
  return match ? t(match[1]) : t('floor_pin.try_again');
}

function signedIn() {
  clearFloorLogoutFailureMarker();
  setFloorSessionMarker();
  window.location.reload();
}

export default function FloorPinPad() {
  const [pin, setPin] = useState('');
  const pinRef = useRef('');
  const [usernameForm, setUsernameForm] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState('');

  const clearPin = () => {
    pinRef.current = '';
    setPin('');
  };

  const submitPin = async (value: string) => {
    try {
      const result = await call.post<{ message?: { csrf_token?: string } }>(
        'ury.ury_pos.floor_pin.sign_in', { pin: value },
      );
      if (result?.message?.csrf_token) window.csrf_token = result.message.csrf_token;
      signedIn();
    } catch (reason) {
      setError(pinError(reason));
    } finally {
      clearPin();
      busyRef.current = false;
      setBusy(false);
    }
  };

  const press = (digit: string) => {
    if (busyRef.current || pinRef.current.length >= 4) return;
    const next = `${pinRef.current}${digit}`;
    setError('');
    pinRef.current = next;
    setPin(next);
    if (next.length === 4) {
      busyRef.current = true;
      setBusy(true);
      void submitPin(next);
    }
  };

  const submitUsername = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError('');
    try {
      await auth.loginWithUsernamePassword({ username, password });
      signedIn();
    } catch (reason) {
      setError(parseFrappeError(reason, t('floor_pin.login_failed')));
      setPassword('');
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-gray-50 p-4">
      <div className="w-full max-w-md rounded-lg bg-white px-6 py-8 shadow-md">
        <div className="mb-8 flex justify-center">
          <img src="/assets/ury/pos/ury_pos.png" alt="URY POS" className="h-8 w-auto" />
        </div>
        {!usernameForm ? (
          <div className="space-y-6 text-center">
            <h1 className="text-2xl font-semibold text-gray-900">{t('floor_pin.title')}</h1>
            <p className="text-gray-600">{t('floor_pin.pin_prompt')}</p>
            <div aria-label="4-digit PIN" className="h-12 text-4xl tracking-widest text-gray-900">
              {'●'.repeat(pin.length)}
            </div>
            <div className="grid grid-cols-3 gap-3" dir="ltr">
              {DIGITS.map((digit) => (
                <Button key={digit} type="button" variant="outline" size="lg" className="min-h-[64px] text-2xl" disabled={busy} onClick={() => press(digit)}>
                  {digit}
                </Button>
              ))}
              <Button type="button" variant="ghost" size="lg" className="min-h-[64px]" disabled={busy} onClick={clearPin}>
                {t('floor_pin.clear')}
              </Button>
              <Button type="button" variant="outline" size="lg" className="min-h-[64px] text-2xl" disabled={busy} onClick={() => press('0')}>
                0
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="lg"
                className="min-h-[64px]"
                aria-label={t('floor_pin.delete')}
                disabled={busy}
                onClick={() => { pinRef.current = pinRef.current.slice(0, -1); setPin(pinRef.current); }}
              >
                ⌫
              </Button>
            </div>
            {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
            <Button type="button" variant="link" onClick={() => { setUsernameForm(true); clearPin(); setError(''); }}>
              {t('floor_pin.username_fallback')}
            </Button>
          </div>
        ) : (
          <form className="space-y-6" onSubmit={submitUsername}>
            <h1 className="text-center text-2xl font-semibold text-gray-900">{t('floor_pin.title')}</h1>
            <div>
              <label htmlFor="user_id" className="mb-2 block text-sm font-medium text-gray-700">{t('floor_pin.username_label')}</label>
              <Input id="user_id" name="user_id" value={username} onChange={(event) => setUsername(event.target.value)} required autoComplete="username" />
            </div>
            <div>
              <label htmlFor="currentPassword" className="mb-2 block text-sm font-medium text-gray-700">{t('floor_pin.password_label')}</label>
              <Input id="currentPassword" name="currentPassword" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required autoComplete="current-password" />
            </div>
            {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
            <Button type="submit" className="w-full" disabled={busy}>{t('floor_pin.sign_in')}</Button>
            <Button type="button" variant="link" className="w-full" onClick={() => { setUsernameForm(false); setError(''); }}>
              {t('floor_pin.back_to_pin')}
            </Button>
          </form>
        )}
      </div>
    </div>
  );
}
