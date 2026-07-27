/**
 * The hint registry — one shared surfacer primitive, N curated one-time moments.
 *
 * Each entry is an anchor + copy. Adding a hint is adding a row here, not
 * building a tour. `anchor` matches a `data-surface="<anchor>"` element mounted
 * by the component that owns the moment. Keep this list SMALL: a hint must be
 * genuinely non-obvious, load-bearing, and fire at one exact moment.
 *
 * Copy is intentionally kept here (not i18n keys) so the whole registry reads as
 * one voice in one place; wrap at the render site if localization is needed.
 */
export interface Hint {
  id: string;
  /** `data-surface="<anchor>"` the hint points at. */
  anchor: string;
  title: string;
  body: string;
}

export const HINTS: Record<string, Hint> = {
  callable: {
    id: 'callable',
    anchor: 'callable',
    title: "It's live — call it from anywhere.",
    body: 'This is now a real endpoint. Hit it over REST, hand it to an AI as an MCP tool, or run it on a schedule.',
  },
  autorepair: {
    id: 'autorepair',
    anchor: 'autorepair',
    title: 'It fixed itself.',
    body: 'The page changed, so Writ rewrote this step and kept going — no failed run, no alert for you.',
  },
};

export function getHint(id: string): Hint | undefined {
  return HINTS[id];
}
