import { FrappeApp } from "frappe-js-sdk";

export function createFrappeClient(baseUrl: string | undefined = import.meta.env?.VITE_FRAPPE_BASE_URL) {
  return new FrappeApp(baseUrl as any);
}

const frappe = createFrappeClient();

// frappe-js-sdk 1.14 reads error.response.data in every catch, so a request
// that never got an HTTP answer surfaces as a TypeError. Give axios
// ERR_NETWORK failures a status-0 response tagged ury_network_failure so
// callers can tell "no answer" from an authoritative 401/403. Aborts and
// timeouts are not tagged.
export const NETWORK_FAILURE_MARK = 'ury_network_failure';
export const transport = (frappe as unknown as { axios: import('axios').AxiosInstance }).axios;
transport.interceptors.response.use(undefined, (error) => {
  if (error && typeof error === 'object' && !error.response && error.code === 'ERR_NETWORK') {
    error.response = {
      status: 0,
      statusText: 'Network Error',
      data: { [NETWORK_FAILURE_MARK]: true },
      headers: {},
      config: error.config,
    };
  }
  return Promise.reject(error);
});
const frappeCall = frappe.call();

export interface CallFunction {
  <T = any>(path: string, params?: any): Promise<T>;
  get: typeof frappeCall.get;
  post: typeof frappeCall.post;
  put: typeof frappeCall.put;
  delete: typeof frappeCall.delete;
}

const callImpl: any = (path: string, params?: any) => frappeCall.post(path, params);
callImpl.get = frappeCall.get.bind(frappeCall);
callImpl.post = frappeCall.post.bind(frappeCall);
callImpl.put = frappeCall.put.bind(frappeCall);
callImpl.delete = frappeCall.delete.bind(frappeCall);

export const call: CallFunction = callImpl;
export const db = frappe.db();
export const auth = frappe.auth();
