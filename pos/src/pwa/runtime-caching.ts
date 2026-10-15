export const READ_CACHE = "ury-pos-reads";
export const SHELL_CACHE = "ury-pos-shell";

export interface OfflineCaptureProfile {
  ury_offline_capture?: unknown;
}

const READ_METHODS = new Set([
  "/api/method/ury.ury_pos.api.getRestaurantMenu",
  "/api/method/ury.ury_pos.api.getPosProfile",
  "/api/method/ury.ury_pos.api.getModeOfPayment",
]);
const TABLE_LIST = "/api/method/frappe.client.get_list";
const TABLE_RESOURCE = "/api/resource/URY Table";
const PROFILE_RESOURCE = "/api/resource/POS Profile";

function decodedPath(url: URL): string | null {
  try {
    return decodeURIComponent(url.pathname);
  } catch {
    return null;
  }
}

// Match the path the browser will actually use. An encoded slash such as
// /pos%2F..%2Fsecret decodes to a /pos/ prefix and would otherwise hit the
// shell cache; dot-segments that decoding invents are not that path.
function canonicalPath(url: URL): string | null {
  const decoded = decodedPath(url);
  if (!decoded || decoded.includes("\\") || decoded.includes("\0")) return null;
  let normalized: string;
  try {
    normalized = decodeURIComponent(new URL(decoded, url.origin).pathname);
  } catch {
    return null;
  }
  if (normalized !== decoded) return null;
  return normalized;
}

function isDocResource(path: string, base: string): boolean {
  if (path === base) return true;
  if (!path.startsWith(base + "/")) return false;
  const name = path.slice(base.length + 1);
  return name.length > 0 && !name.includes("/");
}

// Frappe's form_dict keeps the first of duplicated query keys, so a repeated
// key can make the server run a different read than the URL suggests. Only
// unambiguous same-origin GETs on the named reads are cacheable.
export function matchOfflineRequest(url: URL, method: string, origin: string): string | undefined {
  if (method !== "GET" || url.origin !== origin) return undefined;
  const keys = [...url.searchParams.keys()];
  if (new Set(keys).size !== keys.length || url.searchParams.has("cmd")) return undefined;
  const path = canonicalPath(url);
  if (!path) return undefined;
  if (path === "/pos" || path.startsWith("/pos/")) return SHELL_CACHE;
  if (path === TABLE_LIST) {
    return url.searchParams.get("doctype") === "URY Table" ? READ_CACHE : undefined;
  }
  if (url.searchParams.has("doctype")) return undefined;
  if (READ_METHODS.has(path)) return READ_CACHE;
  if (isDocResource(path, TABLE_RESOURCE) || isDocResource(path, PROFILE_RESOURCE)) return READ_CACHE;
  return undefined;
}

// The worker is served from /sw.js, outside the Vite asset directory.
// Workbox precache paths are relative to the worker script, so they must
// be rooted at the POS asset base or install 404s and never activates.
export const POS_ASSET_BASE = "/assets/ury/pos/";

export function precacheModifyURLPrefix(): Record<string, string> {
  return { "": POS_ASSET_BASE };
}

export function precacheURLForServiceWorker(relativeURL: string): string {
  if (relativeURL.startsWith("/")) return relativeURL;
  return `${precacheModifyURLPrefix()[""]}${relativeURL}`;
}

export function offlineCaptureEnabled(profile: OfflineCaptureProfile | null | undefined): boolean {
  const flag = profile?.ury_offline_capture;
  return flag === 1 || flag === "1" || flag === true;
}

export function runtimeCacheNameFor(input: string, method: string, origin: string): string | undefined {
  let url: URL;
  try {
    url = new URL(input);
  } catch {
    return undefined;
  }
  return matchOfflineRequest(url, method, origin);
}

export function requestMatchesRuntimeCache(input: string, method: string, origin: string): boolean {
  return runtimeCacheNameFor(input, method, origin) !== undefined;
}
