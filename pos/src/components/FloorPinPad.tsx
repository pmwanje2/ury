import { FormEvent, useRef, useState } from 'react';
import { auth, call, parseFrappeError } from '@ury/core';
import { Button, Input } from '@ury/ui';
import { t } from '../i18n';
import { withOutboxLogin } from '../offline/outbox';
import { clearFloorLogoutFailureMarker, getFloorPad, setFloorSessionMarker } from '../lib/floor-session';

const DIGITS = ['1', '2', '3', '4', '5', '6', '7', '8', '9'];

// Stable server refusal ids (ury/ury_pos/floor_pin.py) -> localized copy.
const REFUSAL_COPY: Array<[string, string]> = [
  ['URY_FLOOR_PIN_LOCKED', 'floor_pin.locked'],
  ['URY_FLOOR_TILL_CLOSED', 'floor_pin.till_closed'],
  ['URY_FLOOR_DEVICE_NOT_PAIRED', 'floor_pin.device_not_paired'],
  ['URY_FLOOR_PIN_REFUSED', 'floor_pin.refused'],
];

interface Challenge {
  tmp_id: string;
  username: string;
  verification: { method?: string; prompt?: string | false; setup?: boolean; token_delivery?: boolean };
}

async function signedIn(owner?: string) {
  const user = await auth.getLoggedInUser();
  if (!user || user === 'Guest' || (owner && user !== owner)) {
    throw new Error(t('floor_pin.login_failed'));
  }
  setFloorSessionMarker(user, !!owner);
  clearFloorLogoutFailureMarker();
  window.location.reload();
}

