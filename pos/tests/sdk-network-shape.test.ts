// @vitest-environment node
// Real @ury/core client -> frappe-js-sdk 1.14.0 -> axios fetch adapter ->
// real TCP. Node has no XHR; both browser adapters report ERR_NETWORK.
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterAll, beforeAll, beforeEach, expect, test } from 'vitest';
import type { AxiosInstance } from 'axios';
import { auth, call } from '../../packages/core/src/frappe/client';
import { isNetworkFailure, readLoggedUser } from '../../packages/core/src/frappe/auth';

let server: http.Server;
let liveBase: string;
let deadBase: string;
let status = 200;
// The SDK stores the shared axios instance on each API object.
const transport = (auth as unknown as { axios: AxiosInstance }).axios;

beforeAll(async () => {
  server = http.createServer((_request, response) => {
    response.statusCode = status;
    response.setHeader('content-type', 'application/json');
    response.end(JSON.stringify(status === 200
      ? { message: 'cashier@ury.com' }
      : { exc_type: status === 401 ? 'AuthenticationError' : 'PermissionError' }));
  });
  await new Promise<void>((done) => server.listen(0, '127.0.0.1', done));
  liveBase = 'http://127.0.0.1:' + (server.address() as AddressInfo).port;
  const dead = http.createServer();
  await new Promise<void>((done) => dead.listen(0, '127.0.0.1', done));
  deadBase = 'http://127.0.0.1:' + (dead.address() as AddressInfo).port;
  await new Promise<void>((done) => dead.close(() => done()));
  transport.defaults.adapter = 'fetch';
});

afterAll(async () => {
  await new Promise<void>((done) => server.close(() => done()));
});

beforeEach(() => {
  status = 200;
  transport.defaults.baseURL = liveBase;
});

async function sdkError(run: () => Promise<unknown>): Promise<unknown> {
  try {
    await run();
  } catch (error) {
    return error;
  }
  throw new Error('expected the SDK call to fail');
}

test('a refused connection reaches callers as a tagged network failure, not a TypeError', async () => {
  transport.defaults.baseURL = deadBase;
  const error = await sdkError(() => auth.getLoggedInUser());
  expect(error).not.toBeInstanceOf(TypeError);
  expect(isNetworkFailure(error)).toBe(true);
  expect(await readLoggedUser()).toEqual({ user: null, networkFailure: true });
});

test.each([401, 403])('HTTP %i is an authoritative answer, not a network failure', async (code) => {
  status = code;
  const error = await sdkError(() => auth.getLoggedInUser());
  expect(error).toMatchObject({ httpStatus: code });
  expect(isNetworkFailure(error)).toBe(false);
  expect(await readLoggedUser()).toEqual({ user: null, networkFailure: false });
});

test('a live session returns the user', async () => {
  expect(await readLoggedUser()).toEqual({ user: 'cashier@ury.com', networkFailure: false });
});

test('the RPC path used by the opening check has the same shape', async () => {
  transport.defaults.baseURL = deadBase;
  expect(isNetworkFailure(await sdkError(() => call.get('ury.ury_pos.api.getPosProfile')))).toBe(true);
  transport.defaults.baseURL = liveBase;
  status = 403;
  expect(isNetworkFailure(await sdkError(() => call.get('ury.ury_pos.api.getPosProfile')))).toBe(false);
});

test('an unrelated TypeError is not treated as offline', () => {
  expect(isNetworkFailure(new TypeError("Cannot read properties of undefined (reading 'data')"))).toBe(false);
  expect(isNetworkFailure(new Error('Network Error'))).toBe(false);
  expect(isNetworkFailure({ httpStatus: 0 })).toBe(false);
});
