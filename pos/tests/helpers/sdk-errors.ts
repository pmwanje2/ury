// Errors exactly as the real @ury/core client + frappe-js-sdk produce them,
// over real sockets (a refused port, and a local server answering 403).
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import type { AxiosInstance } from 'axios';
import { auth, call } from '../../../packages/core/src/frappe/client';

const transport = (auth as unknown as { axios: AxiosInstance }).axios;

async function failWith(baseURL: string): Promise<unknown> {
  const previous = { baseURL: transport.defaults.baseURL, adapter: transport.defaults.adapter };
  transport.defaults.baseURL = baseURL;
  transport.defaults.adapter = 'fetch';
  try {
    await call.get('erpnext.selling.page.point_of_sale.point_of_sale.check_opening_entry');
  } catch (error) {
    return error;
  } finally {
    transport.defaults.baseURL = previous.baseURL;
    transport.defaults.adapter = previous.adapter;
  }
  throw new Error('expected the SDK call to fail');
}

export async function realSdkNetworkFailure(): Promise<unknown> {
  const dead = http.createServer();
  await new Promise<void>((done) => dead.listen(0, '127.0.0.1', done));
  const base = 'http://127.0.0.1:' + (dead.address() as AddressInfo).port;
  await new Promise<void>((done) => dead.close(() => done()));
  return failWith(base);
}

export async function realSdkHttpFailure(status: number): Promise<unknown> {
  const server = http.createServer((_request, response) => {
    response.statusCode = status;
    response.setHeader('content-type', 'application/json');
    response.end(JSON.stringify({ exc_type: 'PermissionError' }));
  });
  await new Promise<void>((done) => server.listen(0, '127.0.0.1', done));
  try {
    return await failWith('http://127.0.0.1:' + (server.address() as AddressInfo).port);
  } finally {
    await new Promise<void>((done) => server.close(() => done()));
  }
}