export default function FloorPinPad({ offline = false, usernameOnly = false, recoveryOwner }: { offline?: boolean; usernameOnly?: boolean; recoveryOwner?: string }) {
  const { min_length: minLength = 4, max_length: maxLength = 4 } = getFloorPad();
  const fixedLength = minLength === maxLength;
  const [pin, setPin] = useState('');
  const pinRef = useRef('');
  const [keypadRefused, setKeypadRefused] = useState(false);
  const passwordOnly = usernameOnly || keypadRefused;
  const [challenge, setChallenge] = useState<Challenge | null>(null);
  const verification = challenge?.verification;
  const deliveryFailed = !!verification && (
    ('token_delivery' in verification && !verification.token_delivery) ||
    (verification.method !== 'OTP App' && verification.setup === false)
  );
  const setupMissing = verification?.method === 'OTP App' && verification.setup === false && !verification.prompt;
  const otpUnavailable = deliveryFailed || setupMissing;
  const [otp, setOtp] = useState('');
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
      await withOutboxLogin(recoveryOwner ?? null, async () => {
        try {
          const result = await call.post<{ message?: { csrf_token?: string } }>(
            'ury.ury_pos.floor_pin.sign_in', recoveryOwner ? { pin: value, expected_user: recoveryOwner } : { pin: value },
          );
          if (result?.message?.csrf_token) window.csrf_token = result.message.csrf_token;
          await signedIn(recoveryOwner);
        } catch (reason) {
          const message = parseFrappeError(reason, '');
          const refusal = REFUSAL_COPY.find(([code]) => message.includes(code));
          if (refusal?.[0] === 'URY_FLOOR_TILL_CLOSED' || refusal?.[0] === 'URY_FLOOR_DEVICE_NOT_PAIRED') {
            setKeypadRefused(true);
          }
          setError(t(refusal?.[1] ?? 'floor_pin.try_again'));
        }
      });
    } catch (reason) {
      setError(parseFrappeError(reason, t('floor_pin.owner_required')));
    } finally {
      clearPin();
      busyRef.current = false;
      setBusy(false);
    }
  };

  const confirmPin = (value: string) => {
    if (busyRef.current || value.length < minLength || value.length > maxLength) return;
    busyRef.current = true;
    setBusy(true);
    void submitPin(value);
  };

  const press = (digit: string) => {
    if (busyRef.current || pinRef.current.length >= maxLength) return;
    const next = `${pinRef.current}${digit}`;
    setError('');
    pinRef.current = next;
    setPin(next);
    if (fixedLength && next.length === maxLength) confirmPin(next);
  };

  const submitUsername = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError('');
    try {
      if (otpUnavailable) return;
      const identity = challenge?.username ?? username.trim();
      await withOutboxLogin(identity, async (pending) => {
        // Keep the recovery gate across reloads, even if session verification loses connectivity.
        if (pending) sessionStorage.setItem('ury_floor_reconcile', 'pending');
        const result = await auth.loginWithUsernamePassword(
          challenge ? { tmp_id: challenge.tmp_id, otp } : { username: identity, password },
        );
        if (result?.verification || result?.tmp_id) {
          if (!result.tmp_id || !result.verification) throw new Error(t('floor_pin.login_failed'));
          setChallenge({ tmp_id: result.tmp_id, username: identity, verification: result.verification });
        } else if (result?.message === 'Logged In' || result?.message === 'No App') {
          await signedIn(pending ? identity : undefined);
        } else {
          throw new Error(t('floor_pin.login_failed'));
        }
      });
    } catch (reason) {
      setError(parseFrappeError(reason, t('floor_pin.login_failed')));
    } finally {
      setPassword('');
      setOtp('');
      busyRef.current = false;
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-gray-50 p-4">
      <div className="w-full max-w-md rounded-lg bg-white px-6 py-8 shadow-md">
        <div className="mb-8 flex justify-center">
          <img src="/assets/ury/Images/maximus-mark.svg" alt="Maximus POS" className="h-8 w-auto" />
        </div>
        {offline && <p role="status" className="mb-4 text-center">{t('floor_pin.offline_notice')}</p>}
        {!usernameForm && !passwordOnly ? (
          <div className="space-y-6 text-center">
            <h1 className="text-2xl font-semibold text-gray-900">{t('floor_pin.title')}</h1>
            <p className="text-gray-600">
              {fixedLength && maxLength === 4 ? t('floor_pin.pin_prompt') : t('floor_pin.enter_staff_pin')}
            </p>
            <div aria-label={fixedLength && maxLength === 4 ? '4-digit PIN' : t('floor_pin.enter_staff_pin')} className="h-12 text-4xl tracking-widest text-gray-900">
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
            {!fixedLength && (
              <Button type="button" disabled={busy || pin.length < minLength} onClick={() => confirmPin(pinRef.current)}>
                {t('floor_pin.sign_in')}
              </Button>
            )}
            {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
            <Button type="button" variant="link" onClick={() => { setUsernameForm(true); clearPin(); setError(''); }}>
              {t('floor_pin.username_fallback')}
            </Button>
          </div>
        ) : (
          <form className="space-y-6" onSubmit={submitUsername}>
            <h1 className="text-center text-2xl font-semibold text-gray-900">{t('floor_pin.title')}</h1>
            {challenge ? (
              <div>
                {typeof verification?.prompt === 'string' && <p>{verification.prompt}</p>}
                {verification?.method === 'OTP App' && verification.setup && !verification.prompt && <p>{t('floor_pin.app_code')}</p>}
                {deliveryFailed && <p role="alert">{t('floor_pin.delivery_failed')}</p>}
                {setupMissing && <p role="alert">{t('floor_pin.setup_missing')}</p>}
                {!otpUnavailable && <>
                  <label htmlFor="otp" className="mb-2 block text-sm font-medium text-gray-700">{t('floor_pin.otp')}</label>
                  <Input id="otp" name="otp" autoComplete="one-time-code" inputMode="numeric" autoFocus
                    required value={otp} onChange={(event) => setOtp(event.target.value)} />
                </>}
              </div>
            ) : (
              <>
                <div>
                  <label htmlFor="user_id" className="mb-2 block text-sm font-medium text-gray-700">{t('floor_pin.username_label')}</label>
                  <Input id="user_id" name="user_id" value={username} onChange={(event) => setUsername(event.target.value)} required autoComplete="username" />
                </div>
                <div>
                  <label htmlFor="currentPassword" className="mb-2 block text-sm font-medium text-gray-700">{t('floor_pin.password_label')}</label>
                  <Input id="currentPassword" name="currentPassword" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required autoComplete="current-password" />
                </div>
              </>
            )}
            {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
            {!otpUnavailable && <Button type="submit" className="w-full" disabled={busy}>{t('floor_pin.sign_in')}</Button>}
            {challenge && <Button type="button" variant="link" className="w-full" disabled={busy}
              onClick={() => { setChallenge(null); setOtp(''); setError(''); }}>
              {t('floor_pin.username_fallback')}
            </Button>}
            {!challenge && !passwordOnly && <Button disabled={busy} type="button" variant="link" className="w-full" onClick={() => { setUsernameForm(false); setError(''); }}>
              {t('floor_pin.back_to_pin')}
            </Button>}
          </form>
        )}
      </div>
    </div>
  );
}
