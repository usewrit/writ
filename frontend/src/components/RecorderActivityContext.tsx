import React, { createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ConfirmDialog } from './ConfirmDialog';

/**
 * Tracks whether any BrowserRecorder currently holds a LIVE browser session
 * (connecting/connected/recording) that would be torn down — losing unsaved
 * steps and the running browser — if the user navigates away.
 *
 * The recorder is rendered deep inside page content while nav lives in the
 * sidebar / command palette; both sit under this provider (mounted in AppLayout)
 * so any of them can route a jump through `guardedNavigate`, which confirms
 * first while a recording is live. The confirm dialog is owned here so every
 * entry point shares one guard instead of duplicating dialog + pending state.
 * Ref-counted by a stable per-recorder id so multiple recorders can't clobber
 * each other.
 */
interface RecorderActivityValue {
  /** True while ≥1 recorder has a live browser session in progress. */
  active: boolean;
  /** A recorder registers/deregisters its live-session state under a stable id. */
  setRecorderActive: (id: string, active: boolean) => void;
  /** Navigate to `to`, but if a recording is live (and `to` isn't the current
   *  page), confirm first. Otherwise navigates immediately. */
  guardedNavigate: (to: string) => void;
}

const noop = () => {};
const FALLBACK: RecorderActivityValue = { active: false, setRecorderActive: noop, guardedNavigate: noop };

const RecorderActivityContext = createContext<RecorderActivityValue | null>(null);

export const RecorderActivityProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const activeIds = useRef<Set<string>>(new Set());
  const [active, setActive] = useState(false);
  const [pendingHref, setPendingHref] = useState<string | null>(null);

  const setRecorderActive = useCallback((id: string, isActive: boolean) => {
    const ids = activeIds.current;
    if (isActive) ids.add(id);
    else ids.delete(id);
    setActive(ids.size > 0);
  }, []);

  const guardedNavigate = useCallback(
    (to: string) => {
      if (active && to !== location.pathname) setPendingHref(to);
      else navigate(to);
    },
    [active, location.pathname, navigate],
  );

  const value = useMemo<RecorderActivityValue>(
    () => ({ active, setRecorderActive, guardedNavigate }),
    [active, setRecorderActive, guardedNavigate],
  );

  return (
    <RecorderActivityContext.Provider value={value}>
      {children}
      {/* Leaving-while-recording guard. A live recorder tears down its browser
          session (and any unsaved steps) the instant its page unmounts, so we
          hold the nav until the user confirms. */}
      <ConfirmDialog
        isOpen={pendingHref !== null}
        onClose={() => setPendingHref(null)}
        onConfirm={() => { const href = pendingHref; setPendingHref(null); if (href) navigate(href); }}
        title={t('Leave and stop recording?')}
        message={t("A browser recording is still running. Leaving this page will stop the live browser session and discard any steps you haven't saved yet.")}
        confirmText={t('Leave & stop recording')}
        cancelText={t('Stay on this page')}
        variant="warning"
      />
    </RecorderActivityContext.Provider>
  );
};

/** Read recording state + the guarded navigator. Safe (inert) with no provider. */
export const useRecorderActivity = (): RecorderActivityValue =>
  useContext(RecorderActivityContext) ?? FALLBACK;

/**
 * Called by a BrowserRecorder to publish whether it holds a live browser
 * session. Registers on change and always clears on unmount, so an abrupt
 * teardown can't leave the guard stuck "active".
 */
export const useRegisterRecorderActivity = (active: boolean): void => {
  const { setRecorderActive } = useRecorderActivity();
  const id = useId();
  useEffect(() => {
    setRecorderActive(id, active);
    return () => setRecorderActive(id, false);
  }, [id, active, setRecorderActive]);
};
