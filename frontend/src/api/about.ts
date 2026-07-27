import client from './client';

// ─────────────────────────────────────────────────────────────────────────────
// About — instance identity + the AGPL-3.0 §13 source offer.
//
// Backed by the coordinator's PUBLIC `GET /api/about` (coordinator/main.py), so
// it resolves before login: the network source offer has to be visible to
// anyone interacting with the program, not only to the owner.
//
// `source_url` is operator-configurable (WRIT_SOURCE_URL). On a modified build
// it points at the operator's own fork, which is what §13 actually requires —
// so never hardcode the upstream repo in the UI; always render this value.
// ─────────────────────────────────────────────────────────────────────────────

export interface AboutInfo {
  name: string;
  version: string;
  license: string;
  license_url: string;
  source_url: string;
  /** True when the operator repointed WRIT_SOURCE_URL away from upstream. */
  modified: boolean;
}

export const getAbout = async (): Promise<AboutInfo> => {
  const r = await client.get('/about');
  return r.data;
};
