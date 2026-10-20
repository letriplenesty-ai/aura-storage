# AURA API — Authorization matrix and identity model

**Status:** implemented in `api/authz.py`, `api/identity.py`, `api/main.py`; covered by
`tests/test_security_isolation.py` (offline) and exercised end-to-end only after a
pilot deployment. This document is the source of truth for route classification.

## Principals

AURA has two kinds of caller. Every request resolves to exactly one principal.

| Principal | Authenticates with | CSRF | Capabilities |
|---|---|---|---|
| **user** (human) | HttpOnly `aura_session` cookie | required on non-GET | role map below |
| **service** (machine-to-machine) | `Authorization: Bearer aura_<id>_<secret>` (**only** channel; M06) | not applicable | explicit scopes stored on the identity |

A service identity is scoped to one space and one application/environment. It never
uses a human session cookie, and cookie auth is never accepted for a service.
The former undocumented `X-AURA-Key` alias was removed (M06): machine callers
authenticate with the Bearer header and nothing else, so there is exactly one
credential channel with one scope check.

### Roles (human users)

Default-deny: a role only holds the capabilities listed here; anything else is refused.

| Capability | `user` | `operator` | `admin` |
|---|:--:|:--:|:--:|
| `objects:read` | ✅ | ✅ | ✅ |
| `objects:write` | ✅ | ✅ | ✅ |
| `objects:delete` | ✅ | ✅ | ✅ |
| `objects:purge` | ❌ | ✅ | ✅ |
| `admin:read` | ❌ | ✅ | ✅ |
| `admin:write` | ❌ | ❌ | ✅ |

### Capabilities (service identities)

A service identity may hold **object capabilities only**: `objects:read`,
`objects:write`, `objects:delete`, `objects:purge`. Requesting `admin:read` or
`admin:write` is refused with **422** at creation time, and such a scope is never
*effective* even if it is present on a stored row (defence in depth). Platform
administration — creating applications, spaces or further identities — therefore
stays with a human `operator`/`admin` session, so a leaked machine token cannot
escalate its own reach.

Grant the minimum needed; e.g. a read-only consumer gets only `objects:read`.

## Route matrix

