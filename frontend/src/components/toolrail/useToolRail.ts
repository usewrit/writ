import { useCallback, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { RailConfig } from './configs';

/**
 * Owns the rail's interactive state for one page:
 *  - active view (single select segment)
 *  - facet selections (multi-select, OR within a facet, AND across facets)
 *  - collapsed flag
 *
 * View + facets are mirrored into the URL (?view=…&type=a,b) so a filtered
 * workbench is shareable/bookmarkable, and seeded from / persisted to
 * localStorage. State is the source of truth (one-way state → URL) to avoid
 * feedback loops.
 */

type FacetState = Record<string, string[]>;

const lsGet = (k: string): string | null => {
  try { return localStorage.getItem(k); } catch { return null; }
};
const lsSet = (k: string, v: string) => {
  try { localStorage.setItem(k, v); } catch { /* ignore */ }
};

export function useToolRail(config: RailConfig, items: any[]) {
  const [searchParams, setSearchParams] = useSearchParams();
  const ns = `toolrail:${config.page}`;

  // ── initial state: URL → localStorage → default ──
  const [view, setViewState] = useState<string>(() => {
    const fromUrl = searchParams.get('view');
    if (fromUrl && config.views.some((v) => v.id === fromUrl)) return fromUrl;
    const fromLs = lsGet(`${ns}:view`);
    if (fromLs && config.views.some((v) => v.id === fromLs)) return fromLs;
    return config.views[0]?.id || 'all';
  });

  const [facetState, setFacetState] = useState<FacetState>(() => {
    const initial: FacetState = {};
    for (const f of config.facets) {
      const raw = searchParams.get(f.id) ?? lsGet(`${ns}:facet:${f.id}`);
      const ids = (raw ? raw.split(',') : []).filter((id) => f.options.some((o) => o.id === id));
      if (ids.length) initial[f.id] = ids;
    }
    return initial;
  });

  const [collapsed, setCollapsed] = useState<boolean>(() => lsGet(`${ns}:collapsed`) === '1');

  // ── writers ──
  const syncUrl = useCallback((nextView: string, nextFacets: FacetState) => {
    const next = new URLSearchParams(searchParams);
    if (nextView && nextView !== (config.views[0]?.id || 'all')) next.set('view', nextView);
    else next.delete('view');
    for (const f of config.facets) {
      const ids = nextFacets[f.id];
      if (ids && ids.length) next.set(f.id, ids.join(','));
      else next.delete(f.id);
    }
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams, config.facets, config.views]);

  const setView = useCallback((id: string) => {
    setViewState(id);
    lsSet(`${ns}:view`, id);
    syncUrl(id, facetState);
  }, [ns, facetState, syncUrl]);

  const toggleFacet = useCallback((facetId: string, optionId: string) => {
    setFacetState((prev) => {
      const cur = prev[facetId] || [];
      const nextIds = cur.includes(optionId) ? cur.filter((x) => x !== optionId) : [...cur, optionId];
      const next = { ...prev };
      if (nextIds.length) next[facetId] = nextIds; else delete next[facetId];
      lsSet(`${ns}:facet:${facetId}`, nextIds.join(','));
      syncUrl(view, next);
      return next;
    });
  }, [ns, view, syncUrl]);

  const clearAll = useCallback(() => {
    setViewState(config.views[0]?.id || 'all');
    setFacetState({});
    lsSet(`${ns}:view`, config.views[0]?.id || 'all');
    for (const f of config.facets) lsSet(`${ns}:facet:${f.id}`, '');
    syncUrl(config.views[0]?.id || 'all', {});
  }, [ns, config.views, config.facets, syncUrl]);

  const toggleCollapsed = useCallback(() => {
    setCollapsed((c) => {
      lsSet(`${ns}:collapsed`, c ? '0' : '1');
      return !c;
    });
  }, [ns]);

  // ── derived: predicate applied to the list ──
  const activeView = config.views.find((v) => v.id === view) || config.views[0];

  const predicate = useCallback((item: any) => {
    if (activeView && !activeView.predicate(item)) return false;
    for (const f of config.facets) {
      const ids = facetState[f.id];
      if (!ids || !ids.length) continue;
      const opts = f.options.filter((o) => ids.includes(o.id));
      if (!opts.some((o) => o.predicate(item))) return false;
    }
    return true;
  }, [activeView, config.facets, facetState]);

  // ── derived: counts (views over all items; facet options over current view) ──
  const viewCounts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const v of config.views) m[v.id] = items.filter(v.predicate).length;
    return m;
  }, [config.views, items]);

  const facetCounts = useMemo(() => {
    const inView = activeView ? items.filter(activeView.predicate) : items;
    const m: Record<string, number> = {};
    for (const f of config.facets) for (const o of f.options) m[`${f.id}:${o.id}`] = inView.filter(o.predicate).length;
    return m;
  }, [config.facets, items, activeView]);

  const activeFilterCount = useMemo(
    () => (view !== (config.views[0]?.id || 'all') ? 1 : 0) + Object.values(facetState).reduce((s, a) => s + a.length, 0),
    [view, facetState, config.views],
  );

  return {
    view, setView,
    facetState, toggleFacet,
    clearAll, activeFilterCount,
    collapsed, toggleCollapsed,
    predicate, viewCounts, facetCounts,
  };
}
