import axios, { AxiosInstance, AxiosError, InternalAxiosRequestConfig } from 'axios';
import { getAccessToken, setAccessToken, clearAuth, primeAccessToken, refreshAccessToken } from '../utils/auth';
import i18n from '../i18n';

// Boot-time token prime: the access token is memory-only, so after a hard reload
// it must be re-minted from the httpOnly refresh cookie. The axios interceptor
// handles this for client requests, but direct getAccessToken() consumers (e.g.
// the recorder's raw-fetch WS-ticket mint) need the token to already be present.
// Fire once at module load if a prior session exists.
//
// The PROMISE is kept (it used to be `void`-ed). Priming is async, so every query
// the first render fires went out before it resolved — with no Authorization header
// at all. They each 401'd, each fell into the refresh interceptor below, and each
// was retried: the app worked, but a reload printed a wall of red 401s in the
// console and every boot request was sent twice. Awaiting the in-flight prime in
// the request interceptor makes the first burst carry the token instead.
const bootPrime: Promise<string | null> = primeAccessToken();

// Unauthenticated auth-flow endpoints: a 401 here means "bad credentials /
// bad code", not "session expired". They must NOT trigger the token-refresh +
// redirect dance below — that reloads the page and wipes the error message
// the login/MFA forms are about to show.
const AUTH_FLOW_PATHS = [
  '/auth/login',
  '/auth/refresh',
  '/auth/register',       // first-run onboarding — 403 = "owner exists", not a session issue
  '/auth/setup-status',   // public first-run probe
];
const isAuthFlowRequest = (url?: string) =>
  !!url && AUTH_FLOW_PATHS.some((p) => url.startsWith(p));

const client: AxiosInstance = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Request interceptor: attach the JWT access token from memory.
//
// Async on purpose. When the token is not in memory yet we wait for the boot prime
// (a single-flight refresh shared with everything else, so this adds no extra
// round-trip) rather than sending an anonymous request that is guaranteed to 401.
// Once primed, `getAccessToken()` is a hit and the await resolves immediately — an
// already-settled promise, so steady-state requests are not delayed.
//
// Auth-flow endpoints are skipped: /auth/login and friends are how a session is
// ESTABLISHED, so blocking them on "do we have a session?" would be circular, and
// /auth/refresh in particular must never wait on a refresh.
client.interceptors.request.use(
  async (config: InternalAxiosRequestConfig) => {
    let token = getAccessToken();
    if (!token && !isAuthFlowRequest(config.url)) {
      token = await bootPrime.catch(() => null);
    }
    if (token && config.headers) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// Response interceptor: handle 401 with token refresh
client.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as InternalAxiosRequestConfig & { _retry?: boolean };

    if (isAuthFlowRequest(originalRequest?.url)) {
      // Let the auth pages handle their own errors (wrong password,
      // invalid MFA code, expired reset token, ...).
      return Promise.reject(error);
    }

    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true;

      // Refresh token is in the httpOnly cookie (sent automatically). Go through
      // the SHARED single-flight refresh so the burst of parallel 401s at boot
      // collapses into ONE rotation — otherwise each call rotates the single-use
      // refresh token and invalidates the others, logging the user out.
      const newToken = await refreshAccessToken();
      if (newToken) {
        if (originalRequest.headers) {
          originalRequest.headers.Authorization = `Bearer ${newToken}`;
        }
        return client(originalRequest);
      }
      clearAuth();
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login';
      }
      return Promise.reject(error);
    }

    if (error.response?.status === 401) {
      clearAuth();
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login';
      }
    }

    return Promise.reject(error);
  }
);

export default client;

/**
 * Extract a human-readable message from any API/JS error.
 *
 * Order: backend `detail` (FastAPI standard, including the sanitized
 * "(ref: xxx)" messages) → `error` → `message` → validation-error array →
 * status-based fallback. Use everywhere instead of ad-hoc
 * `err?.response?.data?.detail || '...'` chains.
 */
export function apiErrorMessage(err: any, fallback = i18n.t('Something went wrong')): string {
  const resp = err?.response;
  if (resp) {
    const data = resp.data;
    const detail = data?.detail ?? data?.error ?? data?.message;
    if (typeof detail === 'string' && detail) return detail;
    if (Array.isArray(detail) && detail[0]?.msg) {
      // FastAPI 422 validation errors: [{loc, msg, type}, ...]
      const loc = Array.isArray(detail[0].loc) ? detail[0].loc.slice(1).join('.') : '';
      return loc ? `${loc}: ${detail[0].msg}` : detail[0].msg;
    }
    if (resp.status >= 500) return i18n.t('Server error ({{status}}). Please try again.', { status: resp.status });
    return fallback;
  }
  if (err?.code === 'ERR_NETWORK' || err?.message === 'Network Error') {
    return i18n.t('Network error — check your connection.');
  }
  if (err?.code === 'ECONNABORTED') {
    return i18n.t('Request timed out. Please try again.');
  }
  return (typeof err?.message === 'string' && err.message) || fallback;
}

/**
 * Returns the structured object `detail` from an API error (e.g. a 409
 * {code, listing_id, slug, install_count}), or null when detail is a string /
 * a validation array / absent. Use this when the backend returns a machine-
 * readable error body the UI must branch on (vs. apiErrorMessage for display).
 */
export function apiErrorDetail<T = any>(err: any): T | null {
  const d = err?.response?.data?.detail;
  return d && typeof d === 'object' && !Array.isArray(d) ? (d as T) : null;
}

// Legacy compat
export const apiClient = {
  setApiKey: (key: string) => {
    setAccessToken(key); // Memory only
  },
  getApiKey: () => getAccessToken(),
  clearAuth: () => clearAuth(),
  getClient: () => client,
};
