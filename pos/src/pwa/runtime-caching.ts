export const READ_CACHE = "ury-pos-reads";
export const SHELL_CACHE = "ury-pos-shell";

export interface OfflineCaptureProfile {
  ury_offline_capture?: unknown;
}

export interface RuntimeCacheRoute {
  urlPattern: RegExp;
  handler: "NetworkFirst";
  method: "GET";
  options: {
    cacheName: string;
    networkTimeoutSeconds: number;
    cacheableResponse: { statuses: number[] };
    expiration: { maxEntries: number };
  };
}

// Workbox tests the full request URL. Patterns are self-contained so the
// generated service worker does not close over this module.
const MENU = /^https?:\/\/[^/]+\/api\/method\/ury\.ury_pos\.api\.getRestaurantMenu(?:\?|#|$)/;
const POS_PROFILE_METHOD = /^https?:\/\/[^/]+\/api\/method\/ury\.ury_pos\.api\.getPosProfile(?:\?|#|$)/;
const PAYMENT_MODES = /^https?:\/\/[^/]+\/api\/method\/ury\.ury_pos\.api\.getModeOfPayment(?:\?|#|$)/;
const TABLE_RESOURCE = /^https?:\/\/[^/]+\/api\/resource\/URY(?:%20| )Table(?:\/|\?|#|$)/;
const POS_PROFILE_RESOURCE = /^https?:\/\/[^/]+\/api\/resource\/POS(?:%20| )Profile(?:\/|\?|#|$)/;
const TABLE_LIST = /^https?:\/\/[^/]+\/api\/method\/frappe\.client\.get_list\?(?:[^#]*&)?doctype=URY(?:\+|%20)Table(?:&|#|$)/;
const POS_SHELL = /^https?:\/\/[^/]+\/pos(?:\/|\?|#|$)/;

function route(urlPattern: RegExp, cacheName: string): RuntimeCacheRoute {
  return {
    urlPattern,
    handler: "NetworkFirst",
    method: "GET",
    options: {
      cacheName,
      networkTimeoutSeconds: 3,
      cacheableResponse: { statuses: [200] },
      expiration: { maxEntries: cacheName === SHELL_CACHE ? 8 : 60 },
    },
  };
}

export function workboxRuntimeCaching(): RuntimeCacheRoute[] {
  return [
    route(MENU, READ_CACHE),
    route(POS_PROFILE_METHOD, READ_CACHE),
    route(PAYMENT_MODES, READ_CACHE),
    route(TABLE_RESOURCE, READ_CACHE),
    route(POS_PROFILE_RESOURCE, READ_CACHE),
    route(TABLE_LIST, READ_CACHE),
    route(POS_SHELL, SHELL_CACHE),
  ];
}

export function offlineCaptureEnabled(profile: OfflineCaptureProfile | null | undefined): boolean {
  const flag = profile?.ury_offline_capture;
  return flag === 1 || flag === "1" || flag === true;
}

function requestUrls(input: string): string[] {
  const href = new URL(input).href;
  return href === input ? [href] : [input, href];
}

export function matchingRuntimeRoute(input: string, method: string): RuntimeCacheRoute | undefined {
  const urls = requestUrls(input);
  return workboxRuntimeCaching().find((entry) => (
    entry.method === method && urls.some((url) => entry.urlPattern.test(url))
  ));
}

export function requestMatchesRuntimeCache(input: string, method: string): boolean {
  return matchingRuntimeRoute(input, method) !== undefined;
}

export function runtimeCacheNameFor(input: string, method: string): string | undefined {
  return matchingRuntimeRoute(input, method)?.options.cacheName;
}
