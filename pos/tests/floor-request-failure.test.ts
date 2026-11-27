import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { AxiosInstance } from 'axios';
import { auth, call } from '@ury/core';
import { bindFloorRequestUser, disposeFloorRequests, floorFetch } from '@ury/core/floor-request';

const user = 'cashier@example.test';
const code = 'URY_FLOOR_SESSION_MISMATCH';
const lock = vi.fn();
const transport = (auth as unknown as { axios: AxiosInstance }).axios;
const adapter = transport.defaults.adapter;
const setUser = (value: string | null) => {
  document.cookie = value ? 'user_id=' + encodeURIComponent(value) + '; Path=/' : 'user_id=; Max-Age=0; Path=/';
};

beforeEach(() => {
  setUser(user);
  lock.mockClear();
  bindFloorRequestUser(user, lock);
});
afterEach(() => {
  disposeFloorRequests();
  setUser(null);
  transport.defaults.adapter = adapter;
  vi.unstubAllGlobals();
});

it.each([null, 'Guest'])('refuses a bound request before dispatch when user_id is %s', async live => {
  setUser(live);
  const send = vi.fn(async () => new Response());
  vi.stubGlobal('fetch', send);
  await expect(floorFetch('/api/method/ping')).rejects.toThrow(code);
  expect(send).not.toHaveBeenCalled();
  expect(lock).toHaveBeenCalledOnce();
});

it.each(['SDK', 'fetch'])('locks after an early CSRF failure on a displaced %s POST', async kind => {
  const body = { exc_type: 'CSRFTokenError' };
  if (kind === 'SDK') {
    transport.defaults.adapter = async config => {
      setUser('replacement@example.test');
      throw { config, response: { status: 400, data: body, headers: {} } };
    };
    await call.post('ping').catch(() => undefined);
  } else {
    vi.stubGlobal('fetch', async () => {
      setUser(null);
      return Response.json(body, { status: 400 });
    });
    await floorFetch('/api/method/ping', { method: 'POST' }).catch(() => undefined);
  }
  expect(lock).toHaveBeenCalledOnce();
});

it.each([401, 403, 409])('recognizes a mismatch in a %i response body when the header is unavailable', async status => {
  vi.stubGlobal('fetch', async () => Response.json({ floor_error: code }, { status }));
  await expect(floorFetch('/api/method/ping')).rejects.toThrow(code);
  expect(lock).toHaveBeenCalledOnce();
});

it('preserves an ordinary permission denial for the admitted cashier', async () => {
  vi.stubGlobal('fetch', async () => Response.json({ exc_type: 'PermissionError' }, { status: 403 }));
  expect(await (await floorFetch('/api/method/ping')).json()).toEqual({ exc_type: 'PermissionError' });
  expect(lock).not.toHaveBeenCalled();
});
