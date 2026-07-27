/**
 * Decoupled trigger for one-time in-place hints.
 *
 * A component asks for a hint to surface without importing the provider or its
 * context. The SurfacerProvider listens and decides whether to actually show it
 * (it won't if onboarding is inactive, the hint was already surfaced, or another
 * hint is on screen). Requesting a hint whose id isn't in the registry is a
 * no-op.
 */
export const SURFACE_EVENT = 'ps-onboarding:surface';

export function requestSurface(id: string): void {
  window.dispatchEvent(new CustomEvent(SURFACE_EVENT, { detail: { id } }));
}
