/**
 * Onboarding / guided-tour persistence.
 *
 * State lives in localStorage (non-sensitive, UI-only) so a returning user is
 * not re-shown a tour they have already completed or skipped. Mirrors the
 * defensive read style of utils/auth.ts (never throw on malformed JSON).
 */

const KEY = 'wt_onboarding';
const VERSION = 1;

export type GlobalTourStatus = 'pending' | 'done' | 'skipped';

/** Per-feature tutorial progress (e.g. functions, streaming, ai_workflow). */
export interface TutorialState {
  /** User completed the full interactive sandbox tutorial for this feature. */
  full?: boolean;
  /** User saw the contextual mini-tutorial for this feature. */
  mini?: boolean;
}

export interface OnboardingState {
  v: number;
  /** Set the first time a brand-new account is created (register / OAuth new user). */
  firstLogin: boolean;
  /** Lifecycle of the Tier-1 global interactive tour. */
  globalStatus: GlobalTourStatus;
  /** Section (Tier-2) tour ids the user has already seen. */
  sectionsSeen: string[];
  /** Per-feature advanced-tutorial progress, keyed by feature id. */
  tutorials: Record<string, TutorialState>;
  /** Hard opt-out: never auto-start any tour again. */
  dismissedForever: boolean;
  /** (v3 discover-by-default) One-time in-place hint ids already surfaced. */
  surfaced: string[];
  /** (v3) The first-login sidebar map has been collapsed to the normal rail. */
  sidebarMapSeen: boolean;
  /**
   * Guided-tour ids the user has finished OR skipped. Unlike `sectionsSeen`,
   * this is NOT tied to `firstLogin`: a tour explains one dense screen, so it
   * auto-runs the first time ANY user opens that screen (existing accounts
   * included) and never again. Replaying is explicit.
   */
  toursSeen: string[];
}

const DEFAULT_STATE: OnboardingState = {
  v: VERSION,
  firstLogin: false,
  globalStatus: 'pending',
  sectionsSeen: [],
  tutorials: {},
  dismissedForever: false,
  surfaced: [],
  sidebarMapSeen: false,
  toursSeen: [],
};

export function getOnboarding(): OnboardingState {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULT_STATE };
    const parsed = JSON.parse(raw) as Partial<OnboardingState>;
    // Merge over defaults so older/partial blobs gain new fields safely.
    return {
      ...DEFAULT_STATE,
      ...parsed,
      sectionsSeen: Array.isArray(parsed.sectionsSeen) ? parsed.sectionsSeen : [],
      tutorials: parsed.tutorials && typeof parsed.tutorials === 'object' ? parsed.tutorials : {},
      surfaced: Array.isArray(parsed.surfaced) ? parsed.surfaced : [],
      toursSeen: Array.isArray(parsed.toursSeen) ? parsed.toursSeen : [],
    };
  } catch {
    return { ...DEFAULT_STATE };
  }
}

function write(state: OnboardingState): OnboardingState {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    // localStorage can be unavailable (private mode / quota). The tour still
    // works in-memory for the current session; we just can't persist.
  }
  return state;
}

function patch(updates: Partial<OnboardingState>): OnboardingState {
  return write({ ...getOnboarding(), ...updates });
}

/** Mark a freshly-created account so the global tour auto-starts once. */
export function markFirstLogin(): OnboardingState {
  return patch({ firstLogin: true, globalStatus: 'pending' });
}

export function setGlobalStatus(status: GlobalTourStatus): OnboardingState {
  return patch({ globalStatus: status });
}

// ── Per-feature advanced tutorials ──────────────────────────────────────────

export function getTutorial(featureId: string): TutorialState {
  return getOnboarding().tutorials[featureId] || {};
}

function patchTutorial(featureId: string, updates: TutorialState): OnboardingState {
  const current = getOnboarding();
  return patch({
    tutorials: { ...current.tutorials, [featureId]: { ...current.tutorials[featureId], ...updates } },
  });
}

/** Record that the user completed the full interactive sandbox for a feature. */
export function markTutorialFull(featureId: string): OnboardingState {
  return patchTutorial(featureId, { full: true });
}

/** Record that the user saw the contextual mini-tutorial for a feature. */
export function markTutorialMini(featureId: string): OnboardingState {
  return patchTutorial(featureId, { mini: true });
}

export function hasTutorialFull(featureId: string): boolean {
  return !!getTutorial(featureId).full;
}

/**
 * A contextual mini-tutorial should auto-fire only if the user hasn't already
 * seen the full sandbox tutorial for this feature, hasn't seen the mini, and
 * hasn't opted out of onboarding entirely.
 */
export function shouldShowMini(featureId: string): boolean {
  if (getOnboarding().dismissedForever) return false;
  const tu = getTutorial(featureId);
  return !tu.full && !tu.mini;
}

/**
 * A guided tour auto-runs once per user, on the first visit to the screen it
 * explains. `dismissedForever` (the global opt-out) still wins.
 */
export function shouldAutoRunTour(id: string): boolean {
  const s = getOnboarding();
  return !s.dismissedForever && !s.toursSeen.includes(id);
}

export function markTourSeen(id: string): void {
  const s = getOnboarding();
  if (s.toursSeen.includes(id)) return;
  patch({ toursSeen: [...s.toursSeen, id] });
}

export function hasSeenSection(id: string): boolean {
  return getOnboarding().sectionsSeen.includes(id);
}

export function markSectionSeen(id: string): OnboardingState {
  const current = getOnboarding();
  if (current.sectionsSeen.includes(id)) return current;
  return patch({ sectionsSeen: [...current.sectionsSeen, id] });
}

export function setDismissedForever(value: boolean): OnboardingState {
  return patch({ dismissedForever: value });
}

/** Wipe onboarding progress so every tour can be replayed from scratch. */
export function resetOnboarding(): OnboardingState {
  return write({ ...DEFAULT_STATE, firstLogin: getOnboarding().firstLogin });
}

// ── (v3) Discover-by-default: sidebar map + one-time in-place hints ──────────

/** First-run surfaces auto-show only while this is true. */
export function isOnboardingActive(): boolean {
  const s = getOnboarding();
  return s.firstLogin && !s.dismissedForever;
}

export function hasSurfaced(id: string): boolean {
  return getOnboarding().surfaced.includes(id);
}

export function markSurfaced(id: string): void {
  const s = getOnboarding();
  if (s.surfaced.includes(id)) return;
  patch({ surfaced: [...s.surfaced, id] });
}

export function hasSeenSidebarMap(): boolean {
  return getOnboarding().sidebarMapSeen;
}

export function markSidebarMapSeen(): void {
  if (getOnboarding().sidebarMapSeen) return;
  patch({ sidebarMapSeen: true });
}
