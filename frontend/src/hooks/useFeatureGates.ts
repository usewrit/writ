/**
 * Self-host build: there is no platform admin and no per-org feature gating —
 * the single owner has every feature. These are kept as thin shims so the
 * (few) call sites that still consult a gate map compile and always read
 * "enabled" without a network round-trip.
 */
export function useFeatureGates(): { gates: Record<string, boolean>; loading: boolean } {
  return { gates: {}, loading: false };
}

/** Everything is available in the self-host build. */
export function gateEnabled(_gates: Record<string, boolean>, _id: string): boolean {
  return true;
}
