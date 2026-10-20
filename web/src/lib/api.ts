/**
 * Typed client for the AURA Management API.
 *
 * Security rules enforced here:
 * - Session is carried by the HttpOnly `aura_session` cookie
 *   (`credentials: "include"`); the app never sees or stores it.
 * - Mutations send the CSRF token in the `x-csrf-token` header.
 * - No S3 credentials exist in this bundle. Presigned URLs are opaque
 *   short-lived strings issued by the API; uploads PUT bytes to them.
 * - Only relative paths are used, so no backend address leaks into the UI.
 */

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`API ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export interface LoginResponse {
  csrf_token: string;
}

export interface ObjectItem {
  id: string;
  display_name: string;
  content_type: string;
  size_bytes: number;
  /** Declared size at intent time; null until the first intent (server field). */
  expected_size_bytes?: number | null;
  checksum_sha256: string;
  checksum_algorithm?: string;
  status: string;
  /** Folder the object is filed in, or null when unfiled. */
  folder_id: string | null;
  /** Catalogue creation time, epoch seconds. */
  created_at: number;
  /** Catalogue last-update time, epoch seconds. */
  updated_at: number;
}

export interface ObjectsListResponse {
  items: ObjectItem[];
}

/**
 * Filters and paging for `GET /api/v1/objects` (M02). Every field is optional;
 * omitted fields are left out of the query string so the default request stays
 * the historical "first page of every visible object, newest first".
 */
export interface ObjectQuery {
  /** undefined = all objects, null = unfiled only, string = that folder. */
  folderId?: string | null;
  /** Case-insensitive display-name substring. */
  q?: string;
  /** Exact catalogue status. */
  status?: string;
  /** Exact media type (compared case-insensitively by the API). */
  contentType?: string;
  minSize?: number;
  maxSize?: number;
  /** Inclusive epoch-second bounds on creation time. */
  createdAfter?: number;
  createdBefore?: number;
  sort?: "created_at" | "display_name" | "size_bytes";
  order?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

/** A logical folder owned by the signed-in human user (#61). */
export interface FolderItem {
  id: string;
  name: string;
  parent_id: string | null;
  created_at: number;
}

export interface FolderCreateInput {
  name: string;
  parent_id?: string | null;
}

export interface FolderPatchInput {
  name?: string;
  parent_id?: string | null;
}

/** Response of creating a machine identity: the token is shown exactly once. */
export interface IdentityCreateResponse {
  id: string;
  name: string;
  space_id: string;
  application_id: string;
  scopes: string[];
  expires_at: number | null;
  token: string;
}

export interface IdentityRotateResponse {
  id: string;
  token: string;
}

/** One audit event as returned by the M07 search endpoint (newest first). */
export interface AuditEvent {
  ts: number;
  actor: string | null;
  action: string;
  result: string;
  request_id?: string | null;
  space_id?: string | null;
  application_id?: string | null;
  [key: string]: unknown;
}

/** Filters and paging for `GET /api/v1/admin/audit` (M07). */
export interface AuditQuery {
  since?: number;
  until?: number;
  actor?: string;
  action?: string;
  result?: string;
  spaceId?: string;
  applicationId?: string;
  requestId?: string;
  limit?: number;
  offset?: number;
}

export interface AuditSearchResponse {
  items: AuditEvent[];
  total: number;
  truncated: boolean;
  scanned: number;
  retention_days: number;
  queried_at: number;
}

export interface AuditPruneResponse {
  kept: number;
  dropped: number;
  cutoff: number;
  retention_days: number;
}

/** Real per-space consumption (verified available bytes only, M07). */
export interface UsageSpaceEntry {
  space_id: string;
  space_name: string;
  environment: string;
  application_id: string;
  application_name: string | null;
  objects_by_status: Record<string, number>;
  available_count: number;
  stored_bytes: number;
  pending_count: number;
}

export interface UsageResponse {
  /** Live query time (epoch seconds): the freshness marker. */
  measured_at: number;
  limits: {
    max_object_bytes: number;
    max_pending_intents: number;
    intent_ttl: number;
    allow_empty_objects: boolean;
  };
  /** Capacity guard; null when no capacity is configured. */
  capacity?: {
    capacity_bytes: number;
    high_watermark_pct: number;
    accept_until_bytes: number;
    stored_bytes: number;
    reserved_bytes: number;
    free_for_uploads_bytes: number;
  } | null;
  totals: {
    objects_by_status: Record<string, number>;
    available_count: number;
    stored_bytes: number;
  };
  spaces: UsageSpaceEntry[];
}

export interface HealthComponent {
  status: string;
  latency_ms?: number;
  uptime_s?: number;
  version?: string;
  error?: string;
  note?: string;
  total_bytes?: number;
  used_bytes?: number;
  free_bytes?: number;
}

export interface HealthResponse {
  status: string;
  checked_at: number;
  components: Record<string, HealthComponent>;
  recent_errors: {
    window_s: number;
    events: Array<{
      ts: number;
      action: string;
      actor: string | null;
      result: string;
      request_id?: string | null;
    }>;
    scanned: number;
  };
  note: string;
}

export interface OrphanCandidate {
  id: string;
  display_name: string;
  space_id: string;
  application_id: string | null;
  size_bytes: number;
  updated_at: number;
}

export interface OrphanScanResponse {
  checked: number;
  candidates: OrphanCandidate[];
  checked_at: number;
}

export interface StorageOrphan {
  key: string;
  size_bytes: number;
  last_modified: number;
  reason: "no_row" | "terminal";
  object_id: string | null;
}

export interface StorageOrphanBucket {
  bucket: string;
  scanned?: number;
  orphans?: StorageOrphan[];
  orphan_bytes?: number;
  stale_uploads?: Array<{ key: string; upload_id: string; initiated: number }>;
  next_start_after?: string | null;
  error?: string;
}

export interface StorageOrphanReport {
  grace_seconds: number;
  buckets: StorageOrphanBucket[];
  checked_at: number;
}

export interface IntegrityProblem {
  id: string;
  space_id: string;
  display_name: string;
  size_bytes: number;
  scrub_result: "mismatch" | "missing";
  scrubbed_at: number;
}

export interface IntegrityReport {
  interval_days: number;
  /** Count (and oldest check) per result: ok, mismatch, missing, unchecked. */
  results: Record<string, { count: number; oldest: number | null }>;
  problems: IntegrityProblem[];
}

export interface ReconcileResponse {
  expired: number;
  storage_deletes_attempted: number;
  storage_failures: number;
}

/**
 * Mirrors `SERVICE_SCOPES` in api/authz.py: machine identities are data-plane
 * principals and may never hold platform administration capabilities.
 */
export const SERVICE_SCOPES = [
  "objects:read",
  "objects:write",
  "objects:delete",
  "objects:purge",
] as const;

export interface UploadIntentRequest {
  /**
   * Optional: when omitted the server files the object in the caller's default
   * space bucket. Only sent when a caller must target a specific bucket.
   */
  bucket?: string;
  display_name: string;
  content_type: string;
  /** Exact object size in bytes; the API signs it into the presigned URL. */
  size_bytes: number;
  folder_id?: string | null;
}

export interface UploadIntentResponse {
  id: string;
  status: string;
  /** Data-plane method the browser must use. */
  method: string;
  /** Single presigned PUT; null for a multipart upload (parts are requested). */
  upload_url: string | null;
  /** "multipart" when the server chose part-level upload (#72). */
  upload_mode?: "single" | "multipart";
  multipart?: { part_size_bytes: number; part_count: number };
  /** Headers to send with the presigned PUT (Content-Type is signed). */
  headers: Record<string, string>;
  /** Server-normalized media type actually stored/authorized. */
  content_type: string;
  size_bytes: number;
  expires_in: number;
  /** Absolute expiry (epoch seconds) when the URL stops being valid. */
  expires_at: number | null;
  /** True when the server replayed an earlier intent for the same key. */
  idempotent_replay: boolean;
}

export interface CommitResponse {
  id: string;
  status: string;
  size_bytes: number;
  checksum_sha256: string;
  checksum_algorithm?: string;
}

export interface PreviewIntentResponse {
  id: string;
  preview_url: string;
  method: string;
  expires_in: number;
  expires_at: number | null;
  display_name: string;
  content_type: string;
  size_bytes: number;
}

export interface MultipartStatus {
  id: string;
  size_bytes: number;
  part_size_bytes: number;
  part_count: number;
  uploaded_parts: number[];
}

export interface MultipartPartUrls {
  id: string;
  expires_in: number;
  parts: Array<{ part_number: number; size_bytes: number; url: string }>;
}

export interface DownloadIntentResponse {
  id: string;
  download_url: string;
  method: string;
  expires_in: number;
  expires_at: number | null;
  display_name: string;
  content_type: string;
  size_bytes: number;
  checksum_sha256: string;
  checksum_algorithm?: string;
}

/** Principal metadata returned by GET /api/v1/me. */
export interface SessionInfo {
  id: string;
  /** "user" (human session) or "service" (machine identity). */
  kind: string;
  /** Display name: username for a person, identity name for a service. */
  name?: string | null;
  /** Role for humans: "user" | "operator" | "admin"; null for services. */
  role: string | null;
  space_id: string | null;
  application_id: string | null;
  scopes: string[];
  /**
   * CSRF token for the current session (additive field of GET /api/v1/me).
   * Present for human sessions, `null` for machine identities. Used to
   * re-hydrate the in-memory CSRF token after a reload/new tab.
   */
  csrf_token: string | null;
}

/** True when a role may read the administration console (`admin:read`). */
export function hasAdminAccess(role: string | null | undefined): boolean {
  return role === "operator" || role === "admin";
}

/** True when a machine identity scope list contains `scope`. */
export function hasScope(
  scopes: readonly string[] | null | undefined,
  scope: string,
): boolean {
  return scopes?.includes(scope) ?? false;
}

export interface AdminStats {
  objects_by_status: Record<string, number>;
  applications: number;
  spaces: number;
  buckets: number;
  service_identities: number;
  active_service_identities: number;
}

export interface ApplicationItem {
  id: string;
  name: string;
  status: string;
  created_at?: number;
}

export interface ApplicationDetail extends ApplicationItem {
  created_at: number;
  /** Number of spaces currently attached to this application. */
  space_count: number;
}

export interface SpaceItem {
  id: string;
  application_id: string;
  name: string;
  environment: string;
  status: string;
  created_at?: number;
}

/** A bucket registered to a space; carries no S3 credential of any kind. */
export interface BucketItem {
  id: string;
  space_id: string;
  name: string;
  is_default: boolean;
  created_at: number;
}

/** A bucket row in the global registry, joined with its space/application. */
export interface BucketListedItem extends BucketItem {
  space_name: string | null;
  environment: string | null;
  application_id: string | null;
  application_name: string | null;
}

export interface SpaceDetail extends SpaceItem {
  created_at: number;
  application_name: string | null;
  buckets: BucketItem[];
  counts: { buckets: number; identities: number; objects: number };
}

/** Editable application fields; `status` drives activation/deactivation. */
export interface ApplicationPatchInput {
  name?: string;
  status?: string;
}

/**
 * Editable space fields. `application_id` is deliberately absent: moving a space
 * to another application is refused by the server.
 */
export interface SpacePatchInput {
  name?: string;
  environment?: string;
  status?: string;
}

export interface IdentityItem {
  id: string;
  name: string;
  space_id: string;
  application_id: string;
  scopes: string[];
  created_at: number;
  expires_at: number | null;
  last_used_at: number | null;
  revoked_at: number | null;
  rotated_at: number | null;
}

export interface ListResponse<T> {
  items: T[];
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  csrf?: string | null;
  headers?: Record<string, string>;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { ...(options.headers ?? {}) };
  let body: BodyInit | undefined;
  if (options.body !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  if (options.csrf) {
    headers["x-csrf-token"] = options.csrf;
  }
  const res = await fetch(path, {
    method: options.method ?? "GET",
    headers,
    body,
    credentials: "include",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = (await res.json()) as { detail?: unknown };
      if (typeof data.detail === "string" && data.detail.length > 0) {
        detail = data.detail;
      }
    } catch {
      // Keep the HTTP status text when the body is not JSON.
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}

export const api = {
  login(username: string, password: string): Promise<LoginResponse> {
    return request<LoginResponse>("/api/v1/login", {
      method: "POST",
      body: { username, password },
    });
  },

  logout(csrf: string): Promise<{ status: string }> {
    return request<{ status: string }>("/api/v1/logout", {
      method: "POST",
      csrf,
    });
  },

  /**
   * List objects with optional search, filters, sort and paging (M02). The
   * server owns the filtering/sorting/paging so the UI never has to hold the
   * whole catalogue; see `ObjectQuery` for each field. `folderId`: `undefined`
   * = every object, `null` = only unfiled objects, a string = that folder. The
   * server caps `limit` at 100.
   */
  listObjects(query: ObjectQuery = {}): Promise<ObjectsListResponse> {
    const params = new URLSearchParams();
    if (query.folderId !== undefined) {
      params.set("folder_id", query.folderId ?? "root");
    }
    const q = query.q?.trim();
    if (q) params.set("q", q);
    if (query.status) params.set("status", query.status);
    if (query.contentType) params.set("content_type", query.contentType);
    if (query.minSize !== undefined) params.set("min_size", String(query.minSize));
    if (query.maxSize !== undefined) params.set("max_size", String(query.maxSize));
    if (query.createdAfter !== undefined) {
      params.set("created_after", String(query.createdAfter));
    }
    if (query.createdBefore !== undefined) {
      params.set("created_before", String(query.createdBefore));
    }
    if (query.sort) params.set("sort", query.sort);
    if (query.order) params.set("order", query.order);
    if (query.limit !== undefined) params.set("limit", String(query.limit));
    if (query.offset !== undefined) params.set("offset", String(query.offset));
    const query2 = params.toString();
    return request<ObjectsListResponse>(
      `/api/v1/objects${query2 ? `?${query2}` : ""}`,
    );
  },

  me(): Promise<SessionInfo> {
    return request<SessionInfo>("/api/v1/me");
  },

  getObject(id: string): Promise<ObjectItem> {
    return request<ObjectItem>(`/api/v1/objects/${encodeURIComponent(id)}`);
  },

  createUploadIntent(
    input: UploadIntentRequest,
    csrf: string,
    options?: { idempotencyKey?: string },
  ): Promise<UploadIntentResponse> {
    const headers = options?.idempotencyKey
      ? { "Idempotency-Key": options.idempotencyKey }
      : undefined;
    return request<UploadIntentResponse>("/api/v1/objects/upload-intents", {
      method: "POST",
      body: input,
      csrf,
      headers,
    });
  },

  commitObject(id: string, csrf: string): Promise<CommitResponse> {
    return request<CommitResponse>(`/api/v1/objects/${encodeURIComponent(id)}/commit`, {
      method: "POST",
      csrf,
    });
  },

  /** Cancel a pending upload intent (the only backend-supported cancellation). */
  abortObject(id: string, csrf: string): Promise<{ id: string; status: string }> {
    return request<{ id: string; status: string }>(
      `/api/v1/objects/${encodeURIComponent(id)}/abort`,
      { method: "POST", csrf },
    );
  },

  createDownloadIntent(id: string, csrf: string): Promise<DownloadIntentResponse> {
    return request<DownloadIntentResponse>(
      `/api/v1/objects/${encodeURIComponent(id)}/download-intent`,
      { method: "POST", csrf },
    );
  },

  /** Short-lived inline GET for images, audio and PDF (415 for other types). */
  createPreviewIntent(id: string, csrf: string): Promise<PreviewIntentResponse> {
    return request<PreviewIntentResponse>(
      `/api/v1/objects/${encodeURIComponent(id)}/preview-intent`,
      { method: "POST", csrf },
    );
  },

  // --- Object lifecycle ---
  /** File an object into a folder, or unfile it (`folderId` null = root). */
  moveObject(
    id: string,
    folderId: string | null,
    csrf: string,
  ): Promise<ObjectItem> {
    return request<ObjectItem>(`/api/v1/objects/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: { folder_id: folderId },
      csrf,
    });
  },

  /** Multipart plan and the parts the storage already holds (resume). */
  multipartStatus(id: string): Promise<MultipartStatus> {
    return request<MultipartStatus>(`/api/v1/objects/${encodeURIComponent(id)}/multipart`);
  },

  /** Short-lived presigned PUTs for up to 100 parts. */
  multipartPartUrls(
    id: string,
    partNumbers: number[],
    csrf: string,
  ): Promise<MultipartPartUrls> {
    return request<MultipartPartUrls>(
      `/api/v1/objects/${encodeURIComponent(id)}/multipart/parts`,
      { method: "POST", csrf, body: { part_numbers: partNumbers } },
    );
  },

  /** Soft-delete an object (objects:delete). */
  deleteObject(id: string, csrf: string): Promise<{ id: string; status: string }> {
    return request<{ id: string; status: string }>(
      `/api/v1/objects/${encodeURIComponent(id)}`,
      { method: "DELETE", csrf },
    );
  },

  /** Undo a soft delete (objects:delete); 409 if it is not in the trash. */
  restoreObject(id: string, csrf: string): Promise<ObjectItem> {
    return request<ObjectItem>(
      `/api/v1/objects/${encodeURIComponent(id)}/restore`,
      { method: "POST", csrf },
    );
  },

  /** Permanently delete an object (objects:purge; operator/admin only). */
  purgeObject(id: string, csrf: string): Promise<{ id: string; status: string }> {
    return request<{ id: string; status: string }>(
      `/api/v1/objects/${encodeURIComponent(id)}/purge`,
      { method: "POST", csrf },
    );
  },

  // --- Logical folders (human session only) ---
  /** One level of the caller's tree (root when `parentId` is omitted). */
  listFolders(parentId?: string): Promise<ListResponse<FolderItem>> {
    const path = parentId
      ? `/api/v1/folders?parent_id=${encodeURIComponent(parentId)}`
      : "/api/v1/folders";
    return request<ListResponse<FolderItem>>(path);
  },

  /** The caller's whole folder tree as a flat list, in a single request. */
  listAllFolders(): Promise<ListResponse<FolderItem>> {
    return request<ListResponse<FolderItem>>("/api/v1/folders?all=true");
  },

  getFolder(id: string): Promise<FolderItem> {
    return request<FolderItem>(`/api/v1/folders/${encodeURIComponent(id)}`);
  },

  createFolder(input: FolderCreateInput, csrf: string): Promise<FolderItem> {
    return request<FolderItem>("/api/v1/folders", {
      method: "POST",
      body: input,
      csrf,
    });
  },

  updateFolder(id: string, patch: FolderPatchInput, csrf: string): Promise<FolderItem> {
    return request<FolderItem>(`/api/v1/folders/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: patch,
      csrf,
    });
  },

  deleteFolder(id: string, csrf: string): Promise<{ id: string; status: string }> {
    return request<{ id: string; status: string }>(
      `/api/v1/folders/${encodeURIComponent(id)}`,
      { method: "DELETE", csrf },
    );
  },

  // --- Administration console (reads, `admin:read`) ---
  adminStats(): Promise<AdminStats> {
    return request<AdminStats>("/api/v1/admin/stats");
  },

  listApplications(): Promise<ListResponse<ApplicationItem>> {
    return request<ListResponse<ApplicationItem>>("/api/v1/admin/applications");
  },

  getApplication(id: string): Promise<ApplicationDetail> {
    return request<ApplicationDetail>(
      `/api/v1/admin/applications/${encodeURIComponent(id)}`,
    );
  },

  listSpaces(): Promise<ListResponse<SpaceItem>> {
    return request<ListResponse<SpaceItem>>("/api/v1/admin/spaces");
  },

  getSpace(id: string): Promise<SpaceDetail> {
    return request<SpaceDetail>(`/api/v1/admin/spaces/${encodeURIComponent(id)}`);
  },

  listBuckets(): Promise<ListResponse<BucketListedItem>> {
    return request<ListResponse<BucketListedItem>>("/api/v1/admin/buckets");
  },

  listIdentities(): Promise<ListResponse<IdentityItem>> {
    return request<ListResponse<IdentityItem>>("/api/v1/admin/identities");
  },

  // --- Observability reads (M07, `admin:read`) ---
  searchAudit(query: AuditQuery): Promise<AuditSearchResponse> {
    const params = new URLSearchParams();
    if (query.since !== undefined) params.set("since", String(query.since));
    if (query.until !== undefined) params.set("until", String(query.until));
    if (query.actor) params.set("actor", query.actor);
    if (query.action) params.set("action", query.action);
    if (query.result) params.set("result", query.result);
    if (query.spaceId) params.set("space_id", query.spaceId);
    if (query.applicationId) params.set("application_id", query.applicationId);
    if (query.requestId) params.set("request_id", query.requestId);
    if (query.limit !== undefined) params.set("limit", String(query.limit));
    if (query.offset !== undefined) params.set("offset", String(query.offset));
    const suffix = params.size > 0 ? `?${params.toString()}` : "";
    return request<AuditSearchResponse>(`/api/v1/admin/audit${suffix}`);
  },

  adminUsage(spaceId?: string): Promise<UsageResponse> {
    const suffix = spaceId ? `?space_id=${encodeURIComponent(spaceId)}` : "";
    return request<UsageResponse>(`/api/v1/admin/usage${suffix}`);
  },

  adminHealth(): Promise<HealthResponse> {
    return request<HealthResponse>("/api/v1/admin/health");
  },

  /** Read-only orphan candidates (admin:read, M07/#87). */
  adminOrphans(limit = 100): Promise<OrphanScanResponse> {
    return request<OrphanScanResponse>(
      `/api/v1/admin/orphans?limit=${encodeURIComponent(String(limit))}`,
    );
  },

  /** Storage bytes the catalogue does not account for (admin:read, read-only). */
  adminStorageOrphans(): Promise<StorageOrphanReport> {
    return request<StorageOrphanReport>("/api/v1/admin/storage-orphans");
  },

  /** Periodic integrity scrub results (admin:read). */
  adminIntegrity(): Promise<IntegrityReport> {
    return request<IntegrityReport>("/api/v1/admin/integrity");
  },

  // --- Administration mutations (`admin:write`) ---
  reconcile(csrf: string): Promise<ReconcileResponse> {
    return request<ReconcileResponse>("/api/v1/admin/reconcile", {
      method: "POST",
      csrf,
    });
  },

  /** Enforce the audit retention window (M07, `admin:write`). */
  pruneAudit(csrf: string): Promise<AuditPruneResponse> {
    return request<AuditPruneResponse>("/api/v1/admin/audit/prune", {
      method: "POST",
      csrf,
    });
  },

  createApplication(name: string, csrf: string): Promise<ApplicationDetail> {
    return request<ApplicationDetail>("/api/v1/admin/applications", {
      method: "POST",
      body: { name },
      csrf,
    });
  },

  updateApplication(
    id: string,
    patch: ApplicationPatchInput,
    csrf: string,
  ): Promise<ApplicationDetail> {
    return request<ApplicationDetail>(
      `/api/v1/admin/applications/${encodeURIComponent(id)}`,
      { method: "PATCH", body: patch, csrf },
    );
  },

  createSpace(
    input: {
      application_id: string;
      name: string;
      environment: string;
      default_bucket?: string;
    },
    csrf: string,
  ): Promise<SpaceDetail & { bucket: string }> {
    return request<SpaceDetail & { bucket: string }>("/api/v1/admin/spaces", {
      method: "POST",
      body: input,
      csrf,
    });
  },

  updateSpace(
    id: string,
    patch: SpacePatchInput,
    csrf: string,
  ): Promise<SpaceDetail> {
    return request<SpaceDetail>(`/api/v1/admin/spaces/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: patch,
      csrf,
    });
  },

  /** Register an additional (non-default) bucket for a space. */
  createSpaceBucket(
    spaceId: string,
    name: string,
    csrf: string,
  ): Promise<BucketItem> {
    return request<BucketItem>(
      `/api/v1/admin/spaces/${encodeURIComponent(spaceId)}/buckets`,
      { method: "POST", body: { name }, csrf },
    );
  },

  /** Set (or clear) a bucket's default flag. */
  updateBucket(
    id: string,
    isDefault: boolean,
    csrf: string,
  ): Promise<BucketItem> {
    return request<BucketItem>(`/api/v1/admin/buckets/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: { is_default: isDefault },
      csrf,
    });
  },

  createIdentity(
    input: {
      space_id: string;
      name: string;
      scopes: string[];
      ttl_seconds?: number;
    },
    csrf: string,
  ): Promise<IdentityCreateResponse> {
    return request<IdentityCreateResponse>("/api/v1/admin/identities", {
      method: "POST",
      body: input,
      csrf,
    });
  },

  rotateIdentity(id: string, csrf: string): Promise<IdentityRotateResponse> {
    return request<IdentityRotateResponse>(
      `/api/v1/admin/identities/${encodeURIComponent(id)}/rotate`,
      { method: "POST", csrf },
    );
  },

  revokeIdentity(id: string, csrf: string): Promise<{ id: string; revoked: boolean }> {
    return request<{ id: string; revoked: boolean }>(
      `/api/v1/admin/identities/${encodeURIComponent(id)}/revoke`,
      { method: "POST", csrf },
    );
  },
};

/** Failure kinds the transfer engine reacts to. */
export type UploadFailureKind = "network" | "expired" | "rejected" | "aborted";

/** Error raised by the presigned PUT transport, classified for retry logic. */
export class PresignedUploadError extends Error {
  readonly kind: UploadFailureKind;
  readonly status: number;

  constructor(kind: UploadFailureKind, status: number, message: string) {
    super(message);
    this.name = "PresignedUploadError";
    this.kind = kind;
    this.status = status;
  }
}

export interface PutOptions {
  /** Signed headers the API returned for this intent (Content-Type). */
  headers?: Record<string, string>;
  /** Real byte progress, available because XHR exposes upload progress. */
  onProgress?: (loaded: number, total: number) => void;
  signal?: AbortSignal;
}

/**
 * PUT file bytes to the short-lived URL issued by the upload intent.
 *
 * Uses XMLHttpRequest (not fetch) because only XHR exposes *upload* progress,
 * and supports real cancellation through the caller's AbortSignal. The URL is a
 * bearer capability: it is never logged here, only passed to XHR.
 */
export function putToPresignedUrl(
  url: string,
  file: Blob,
  options: PutOptions = {},
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let settled = false;
    const fail = (err: Error) => {
      if (!settled) {
        settled = true;
        reject(err);
      }
    };

    const onAbort = () => {
      xhr.abort();
      fail(new PresignedUploadError("aborted", 0, "Transfert annulé."));
    };
    if (options.signal) {
      if (options.signal.aborted) {
        onAbort();
        return;
      }
      options.signal.addEventListener("abort", onAbort, { once: true });
    }

    xhr.open("PUT", url, true);
    for (const [name, value] of Object.entries(options.headers ?? {})) {
      xhr.setRequestHeader(name, value);
    }
    xhr.upload.onprogress = (event) => {
      const total = event.lengthComputable ? event.total : file.size;
      options.onProgress?.(event.loaded, total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        options.onProgress?.(file.size, file.size);
        if (!settled) {
          settled = true;
          resolve();
        }
        return;
      }
      // 403 on a presigned PUT classically means the signature/URL expired.
      const kind: UploadFailureKind = xhr.status === 403 ? "expired" : "rejected";
      fail(
        new PresignedUploadError(
          kind,
          xhr.status,
          `Dépôt refusé (HTTP ${xhr.status}).`,
        ),
      );
    };
    xhr.onerror = () =>
      fail(new PresignedUploadError("network", xhr.status || 0, "Réseau interrompu."));
    xhr.ontimeout = () =>
      fail(new PresignedUploadError("network", xhr.status || 0, "Délai dépassé."));
    xhr.onabort = () =>
      fail(new PresignedUploadError("aborted", 0, "Transfert annulé."));

    try {
      xhr.send(file);
    } catch (err) {
      fail(
        err instanceof Error
          ? err
          : new PresignedUploadError("network", 0, "Envoi impossible."),
      );
    }
  });
}
