/**
 * Compat shim.
 *
 * The v1 guided-tour engine (spotlights + sandbox simulation) was retired in
 * favour of the discover-by-default model — the sidebar map (Layout) plus the
 * one-time in-place hints (SurfacerProvider). A few list pages and Settings
 * still call `useTour().startTour` / `startFullTutorial`; those are now no-ops.
 * This thin shim keeps those call sites compiling unchanged. Mount
 * `SurfacerProvider` (onboarding/Surfacer) at the app root instead of this.
 */
export function useTour(): {
  startTour: (id: string) => void;
  startFullTutorial: (featureId: string) => void;
} {
  return { startTour: () => {}, startFullTutorial: () => {} };
}
