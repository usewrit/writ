/**
 * Marketplace shim (self-host build).
 *
 * The self-host coordinator has no marketplace, monetization, collections, or
 * installs. This shim exposes the tiny surface that the master-detail library
 * components (WorkflowList / WorkflowDetailPane / RunWorkflowModal) import — every
 * API method returns an empty/neutral result, so the marketplace/collection/
 * monetization UI in those components naturally renders nothing.
 *
 * Types are intentionally loose: the consumers only read a handful of fields,
 * and there is no live data to type against.
 */

import i18n from '../i18n';

export type PricingModel = 'free' | 'per_success' | string;
export type Sku = string;
export type InstallStatus = 'active' | 'needs_attach' | 'disabled' | string;

export interface WorkflowCostSummary {
  is_monetized: boolean;
  price_per_success_micros?: number | null;
  baseline_duration_ms?: number | null;
  pricing_model?: PricingModel;
  sku_allowed?: Sku;
  est_run_cost_micros: number;
  earnings_total_micros: number;
  earnings_pending_micros: number;
  run_count: number;
}

export interface CollectionItem {
  id?: number;
  collection_id?: number;
  workflow_id: number;
  listing_id?: number | null;
  sort_order?: number;
  price_per_success_micros?: number | null;
}

export interface MarketplaceCollection {
  id?: number;
  slug?: string;
  name: string;
  description?: string | null;
  item_count?: number;
  items?: CollectionItem[];
  [k: string]: any;
}

export interface InstalledWorkflow {
  id: number;
  workflow_id: number;
  listing_id: number;
  status: InstallStatus;
  slug?: string;
  title?: string;
  [k: string]: any;
}

// Data-less required-data manifest (names/types only). Loose — the run modal
// only ever reads slot arrays, which are absent in the self-host build.
export interface DataManifest {
  persona_slots?: any[];
  secret_slots?: any[];
  input_slots?: any[];
  file_slots?: any[];
  output_fields?: any[];
  manifest_version?: number;
  has_login?: boolean;
  [k: string]: any;
}

export interface MarketplaceListingDetail {
  id: number;
  workflow_id: number;
  slug?: string;
  title?: string;
  input_schema?: DataManifest;
  [k: string]: any;
}

export interface WorkflowMonetization extends WorkflowCostSummary {
  is_public?: boolean;
  has_listing?: boolean;
  listing_id?: number | null;
  listing_slug?: string | null;
  [k: string]: any;
}

// ── Inert API objects (self-host: nothing to serve) ────────────────────────

export const marketplaceApi = {
  async listInstalls(): Promise<InstalledWorkflow[]> { return []; },
  async getListing(_slug: string): Promise<MarketplaceListingDetail | null> { return null; },
  async uninstall(_slug: string): Promise<void> { /* no marketplace */ },
  async uploadCoverImage(_file: File): Promise<{ url: string } | null> { return null; },
};

export const collectionsApi = {
  async listMine(): Promise<{ collections: MarketplaceCollection[] }> { return { collections: [] }; },
  async create(_body: { name: string; visibility?: string }): Promise<MarketplaceCollection> {
    throw new Error(i18n.t('Collections are not available in the self-host build.'));
  },
  async setItems(_id: number, _items: any[]): Promise<void> { /* no-op */ },
  async remove(_id: number): Promise<void> { /* no-op */ },
};

export const monetizationApi = {
  async costSummary(_ids?: number[]): Promise<Record<number, WorkflowCostSummary>> { return {}; },
};

export const earningsApi = {
  async myListings(): Promise<{ listings: MarketplaceListingDetail[] }> { return { listings: [] }; },
};

// ── Formatters (display-only; no wallet in the self-host build) ─────────────

export const formatMicros = (micros?: number | null): string => {
  const m = typeof micros === 'number' && Number.isFinite(micros) ? micros : 0;
  return `$${(m / 1_000_000).toFixed(2)}`;
};

export const formatPricePerRun = (micros?: number | null): string => {
  if (!micros || micros <= 0) return 'Free';
  return formatMicros(micros);
};