| Method | Route | Capability | Notes |
|---|---|---|---|
| GET | `/health/live` | public | liveness only |
| GET | `/health/ready` | public | does not leak backend details |
| POST | `/api/v1/login` | public | failed attempts rate-limited per client IP |
| POST | `/api/v1/logout` | authenticated **human** + CSRF | deletes the session |
| GET | `/api/v1/me` | authenticated | principal metadata |
| GET | `/api/v1/objects` | `objects:read` | scoped to space (+ owner for humans); optional `folder_id` filter (`root` = unfiled) |
| GET | `/api/v1/objects/{oid}` | `objects:read` | 404 if outside the caller's scope |
| PATCH | `/api/v1/objects/{oid}` | `objects:write` | **human only**; file/unfile an object into an owned folder |
| POST | `/api/v1/objects/upload-intents` | `objects:write` | server picks the object key; server enforces type/size/empty policy; optional `Idempotency-Key` dedupes double submits |
| POST | `/api/v1/objects/{oid}/commit` | `objects:write` | streaming SHA-256, idempotent; **202 `verifying`** for large objects, hashed by the verification worker (N5) |
| POST | `/api/v1/objects/{oid}/abort` | `objects:write` | cancel a pending intent (best-effort delete); idempotent; refused on available/deleted |
| POST | `/api/v1/objects/{oid}/download-intent` | `objects:read` | presigned GET, scoped |
| POST | `/api/v1/objects/{oid}/preview-intent` | `objects:read` | presigned GET bound to `Content-Disposition: inline` (PDF/image/audio preview); same 404/409 as download-intent, plus **415** for types outside the inline allow-list (images except SVG, PDF, audio: never HTML/SVG/XML); scoped |
| DELETE | `/api/v1/objects/{oid}` | `objects:delete` | soft delete |
| GET | `/api/v1/objects/{oid}/multipart` | `objects:write` | multipart plan and parts already held (resume); only when `AURA_MULTIPART_ENABLED=1` created the session |
| POST | `/api/v1/objects/{oid}/multipart/parts` | `objects:write` | presigned part PUTs (≤100 per call, exact signed length per part) |
| POST | `/api/v1/objects/{oid}/restore` | `objects:delete` | undo a soft delete (trash); 409 unless the object is `deleted` and was fully uploaded |
| POST | `/api/v1/objects/{oid}/purge` | `objects:purge` | destructive, operator/admin; a storage failure keeps the catalogue row and returns **502** (M06) |
| GET | `/api/v1/folders` | `objects:read` | **human only**; owner-scoped, root, `?parent_id=`, or `?all=true` (flat tree) |
| POST | `/api/v1/folders` | `objects:write` | **human only**; logical folder, no host directory |
| GET | `/api/v1/folders/{fid}` | `objects:read` | **human only**; 404 outside the owner's tree |
| PATCH | `/api/v1/folders/{fid}` | `objects:write` | **human only**; rename/move, cycle-refused |
| DELETE | `/api/v1/folders/{fid}` | `objects:delete` | **human only**; refuses a non-empty folder |
| POST | `/api/v1/admin/reconcile` | `admin:write` | bounded global sweep of stale intents (`limit` 1..5000, oldest first; repeat until 0); returns `{expired, storage_deletes_attempted, storage_failures}` — a storage failure keeps the intent `pending` for the next sweep and is counted in `storage_failures`, never as cleaned (M06) |
| GET | `/api/v1/admin/stats` | `admin:read` | aggregate counts, no content |
| GET | `/api/v1/admin/audit` | `admin:read` | audit search newest-first (period/actor/action/result/space/application/request filters, `limit` 1..500 + `offset`); `total` counts window matches, `truncated` warns of older history (M07) |
| POST | `/api/v1/admin/audit/prune` | `admin:write` | enforce `AURA_AUDIT_RETENTION_DAYS`
| GET | `/api/v1/admin/storage-orphans` | `admin:read` | read-only: storage keys with no catalogue row or a terminal row, and stale multipart uploads; trash excluded; grace period; bounded, resumable |
| POST | `/api/v1/admin/storage-orphans/cleanup` | `admin:write` | **destructive**: remove listed orphan keys / abort listed uploads (≤100 each), each re-checked just before acting |
| GET | `/api/v1/admin/integrity` | `admin:read` | periodic scrub results: counts per result and objects whose bytes no longer match (`mismatch`) or are gone (`missing`); read-only |
| POST | `/api/v1/admin/integrity/scrub` | `admin:write` | run one bounded scrub pass now (same byte budget as the periodic pass) |
| GET | `/api/v1/admin/orphans` | `admin:read` | read-only orphan candidates (available rows missing at storage); never deletes (M07/#87) | (default 90); timestamp-less lines kept (M07) |
| GET | `/api/v1/admin/usage` | `admin:read` | real per-space consumption (verified available bytes + counts) with `measured_at` and configured limits; optional `space_id` filter (M07) |
| GET | `/api/v1/admin/health` | `admin:read` | supervision snapshot (api/database/storage/disk + recent errors); always 200, top-level `degraded` on dependency failure (M07) |
| POST/GET | `/api/v1/admin/applications` | `admin:write` / `admin:read` | platform registry |
| GET | `/api/v1/admin/applications/{aid}` | `admin:read` | detail + stable id + space count |
| PATCH | `/api/v1/admin/applications/{aid}` | `admin:write` | rename and/or soft activate/disable |
| POST/GET | `/api/v1/admin/spaces` | `admin:write` / `admin:read` | space + default bucket; the application must exist and be active |
| GET | `/api/v1/admin/spaces/{sid}` | `admin:read` | detail + buckets + counts |
| PATCH | `/api/v1/admin/spaces/{sid}` | `admin:write` | rename / re-environment / soft activate-disable; `application_id` immutable |
| POST | `/api/v1/admin/spaces/{sid}/buckets` | `admin:write` | register an extra (non-default) bucket for a space |
| GET | `/api/v1/admin/buckets` | `admin:read` | every bucket with its space/application attachment |
| PATCH | `/api/v1/admin/buckets/{bid}` | `admin:write` | set/clear the default flag (one default per space) |
| POST/GET | `/api/v1/admin/identities` | `admin:write` / `admin:read` | M2M identities (refused for a disabled space) |
| POST | `/api/v1/admin/identities/{iid}/rotate` | `admin:write` | returns a new token once |
| POST | `/api/v1/admin/identities/{iid}/revoke` | `admin:write` | immediate revocation |

A route that is not classified here is **closed**: `tests/test_security_isolation.py`
asserts that every `/api/v1` route rejects anonymous callers with 401 or 403 (a 422
never counts as closed — it would let an auth-less route pass if its body were invalid).

## Isolation rules

- An object always carries `space_id` (and `application_id`, `environment`). Every
  catalogue query is filtered by the caller's space.
- Human users are additionally filtered by `owner_id`: they only see their own
  objects inside their space.
- Service identities see the whole space they are bound to, and nothing else.
- The **object key is always generated server-side** (`aura/<space>/<actor>/<uuid>`).
  Client-supplied `object_key`, `owner_id`, `space_id` fields are rejected (422).
- The **bucket is never client-chosen**: the caller's space has a registered bucket
  allow-list; an unregistered bucket is refused (403). An upload without an
  explicit bucket uses the space's **default bucket only** (M06): a space with
  no default is refused with 409 instead of silently falling back to a
  non-default bucket.
- A space's default bucket name (derived or supplied) is validated against the S3
  naming rules on creation; an invalid name is refused (422) so no space can point at
  a bucket S3 would reject.
- **Applications and spaces** have a soft lifecycle only (M04): a rename/disable is a
  `PATCH`, never a delete. A space may only be attached to an **active** application
  (409 otherwise) and `application_id` is immutable (reassignment is 422), so a space
  can never be moved to a different tenant. Disabling a space refuses new upload
  intents (403) and new machine identities (409) while every existing object stays
  readable and downloadable. A bucket is registered additively (validated S3 name,
  globally unique); making one the default clears the flag on the space's other
  buckets. Buckets are never deleted through the API, and no bucket payload ever
  carries an S3 credential.
- Tenant/application/space identifiers received in a request never grant access.
- **Folders** (`#61`) are logical catalogue rows, not host directories. They are
  **owned by a human user** (`folders.owner_id → users.id`, NOT NULL) and every folder
  route additionally requires a human principal, so a service identity never manages
  them. Listing, reading, moving or deleting a folder filters by the caller's
  `owner_id`; a folder owned by someone else is a 404, never a 403 (no existence leak).
  A move that would place a folder inside its own subtree is refused (409), and a
  folder that still has children or objects cannot be deleted (409). Filing an
  **object** into a folder (`PATCH /api/v1/objects/{oid}`) is likewise human-only and
  owner-scoped: the object is read with the caller's space/owner filter, and the target
  folder must be owned by the caller, otherwise 404. Space-scoped folders for service
  identities are a follow-up tied to `#35`.
- 401 (not authenticated) and 403 (not permitted) never reveal whether an object
  exists; cross-scope reads return 404.

## Provisioning, rotation and revocation

Admin-only, via the API (no direct database editing required):

1. **Application** — `POST /api/v1/admin/applications {name}`.
2. **Space** — `POST /api/v1/admin/spaces {application_id, name, environment,
   default_bucket?}`. A dedicated bucket is registered for the space.
3. **Identity** — `POST /api/v1/admin/identities {space_id, name, scopes,
   ttl_seconds?}` (object scopes only; `admin:*` is refused with 422). The response
   contains the token **exactly once**; store it in the consuming platform's secret
   manager. AURA keeps only the SHA-256 fingerprint.
4. **Rotate** — `POST /api/v1/admin/identities/{id}/rotate`; the previous secret stops
   working immediately and the new token is returned once.
5. **Revoke** — `POST /api/v1/admin/identities/{id}/revoke`; access stops immediately.
6. **Audit** — creation, rotation, revocation and every authenticated action are
   recorded in the JSON-lines audit log by identity ID, never by secret.

Never place a token in logs, tickets, chat, the frontend bundle, or this repository.

## Audit and observability (M07)

- Every response carries `X-Request-Id` (a safe client value echoed, otherwise
  a server-minted UUID v4); every in-request audit event stores it, so a UI
  action traces to its backend events. Pre-authentication events (e.g. failed
  logins) carry the request id but no tenant.
- Post-authentication events are attributed with the caller's `space_id` /
  `application_id` by the authorization layer; explicit event fields always
  win, so attribution can never be rewritten silently.
- The log stays append-only JSON-lines with a scalar whitelist: passwords,
  secrets, session/CSRF tokens and (presigned) URLs are never recorded, not
  even truncated. Reads (`GET /api/v1/admin/audit`, `admin:read`) scan a
  bounded tail (`AURA_AUDIT_MAX_SCAN_LINES`, default 20000) newest-first with
  `limit` 1..500 + `offset`; `truncated` warns of older history beyond the
  window. Retention (`POST /api/v1/admin/audit/prune`, `admin:write`)
  enforces `AURA_AUDIT_RETENTION_DAYS` (default 90) and keeps
  timestamp-less lines. The trail is written to `AURA_AUDIT_LOG`
  (`/var/lib/aura/audit/aura-audit.jsonl` in the Compose stack) on the
  persistent `api-audit` volume, so it survives container recreation.
- Usage (`GET /api/v1/admin/usage`, `admin:read`) reports live catalogue
  values only — verified available bytes, counts, configured ceilings — with
  `measured_at` as the freshness marker. See `docs/OBSERVABILITY.md` for the
  unavailable metrics and the validations that need real infrastructure.
- Exceptions by design: a successful `login` is emitted before any
  `authorize` call, so its audit event carries `user_id` but no tenant
  (`space_id`/`application_id`). Purge stays owner-scoped for a human caller
  (a user may only purge objects they own); a space-wide purge is performed
  by a service identity holding `objects:purge`.

## Secret handling

- Tokens are `aura_<uuid>_<64-hex>`, where `<uuid>` is a canonical UUID v4
  (`docs/DATA-MODEL.md`); only `sha256(secret)` is stored. A token whose id is not a
  canonical UUID v4 is rejected before any lookup.
- Comparison is constant-time; unknown, revoked and expired tokens all yield 401.
- Failed machine authentication is rate-limited per client IP (default 60/60s).
- Failed human logins are rate-limited per client IP (default 10/60s); only
  failures are counted and a success clears the counter, so legitimate logins
  never lock a client out.
- The client IP is resolved from `X-Forwarded-For` only when the immediate peer
  is a trusted proxy (`AURA_TRUSTED_PROXIES`, default loopback + RFC1918); the
  right-most untrusted entry wins, so a client cannot spoof its own address.
