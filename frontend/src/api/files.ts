import client from './client';
import { downloadBlob } from '../utils/format';

/**
 * File-assets API client.
 *
 * Mirrors the backend surface in the coordinator files router (prefix /files, JWT +
 * get_auth_context). Every mutation funnels through services.file_service, so
 * quota / size / content-type / ownership are all enforced server-side — the
 * client just calls.
 *
 * A StoredFile is an OpenAI Files-API-shaped row: a stable `file_<id>` handle the
 * UI links to an upload step, passes into a run, or fetches via /files/{id}/content.
 * `created_at` is a UNIX EPOCH int (OpenAI convention), NOT an ISO string.
 */

// ============================================================================
// Types
// ============================================================================

/** Where a file came from (StoredFile.source). */
export type FileSource =
  | 'upload'
  | 'api'
  | 'workflow_output'
  | 'ai_session'
  | 'streaming';

/** Processing lifecycle (StoredFile.status). */
export type FileStatus = 'processing' | 'ready' | 'error';

/**
 * A stored file row (file_service.open_serialize — OpenAI Files-API shape).
 * Never carries the storage key.
 */
export interface StoredFile {
  /** Stable handle: "file_<uuid4hex>". */
  id: string;
  object: 'file';
  /** Size in bytes (storage is the source of truth). */
  bytes: number;
  /** UNIX epoch seconds (OpenAI convention) — NOT an ISO string. */
  created_at: number;
  /** Original/suggested name (sanitized server-side). */
  filename: string;
  /** OpenAI "purpose"-style tag (defaults to "user_data"). */
  purpose: string;
  content_type: string;
  status: FileStatus;
  source: FileSource;
}

/** GET /files — paged list of the caller's files (newest first). */
export interface FileListResponse {
  object: 'list';
  data: StoredFile[];
  total: number;
  limit: number;
  offset: number;
}

/**
 * GET /files/usage — the library quota bar. A `*_limit` of -1 means UNLIMITED
 * (no plan/override ceiling); render those as "unlimited" rather than a bar.
 */
export interface FileUsage {
  bytes_used: number;
  /** -1 = unlimited. */
  bytes_limit: number;
  file_count: number;
  /** -1 = unlimited (admin override only). */
  file_limit: number;
}

export interface ListFilesParams {
  /** Filter by origin (e.g. 'upload' to hide workflow-captured artifacts). */
  source?: FileSource;
  /** 1–200, default 50. */
  limit?: number;
  offset?: number;
}

// ============================================================================
// API
// ============================================================================

export const filesApi = {
  /** List the stored files (newest first), optionally by source. */
  list: async (params: ListFilesParams = {}): Promise<FileListResponse> => {
    const res = await client.get('/files', {
      params: {
        source: params.source || undefined,
        limit: params.limit ?? undefined,
        offset: params.offset ?? undefined,
      },
    });
    return res.data;
  },

  /**
   * Upload a file to the library (source=upload). Multipart, mirrors
   * marketplaceApi.uploadCoverImage; the backend validates content-type (415),
   * size (413), and quota (402/409) authoritatively. Returns the new row.
   */
  upload: async (file: File, purpose?: string): Promise<StoredFile> => {
    const form = new FormData();
    form.append('file', file);
    if (purpose) form.append('purpose', purpose);
    const res = await client.post('/files', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
    return res.data;
  },

  /** File metadata. 404 if not owned / deleted (fail-closed). */
  get: async (fileId: string): Promise<StoredFile> => {
    const res = await client.get(`/files/${encodeURIComponent(fileId)}`);
    return res.data;
  },

  /** Soft-delete a file (and hard-remove its bytes). 404 if not owned. */
  delete: async (fileId: string): Promise<void> => {
    await client.delete(`/files/${encodeURIComponent(fileId)}`);
  },

  /** Quota snapshot for the library bar (bytes + file count, used vs limit). */
  usage: async (): Promise<FileUsage> => {
    const res = await client.get('/files/usage');
    return res.data;
  },

  /**
   * Same-origin URL for a file's bytes (GET /files/{id}/content). The backend
   * 302-redirects to a short-TTL, single-object presigned GET (or proxies the
   * bytes when storage is degraded); either way the MinIO host/creds are never
   * exposed.
   *
   * NOTE: this endpoint is JWT-gated, and the access token is memory-only — a
   * plain `<a href>` / `<img src>` would NOT carry it. Use this only for the
   * 302→presigned case (the presigned URL needs no auth) when you control the
   * fetch; for robust, auth-correct download/preview prefer `fetchBlob` /
   * `download` below, which go through the axios client (token + 302 follow).
   */
  contentUrl: (fileId: string): string =>
    `/api/files/${encodeURIComponent(fileId)}/content`,

  /**
   * Resolve a file to a SHORT-TTL signed GET descriptor
   * (`{file_id, url, filename, content_type, size}`).
   *
   * For handing a file to something that cannot authenticate as this user — namely a
   * remote recording agent that has to satisfy a page's file chooser mid-recording.
   * Ownership is checked server-side when minting, and the URL is single-object and
   * expiring, so passing it down the recording socket exposes nothing else.
   */
  signedUrl: async (
    fileId: string,
  ): Promise<{ file_id: string; url: string; filename: string; content_type?: string; size?: number }> => {
    const res = await client.get(`/files/${encodeURIComponent(fileId)}/signed-url`);
    return res.data;
  },

  /**
   * Fetch a file's bytes as a Blob through the authenticated axios client. axios
   * attaches the Bearer token and transparently follows the backend's 302 to the
   * presigned GET, so this works for BOTH the presigned-redirect and the proxy-
   * fallback paths. Caller owns the returned Blob (revoke any object URL made
   * from it). Used for inline preview (images/PDF) and download.
   */
  fetchBlob: async (fileId: string): Promise<Blob> => {
    const res = await client.get(
      `/files/${encodeURIComponent(fileId)}/content`,
      { responseType: 'blob' },
    );
    return res.data as Blob;
  },

  /** Download a file to the user's disk (auth-correct; via fetchBlob). */
  download: async (fileId: string, filename: string): Promise<void> => {
    const blob = await filesApi.fetchBlob(fileId);
    downloadBlob(blob, filename || 'file');
  },
};

export default filesApi;
