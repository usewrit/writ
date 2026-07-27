import { useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { getAuth, isAuthenticated, clearAuth } from '../utils/auth';
import { resetPreferencesBoot } from '../utils/preferences';
import { useQueryCache } from '../stores/queryCache';
import client from '../api/client';

/**
 * Sign out, on the server as well as in this tab.
 *
 * The server call is the part that matters. The access token lives only in
 * memory, so dropping it locally is enough to end THIS page's session — but the
 * refresh token is an httpOnly cookie scoped to /api/auth, which JavaScript
 * cannot touch. Clearing local state alone leaves that cookie live and
 * redeemable: anyone with the browser afterwards can POST /api/auth/refresh and
 * get a fresh access token with full owner rights, even though the UI is sitting
 * on the login screen.
 *
 * POST /auth/logout blacklists the presented refresh and access tokens and
 * deletes the cookies. It is best-effort — if the network is down we still clear
 * everything locally rather than trapping the user in a session they asked to
 * leave.
 */
export const performLogout = async (): Promise<void> => {
  try {
    await client.post('/auth/logout');
  } catch {
    /* best-effort revocation — always clear locally regardless */
  }
  clearAuth();
  resetPreferencesBoot();
  useQueryCache.getState().clear();
};

export const useAuth = () => {
  const navigate = useNavigate();
  const auth = getAuth();

  const logout = async () => {
    await performLogout();
    navigate('/login');
  };

  return {
    ...auth,
    isAuthenticated: isAuthenticated(),
    logout,
  };
};

export const useRequireAuth = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const auth = getAuth();

  useEffect(() => {
    if (!isAuthenticated()) {
      // Never point the redirect back at an auth route — that self-nests the
      // ?redirect param into an ever-growing /login?redirect=/login?redirect=… loop.
      const currentPath = location.pathname + location.search;
      const isAuthRoute = location.pathname === '/login' || location.pathname === '/setup';
      const redirectParam =
        currentPath !== '/' && !isAuthRoute ? `?redirect=${encodeURIComponent(currentPath)}` : '';
      navigate(`/login${redirectParam}`, { replace: true });
    }
  }, [navigate, location.pathname, location.search]);

  return auth;
};
