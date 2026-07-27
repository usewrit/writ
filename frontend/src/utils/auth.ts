/**
 * JWT-based authentication utilities (single-owner self-host build).
 *
 * Access token is held in MEMORY ONLY — never persisted to a JS-readable cookie
 * or localStorage. This keeps the bearer token out of reach of XSS / theft. On
 * a hard reload the in-memory token is gone; the app silently re-mints it from
 * the httpOnly refresh cookie via the 401-refresh interceptor in api/client.ts.
 *
 * Refresh token stored as httpOnly cookie by the coordinator (inaccessible to
 * JS). User display data in localStorage (non-sensitive, UI display only).
 *
 * There are no organizations/plans in the self-host build. `OrgData`/`getOrg`
 * are kept as inert shims so the (few) call sites that still reference them
 * compile; `getOrg()` always returns null.
 */

const USER_KEY = 'user_data';

// Access token — memory only. NOT persisted (no cookie / no localStorage), so a
// hard reload drops it and the refresh-cookie path re-mints it.
let _accessToken: string | null = null;

export interface UserData {
  id: string;
  email: string;
  name: string | null;
  is_verified: boolean;
  is_platform_admin: boolean;
}

// Kept only so legacy call sites (Settings) that destructure an org object still
// typecheck. No organizations exist in the self-host build.
export interface OrgData {
  id: string;
  name: string;
  slug: string;
}

export const setAuth = (accessToken: string, user: UserData, _org?: OrgData | null) => {
  _accessToken = accessToken; // memory only — refresh cookie re-mints on reload
  localStorage.setItem(USER_KEY, JSON.stringify(user));
};

// Refresh the cached user display data (e.g. from a fresh /auth/me response)
// without touching the access token.
export const setUserOrg = (user: UserData, _org?: OrgData | null) => {
  localStorage.setItem(USER_KEY, JSON.stringify(user));
};

export const getAccessToken = (): string | null => {
  return _accessToken;
};

export const setAccessToken = (token: string) => {
  _accessToken = token; // memory only
};

// Single-flight refresh. The refresh token is single-use: the coordinator
// ROTATES it (blacklists the presented jti, issues a fresh one) on every
// /auth/refresh. Sharing ONE in-flight promise means only one rotation happens
// and all callers reuse its result. Never throws; resolves to the token (or null).
let _refreshPromise: Promise<string | null> | null = null;
export const refreshAccessToken = (): Promise<string | null> => {
  if (_refreshPromise) return _refreshPromise;
  _refreshPromise = (async () => {
    try {
      const resp = await fetch('/api/auth/refresh', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      if (!resp.ok) return null;
      const data = await resp.json();
      const token = typeof data?.access_token === 'string' ? data.access_token : null;
      if (token) _accessToken = token;
      return token;
    } catch {
      return null;
    } finally {
      _refreshPromise = null;
    }
  })();
  return _refreshPromise;
};

// Boot-time priming: after a hard reload the in-memory access token is gone.
// Most callers go through the axios client, whose 401-refresh interceptor
// re-mints transparently — but a few (e.g. the recorder's WS-ticket fetch) read
// getAccessToken() directly via raw fetch. So if we have a persisted prior
// session but no token yet, silently re-mint once from the httpOnly refresh
// cookie via the shared single-flight above. Never throws.
export const primeAccessToken = (): Promise<string | null> => {
  if (_accessToken) return Promise.resolve(_accessToken);
  if (getUser() === null) return Promise.resolve(null); // no prior session
  return refreshAccessToken();
};

export const getUser = (): UserData | null => {
  try {
    const data = localStorage.getItem(USER_KEY);
    return data ? JSON.parse(data) : null;
  } catch {
    return null;
  }
};

// No organizations in the self-host build.
export const getOrg = (): OrgData | null => null;

export const getAuth = (): { apiKey: string | null; role: 'admin' | 'viewer' | null } => {
  return { apiKey: _accessToken, role: 'admin' };
};

// Per-user localStorage keys that must NOT outlive a logout — extracted-data
// field/column names and dismissed failed-run IDs. Matched by PREFIX so
// id-suffixed keys (writ.dataKey.<workflowId>, …) are swept without enumerating
// ids. Device-level UI prefs (language, onboarding, list view/sort) are NOT
// listed: they carry no data and survive logout. (Concierge / connected-apps
// keys don't exist in the self-host build, but listing their prefixes is a
// harmless no-op and keeps this in sync with the cloud sweep.)
const PER_USER_STORAGE_PREFIXES = [
  'concierge_',
  'writ.dataKey.',        // extracted-data identity field names
  'writ.dataCols.',       // extracted-data visible column names
  'writ:needsAttention',  // dismissed failed-run IDs
  'connected_apps:',
];

// Exact keys from OLDER builds that current code no longer references — purge on
// logout so nothing (incl. an early-build access token under 'token') lingers.
const LEGACY_STORAGE_KEYS = ['organization', 'token', 'recorder_auth_token'];

const clearPerUserStorage = () => {
  try {
    // Collect first, then delete: removing during the length-indexed walk would
    // renumber the remaining keys and skip some.
    const doomed: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && PER_USER_STORAGE_PREFIXES.some((p) => k.startsWith(p))) doomed.push(k);
    }
    doomed.forEach((k) => localStorage.removeItem(k));
    LEGACY_STORAGE_KEYS.forEach((k) => localStorage.removeItem(k));
  } catch {
    /* localStorage unavailable — nothing to sweep. */
  }
  try {
    sessionStorage.clear();
  } catch {
    /* sessionStorage unavailable. */
  }
};

export const clearAuth = () => {
  _accessToken = null;
  localStorage.removeItem(USER_KEY);
  clearPerUserStorage();
};

export const isAuthenticated = (): boolean => {
  // The access token is memory-only and is gone after a hard reload, so we key
  // "is the user logged in?" off persisted (non-sensitive) user data from a
  // prior login: the first authenticated API call transparently re-mints the
  // access token from the httpOnly refresh cookie, or cleanly redirects to
  // /login if that cookie has expired. clearAuth() wipes the user data, so an
  // explicit logout still reads as unauthenticated.
  return _accessToken !== null || getUser() !== null;
};

export const hasRole = (_requiredRole: 'admin' | 'viewer'): boolean => {
  return isAuthenticated();
};

// Single owner — always the admin.
export const isPlatformAdmin = (): boolean => isAuthenticated();
