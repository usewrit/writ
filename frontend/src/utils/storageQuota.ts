import i18n from '../i18n';
import { apiErrorDetail } from '../api/client';

/**
 * Canonical, machine-readable code for a STORAGE / file-count quota denial.
 *
 * This is the STORAGE-LIMIT ERROR CONTRACT string both layers agree on: a
 * storage denial must be DISTINGUISHABLE from a credits/wallet 402 so the UI
 * shows a storage-specific surface (free up space / raise plan) and NEVER the
 * "insufficient funds" credits modal.
 *
 * Treat this as the single source of truth — the 402/409 interceptor normalizes
 * whatever the backend emits to this code, and every consumer branches on it.
 */
export const STORAGE_QUOTA_CODE = 'storage_quota_exceeded';

/**
 * Backend codes that mean "storage/file-count quota exceeded". The contract
 * names ONE canonical code (STORAGE_QUOTA_CODE); the legacy plan_enforcer codes
 * are recognized too so the UX is correct whether or not the backend has been
 * brought fully in line yet (defense-in-depth across the two sibling fixers).
 */
const STORAGE_QUOTA_CODES = new Set<string>([
  STORAGE_QUOTA_CODE,
  'storage_quota', // legacy: per-org plan / platform byte-ceiling denial
  'file_count_limit', // legacy: per-org / platform file-count denial
]);

/** True when an error code denotes a storage / file-count quota denial. */
export const isStorageQuotaCode = (code?: string | null): boolean =>
  !!code && STORAGE_QUOTA_CODES.has(code);

/** Structured storage fields the contract may carry on a quota denial body. */
export interface StorageQuotaDetail {
  message?: string;
  bytes_used?: number;
  bytes_limit?: number;
  file_count?: number;
  file_limit?: number;
  scope?: string;
}

/**
 * True when a thrown API error is a storage/file-count quota denial — checks the
 * structured `detail.code` (the contract shape). Use at upload call sites to
 * decide whether to suppress a local toast and let the central storage modal own
 * the message (no double-surfacing).
 */
export const isStorageQuotaError = (err: any): boolean => {
  const detail = apiErrorDetail<{ code?: string }>(err);
  return isStorageQuotaCode(detail?.code);
};

/** The contract's storage-specific user message (i18n, natural-language key). */
export const storageLimitMessage = (): string =>
  i18n.t('Storage limit reached — free up space or raise your plan/limit.');
