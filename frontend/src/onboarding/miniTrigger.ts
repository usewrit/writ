/**
 * Compat shim — the contextual mini-tutorials were retired with the v1 tour
 * engine. Kept as a no-op so existing call sites compile unchanged. The
 * discover-by-default model surfaces hints via onboarding/surfaceTrigger.
 */
export function triggerMini(_featureId: string): void {
  /* no-op */
}
