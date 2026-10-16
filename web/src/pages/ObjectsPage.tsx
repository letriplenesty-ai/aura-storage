import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  ApiError,
  api,
  hasScope,
  putToPresignedUrl,
} from "../lib/api";
import type { FolderItem, ObjectItem, ObjectQuery } from "../lib/api";
import { emptyQueue, startUploadQueue } from "../lib/transfer";
import type {
  QueueInput,
  QueueState,
  TransferDeps,
  TransferPhase,
  UploadQueue,
} from "../lib/transfer";
import {
  MAX_UPLOAD_DEPTH,
  normalizeRelativePath,
  parentDirOf,
  planFolders,
} from "../lib/folderTree";
import { useAuth, useSession } from "../lib/auth";
import { ConfirmAction, Dialog } from "../components/Dialog";
import { makeToast, ToastStack, type Toast } from "../components/Toasts";
import { RowMenu } from "../components/RowMenu";
import { useFolderNav, type FolderActions } from "../components/Layout";

/** Objects fetched per page ("Charger plus" increments the offset). */
const OBJECTS_PAGE_SIZE = 50;

/** Human label for each transfer phase (import → validation → availability). */
const PHASE_LABEL: Record<TransferPhase, string> = {
  idle: "En attente",
  "requesting-intent": "Autorisation…",
  uploading: "Envoi…",
  validating: "Validation…",
  available: "Disponible",
  failed: "Échec",
  cancelled: "Annulé",
};

/** Phases that are still moving (offer a per-file interrupt). */
const ACTIVE_PHASES: TransferPhase[] = ["requesting-intent", "uploading", "validating"];

/** Chip style for a queue item's phase. */
function queuePhaseClass(phase: TransferPhase): string {
  if (phase === "available") return "chip chip-ok";
  if (phase === "failed") return "chip chip-error";
  if (phase === "cancelled" || phase === "idle") return "chip chip-muted";
  return "chip chip-warn";
}

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "Tous les états" },
  { value: "available", label: "Disponible" },
  { value: "pending", label: "En cours" },
  { value: "deleted", label: "Supprimé" },
  { value: "expired", label: "Expiré" },
  { value: "failed", label: "Échec" },
  { value: "aborted", label: "Abandonné" },
];

const SORT_OPTIONS: {
  value: NonNullable<ObjectQuery["sort"]>;
  label: string;
}[] = [
  { value: "created_at", label: "Date" },
  { value: "display_name", label: "Nom" },
  { value: "size_bytes", label: "Taille" },
];

/** Accepted import categories, shown as informative (non-clickable) badges. */
const IMPORT_CATEGORIES = [
  "Documents",
  "Images",
  "Vidéos",
  "Audio",
  "Archives",
  "Autres",
];

/** Common media types offered as suggestions; the field stays free text. */
const CONTENT_TYPE_SUGGESTIONS = [
  "application/pdf",
  "application/json",
  "application/octet-stream",
  "image/jpeg",
  "image/png",
  "text/csv",
  "text/plain",
  "audio/mpeg",
  "video/mp4",
];

/** The "Tous les types" dropdown: exact MIME values the server matches on. */
const TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "Tous les types" },
  { value: "application/pdf", label: "PDF" },
  { value: "image/jpeg", label: "Image JPEG" },
  { value: "image/png", label: "Image PNG" },
  { value: "text/plain", label: "Texte brut" },
  { value: "text/csv", label: "CSV" },
  { value: "application/json", label: "JSON" },
  { value: "audio/mpeg", label: "Audio MP3" },
  { value: "video/mp4", label: "Vidéo MP4" },
  { value: "application/octet-stream", label: "Binaire" },
];

/** Listing density: the register is a dense list by default, a card grid on demand. */
type ViewMode = "list" | "grid";

function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size < 0) {
    return "taille inconnue";
  }
  if (size === 0) {
    return "0 o";
  }
  const units = ["o", "Ko", "Mo", "Go", "To"];
  const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
  const value = size / 1024 ** index;
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

const DATE_FORMAT = new Intl.DateTimeFormat("fr-FR", {
  dateStyle: "medium",
  timeStyle: "short",
});

/** Epoch seconds -> localized label. Invalid/zero values render an em dash. */
function formatDateTime(epoch: number | undefined): string {
  if (epoch === undefined || !Number.isFinite(epoch) || epoch <= 0) {
    return "—";
  }
  return DATE_FORMAT.format(new Date(epoch * 1000));
}

/** "<input type=date>" value -> inclusive epoch-second bound (local day). */
function dateToEpoch(value: string, endOfDay = false): number | undefined {
  if (!value) return undefined;
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) return undefined;
  if (endOfDay) date.setHours(23, 59, 59, 999);
  return Math.floor(date.getTime() / 1000);
}

/** Free-text byte count -> a non-negative integer, or undefined when blank. */
function parseSize(value: string): number | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : undefined;
}

const STATUS_LABELS: Record<string, string> = {
  available: "Disponible",
  pending: "En cours",
  deleted: "Supprimé",
  expired: "Expiré",
  failed: "Échec",
  aborted: "Abandonné",
};

/** Friendly type label for scanning; the raw MIME stays in the details panel. */
function typeLabel(contentType: string): string {
  const value = contentType.toLowerCase();
  if (value === "application/pdf") return "PDF";
  if (value.startsWith("image/")) return "Image";
  if (value.startsWith("audio/")) return "Audio";
  if (value.startsWith("video/")) return "Vidéo";
  if (
    value === "application/zip" ||
    value === "application/gzip" ||
    value === "application/x-tar"
  ) {
    return "Archive";
  }
  if (value === "application/json") return "JSON";
  if (value === "text/csv") return "CSV";
  if (value.startsWith("text/")) return "Texte";
  if (value === "application/octet-stream") return "Binaire";
  return contentType.split("/")[1] ?? contentType;
}

/** Status shown as a dot plus a word, so it never relies on colour alone. */
function StatusLabel({ status }: { status: string }) {
  const variant =
    status === "available"
      ? "status status-ok"
      : status === "pending"
        ? "status status-warn"
        : status === "failed"
          ? "status status-error"
          : status === "deleted"
            ? // Vault token: the deleted pastille stays neutral grey (never the
              // mock's green, so deletion never borrows the activity colour).
              "status vault-dot-deleted-neutre"
            : "status";
  return <span className={variant}>{STATUS_LABELS[status] ?? status}</span>;
}

/** Server-side soft delete accepts only `available` or `pending` rows; anything
 * else is refused with 409. The bulk selection and the per-row "Supprimer"
 * action share this predicate so they never diverge. */
function isDeletable(item: ObjectItem): boolean {
  return item.status === "available" || item.status === "pending";
}

// -- Recursive (cascade) folder deletion ------------------------------------
// The server refuses a folder that still holds files or subfolders (409). The
// only sanctioned way to delete such a folder is to empty it explicitly: this
// flow removes every object of the subtree, then the now-empty folders from the
// leaves up to the target. Nothing is ever reported as deleted until the API
// confirms it, and nothing is removed without an explicit confirmation.

/** One file in the target subtree, resolved through real list requests. */
interface CascadeObject {
  id: string;
  name: string;
  folderId: string;
  /** Human path ("Racine / Dossier / Sous-dossier"). */
  path: string;
}

/** One folder of the target subtree; the target itself is included. */
interface CascadeFolder {
  id: string;
  name: string;
  parentId: string | null;
  path: string;
}

/** The real content snapshot of a folder (never guessed, always listed). */
interface CascadePlan {
  rootId: string;
  rootName: string;
  objects: CascadeObject[];
  folders: CascadeFolder[];
  fileCount: number;
  /** Descendant folders only: the target is not its own "subfolder". */
  folderCount: number;
}

/** One delete step, ordered so a folder is emptied before it is removed. */
interface CascadeOp {
  kind: "object" | "folder";
  id: string;
  label: string;
  path: string;
}

/** A step that failed; kept so the user can retry or abandon it explicitly. */
interface CascadeFailure {
  key: string;
  kind: "object" | "folder";
  id: string;
  label: string;
  path: string;
  error: string;
}

type CascadePhase = "scanning" | "confirm" | "deleting" | "done";

interface CascadeState {
  rootId: string;
  rootName: string;
  phase: CascadePhase;
  plan: CascadePlan | null;
  ops: CascadeOp[];
  done: number;
  /** Path of the item currently being processed (live progress). */
  current: string | null;
  failures: CascadeFailure[];
  cancelled: boolean;
  scanError: string | null;
}

/** The server caps `limit` at 100; page until a short page comes back. */
const CASCADE_PAGE_SIZE = 100;

/** Every object filed directly in one folder, across all pages. */
async function fetchFolderObjects(folderId: string): Promise<ObjectItem[]> {
  const out: ObjectItem[] = [];
  let offset = 0;
  // Hard guard: a server that ignores `offset` can never spin forever.
  for (let guard = 0; guard < 1000; guard += 1) {
    const page = await api.listObjects({
      folderId,
      limit: CASCADE_PAGE_SIZE,
      offset,
    });
    out.push(...page.items);
    if (page.items.length < CASCADE_PAGE_SIZE) break;
    offset += page.items.length;
  }
  return out;
}

/** Build the real content snapshot of `rootId`: descendant folders + files. */
async function scanCascade(rootId: string, rootName: string): Promise<CascadePlan> {
  const all = await api.listAllFolders();
  const byId = new Map(all.items.map((folder) => [folder.id, folder]));
  const childMap = new Map<string | null, FolderItem[]>();
  for (const folder of all.items) {
    const key = folder.parent_id ?? null;
    const bucket = childMap.get(key);
    if (bucket) bucket.push(folder);
    else childMap.set(key, [folder]);
  }
  const pathOf = (id: string): string => {
    const names: string[] = [];
    let cursor: string | null = id;
    let guard = 0;
    while (cursor && guard < 64) {
      const folder: FolderItem | undefined = byId.get(cursor);
      if (!folder) break;
      names.unshift(folder.name);
      cursor = folder.parent_id;
      guard += 1;
    }
    return ["Racine", ...names].join(" / ");
  };
  const folders: CascadeFolder[] = [];
  const seen = new Set<string>();
  const stack: string[] = [rootId];
  while (stack.length > 0) {
    const id = stack.pop();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const folder = byId.get(id);
    if (!folder) continue;
    folders.push({
      id,
      name: folder.name,
      parentId: folder.parent_id,
      path: pathOf(id),
    });
    for (const child of childMap.get(id) ?? []) stack.push(child.id);
  }
  const objects: CascadeObject[] = [];
  for (const folder of folders) {
    const list = await fetchFolderObjects(folder.id);
    for (const item of list) {
      objects.push({
        id: item.id,
        name: item.display_name,
        folderId: folder.id,
        path: folder.path,
      });
    }
  }
  return {
    rootId,
    rootName,
    objects,
    folders,
    fileCount: objects.length,
    folderCount: Math.max(0, folders.length - 1),
  };
}

/** Flatten the snapshot into a leaf-first (post-order) list of delete steps:
 * a folder's files, its subfolders (recursively), then the folder itself, so
 * every folder is empty by the time its own DELETE is issued. */
function planCascadeOps(plan: CascadePlan): CascadeOp[] {
  const inTree = new Set(plan.folders.map((folder) => folder.id));
  const children = new Map<string, CascadeFolder[]>();
  for (const folder of plan.folders) {
    if (folder.id === plan.rootId) continue;
    const parent = folder.parentId;
    if (!parent || !inTree.has(parent)) continue;
    const bucket = children.get(parent);
    if (bucket) bucket.push(folder);
    else children.set(parent, [folder]);
  }
  const objectsByFolder = new Map<string, CascadeObject[]>();
  for (const object of plan.objects) {
    const bucket = objectsByFolder.get(object.folderId);
    if (bucket) bucket.push(object);
    else objectsByFolder.set(object.folderId, [object]);
  }
  const byId = new Map(plan.folders.map((folder) => [folder.id, folder]));
  const ops: CascadeOp[] = [];
  const visit = (folderId: string) => {
    for (const object of objectsByFolder.get(folderId) ?? []) {
      ops.push({
        kind: "object",
        id: object.id,
        label: object.name,
        path: object.path,
      });
    }
    for (const child of children.get(folderId) ?? []) visit(child.id);
    const self = byId.get(folderId);
    if (self) {
      ops.push({ kind: "folder", id: self.id, label: self.name, path: self.path });
    }
  };
  visit(plan.rootId);
  return ops;
}

/** Stable identity of a step, so a successful retry clears its failure entry. */
function cascadeOpKey(op: { kind: string; id: string }): string {
  return `${op.kind}:${op.id}`;
}

/** The whole folder tree is fetched in one request (`?all=true`); the hierarchy
 * is rebuilt client-side from parent_id for breadcrumbs and the move pickers. */
export function ObjectsPage() {
  const { csrf } = useAuth();
  const session = useSession();
  const queryClient = useQueryClient();

  // Folder selection lives in the shell (Layout) so its sidebar can navigate
  // and trigger the page-owned folder flows registered below.
  const { currentFolderId, setCurrentFolderId, registerFolderActions } =
    useFolderNav();
  // Latest folder handlers, delegated to by the shell-registered actions so the
  // registration below stays mount-once while reading fresh closures.
  const folderActionsRef = useRef<FolderActions | null>(null);
  useEffect(() => {
    registerFolderActions({
      rename: (folder) => folderActionsRef.current?.rename(folder),
      move: (folder) => folderActionsRef.current?.move(folder),
      remove: (folder) => folderActionsRef.current?.remove(folder),
    });
    return () => registerFolderActions(null);
  }, [registerFolderActions]);
  // Uploads run through the M03 transfer engine as a bounded multi-file queue.
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [queue, setQueue] = useState<QueueState>(() => emptyQueue());
  const queueRef = useRef<UploadQueue | null>(null);
  const dirInputRef = useRef<HTMLInputElement | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // A refused (409, non-empty) folder deletion is actionable: keep the target
  // so the user can empty it and retry, instead of losing the failure.
  const [folderDeleteError, setFolderDeleteError] = useState<{
    id: string;
    name: string;
  } | null>(null);
  // Recursive folder deletion: opened from the 409 refusal or from the
  // "Supprimer avec son contenu" option; always explicit and confirmed.
  const [folderDeleteWithContent, setFolderDeleteWithContent] = useState(false);
  const [cascade, setCascade] = useState<CascadeState | null>(null);
  const [cascadeConfirmText, setCascadeConfirmText] = useState("");
  // Mirror of the live cascade state, read/written by the async run loop where
  // a state updater would otherwise close over a stale render.
  const cascadeRef = useRef<CascadeState | null>(null);
  const cascadeCancelRef = useRef(false);
  const cascadeDialogRef = useRef<HTMLDialogElement | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);

  function dismissToast(id: number) {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }

  function notify(kind: Toast["kind"], text: string) {
    setToasts((current) => [...current.slice(-4), makeToast(kind, text)]);
  }
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");
  const [renameTarget, setRenameTarget] = useState<FolderItem | null>(null);
  const [renameName, setRenameName] = useState("");
  const [moveTarget, setMoveTarget] = useState<FolderItem | null>(null);
  const [moveObjectTarget, setMoveObjectTarget] = useState<ObjectItem | null>(null);
  const [details, setDetails] = useState<ObjectItem | null>(null);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [pendingConfirm, setPendingConfirm] = useState<{
    title: string;
    message: ReactNode;
    confirmLabel: string;
    danger?: boolean;
    /** Folder deletion: offer the "with its content" recursive option. */
    folderCascade?: boolean;
    /** `withContent` is only meaningful for a folder deletion confirmation. */
    onConfirm: (withContent: boolean) => void;
  } | null>(null);

  // -- search / filter / sort (server-side; M02) ---------------------------
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [minSize, setMinSize] = useState("");
  const [maxSize, setMaxSize] = useState("");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [sortField, setSortField] = useState<NonNullable<ObjectQuery["sort"]>>(
    "created_at",
  );
  const [sortOrder, setSortOrder] = useState<NonNullable<ObjectQuery["order"]>>("desc");

  // Debounce the name filter so typing does not fire one request per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const filters = useMemo<ObjectQuery>(() => {
    const query: ObjectQuery = { folderId: currentFolderId };
    if (debouncedSearch) query.q = debouncedSearch;
    if (statusFilter) query.status = statusFilter;
    if (typeFilter.trim()) query.contentType = typeFilter.trim();
    const min = parseSize(minSize);
    const max = parseSize(maxSize);
    if (min !== undefined) query.minSize = min;
    if (max !== undefined) query.maxSize = max;
    const after = dateToEpoch(fromDate);
    const before = dateToEpoch(toDate, true);
    if (after !== undefined) query.createdAfter = after;
    if (before !== undefined) query.createdBefore = before;
    query.sort = sortField;
    query.order = sortOrder;
    return query;
  }, [
    currentFolderId,
    debouncedSearch,
    statusFilter,
    typeFilter,
    minSize,
    maxSize,
    fromDate,
    toDate,
    sortField,
    sortOrder,
  ]);

  const hasActiveFilters =
    debouncedSearch !== "" ||
    statusFilter !== "" ||
    typeFilter.trim() !== "" ||
    minSize.trim() !== "" ||
    maxSize.trim() !== "" ||
    fromDate !== "" ||
    toDate !== "";

  /** Advanced filters moved off the default screen; the count badges the toggle. */
  const advancedCount = [typeFilter, minSize, maxSize, fromDate, toDate].filter(
    (value) => value.trim() !== "",
  ).length;

  function resetFilters() {
    setSearch("");
    setStatusFilter("");
    setTypeFilter("");
    setMinSize("");
    setMaxSize("");
    setFromDate("");
    setToDate("");
  }

  const canPurge = hasScope(session.data?.scopes, "objects:purge");

  // Recursive deletion deletes objects (objects:delete) inside folders the
  // caller owns (objects:write); it is a human-only flow, like folders. When
  // either scope is missing the option is never offered.
  const canCascadeDelete =
    session.data?.kind === "user" &&
    hasScope(session.data?.scopes, "objects:delete") &&
    hasScope(session.data?.scopes, "objects:write");

  /** True while any file of the queue is transferring (blocks double submit). */
  const transferActive = queue.active > 0;

  /** Wire the injected engine to the real API for one authenticated session. */
  function transferDeps(activeCsrf: string): TransferDeps {
    return {
      requestIntent: (input) =>
        api.createUploadIntent(
          {
            display_name: input.fileName,
            content_type: input.contentType,
            size_bytes: input.sizeBytes,
            folder_id: input.folderId,
          },
          activeCsrf,
          { idempotencyKey: input.idempotencyKey },
        ),
      upload: (url, selected, opts) =>
        putToPresignedUrl(url, selected, {
          headers: opts.headers,
          onProgress: opts.onProgress,
          signal: opts.signal,
        }),
      commit: (id) => api.commitObject(id, activeCsrf),
      abortIntent: async (id) => {
        await api.abortObject(id, activeCsrf);
      },
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
      random: Math.random,
    };
  }

  const treeQuery = useQuery({
    queryKey: ["folders", "all"],
    queryFn: () => api.listAllFolders(),
  });
  const objectsQuery = useInfiniteQuery({
    queryKey: ["objects", filters],
    queryFn: ({ pageParam }) =>
      api.listObjects({ ...filters, limit: OBJECTS_PAGE_SIZE, offset: pageParam }),
    initialPageParam: 0,
    getNextPageParam: (lastPage, _pages, lastOffset) =>
      lastPage.items.length === OBJECTS_PAGE_SIZE
        ? lastOffset + lastPage.items.length
        : undefined,
  });

  const tree = treeQuery.data?.items ?? [];
  const byId = useMemo(() => new Map(tree.map((f) => [f.id, f])), [tree]);
  const childrenOf = useMemo(() => {
    const map = new Map<string | null, FolderItem[]>();
    for (const folder of tree) {
      const key = folder.parent_id;
      const bucket = map.get(key);
      if (bucket) {
        bucket.push(folder);
      } else {
        map.set(key, [folder]);
      }
    }
    return map;
  }, [tree]);

  const breadcrumb = useMemo(() => {
    const path: FolderItem[] = [];
    let cursor = currentFolderId;
    let guard = 0;
    while (cursor && guard < 64) {
      const folder = byId.get(cursor);
      if (!folder) break;
      path.unshift(folder);
      cursor = folder.parent_id;
      guard += 1;
    }
    return path;
  }, [byId, currentFolderId]);

  /** Human-readable folder path for an object ("Racine / Dossier / Sous-dossier"). */
  function folderPath(folderId: string | null): string {
    if (!folderId) return "Racine";
    const names: string[] = [];
    let cursor: string | null = folderId;
    let guard = 0;
    while (cursor && guard < 64) {
      const folder: FolderItem | undefined = byId.get(cursor);
      if (!folder) break;
      names.unshift(folder.name);
      cursor = folder.parent_id;
      guard += 1;
    }
    return ["Racine", ...names].join(" / ");
  }

  function invalidateLibrary() {
    void queryClient.invalidateQueries({ queryKey: ["objects"] });
    void queryClient.invalidateQueries({ queryKey: ["folders"] });
  }

  /** Manual refresh from the header (and the header overflow menu). */
  function refreshLibrary() {
    setActionError(null);
    void queryClient.invalidateQueries({ queryKey: ["objects"] });
    void queryClient.invalidateQueries({ queryKey: ["folders"] });
  }

  function reportError(err: unknown, fallback: string) {
    setNotice(null);
    setActionError(err instanceof ApiError ? err.detail : fallback);
  }

  // -- mutations -----------------------------------------------------------
  const createFolderMutation = useMutation({
    mutationFn: (name: string) => {
      if (!csrf) throw new Error("Session expirée.");
      return api.createFolder({ name, parent_id: currentFolderId }, csrf);
    },
    onSuccess: () => {
      setNewFolderOpen(false);
      setNewFolderName("");
      setNotice("Dossier créé.");
      invalidateLibrary();
    },
    onError: (err) => reportError(err, "Création du dossier impossible."),
  });

  const renameMutation = useMutation({
    mutationFn: (input: { id: string; name: string }) => {
      if (!csrf) throw new Error("Session expirée.");
      return api.updateFolder(input.id, { name: input.name }, csrf);
    },
    onSuccess: () => {
      setRenameTarget(null);
      setNotice("Dossier renommé.");
      invalidateLibrary();
    },
    onError: (err) => reportError(err, "Renommage impossible."),
  });

  const moveMutation = useMutation({
    mutationFn: (input: { id: string; parentId: string | null }) => {
      if (!csrf) throw new Error("Session expirée.");
      return api.updateFolder(input.id, { parent_id: input.parentId }, csrf);
    },
    onSuccess: () => {
      setMoveTarget(null);
      setNotice("Dossier déplacé.");
      invalidateLibrary();
    },
    onError: (err) => reportError(err, "Déplacement impossible."),
  });

  const moveObjectMutation = useMutation({
    mutationFn: (input: { id: string; folderId: string | null }) => {
      if (!csrf) throw new Error("Session expirée.");
      return api.moveObject(input.id, input.folderId, csrf);
    },
    onSuccess: () => {
      setMoveObjectTarget(null);
      setDetails(null);
      setNotice("Objet déplacé.");
      invalidateLibrary();
    },
    onError: (err) => reportError(err, "Déplacement de l'objet impossible."),
  });

  // Folder delete is confirmed against the server: a non-empty folder (409) is
  // surfaced as an actionable refusal (friendly FR + retry), never a fake success.
  const deleteFolderMutation = useMutation({
    mutationFn: (input: { id: string; name: string }) => {
      if (!csrf) throw new Error("Session expirée.");
      return api.deleteFolder(input.id, csrf);
    },
    onSuccess: (_data, input) => {
      if (currentFolderId === input.id) setCurrentFolderId(null);
      setFolderDeleteError(null);
      setNotice("Dossier supprimé.");
      invalidateLibrary();
    },
    onError: (err, input) => {
      setNotice(null);
      if (err instanceof ApiError && err.status === 409) {
        // The server refuses a folder that still holds files or subfolders.
        setActionError(null);
        setFolderDeleteError({ id: input.id, name: input.name });
        return;
      }
      setFolderDeleteError(null);
      reportError(err, "Suppression du dossier impossible.");
    },
  });

  // Delete is confirmed against the server: the row is only refreshed (never
  // removed locally) once the API reports success.
  const deleteObjectMutation = useMutation({
    mutationFn: (id: string) => {
      if (!csrf) throw new Error("Session expirée.");
      return api.deleteObject(id, csrf);
    },
    onSuccess: () => {
      setDetails(null);
      notify("ok", "Objet marqué comme supprimé. Les octets restent jusqu'à une purge.");
      invalidateLibrary();
    },
    onError: (err) => reportError(err, "Suppression impossible."),
  });

  // Bulk delete reuses the audited single-object endpoint once per selected
  // file (no bulk API surface): sequential, per-file outcome, server truth.
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const deleteManyMutation = useMutation({
    mutationFn: async (ids: string[]) => {
      if (!csrf) throw new Error("Session expirée.");
      const failed: string[] = [];
      let deleted = 0;
      for (const id of ids) {
        try {
          await api.deleteObject(id, csrf);
          deleted += 1;
        } catch {
          const target = items.find((item) => item.id === id);
          failed.push(target?.display_name ?? id);
        }
      }
      return { deleted, failed };
    },
    onSuccess: ({ deleted, failed }) => {
      setSelectedIds(new Set());
      setDetails(null);
      if (failed.length === 0) {
        notify(
          "ok",
          `${deleted} objet(s) marqué(s) comme supprimé(s). Les octets restent jusqu'à une purge.`,
        );
      } else {
        notify(
          "error",
          `${deleted} supprimé(s), ${failed.length} échec(s) : ${failed.slice(0, 5).join(", ")}${failed.length > 5 ? "…" : ""}`,
        );
      }
      invalidateLibrary();
    },
    onError: (err) => reportError(err, "Suppression en masse impossible."),
  });

  const purgeMutation = useMutation({
    mutationFn: (id: string) => {
      if (!csrf) throw new Error("Session expirée.");
      return api.purgeObject(id, csrf);
    },
    onSuccess: () => {
      setDetails(null);
      setNotice("Objet purgé définitivement.");
      invalidateLibrary();
    },
    onError: (err) => reportError(err, "Purge impossible."),
  });

  // Abandon a stuck pending upload intent: the backend deletes the partial
  // bytes and the row leaves the "En cours" state. Re-uploading the same
  // file afterwards mints a fresh intent.
  const abortUploadMutation = useMutation({
    mutationFn: (id: string) => {
      if (!csrf) throw new Error("Session expirée.");
      return api.abortObject(id, csrf);
    },
    onSuccess: () => {
      setDetails(null);
      setNotice("Envoi abandonné, octets partiels supprimés.");
      invalidateLibrary();
    },
    onError: (err) => reportError(err, "Abandon impossible."),
  });

  // -- handlers ------------------------------------------------------------
  /** Shared queue subscriber: mirrors state and reacts when it settles. */
  function handleQueueUpdate(next: QueueState) {
    setQueue(next);
    if (!next.finished) return;
    if (next.done > 0) {
      invalidateLibrary();
      notify("ok", `${next.done} fichier(s) vérifié(s) et disponible(s).`);
    }
    if (next.failed > 0) {
      const firstFailed = next.items.find((item) => item.state.phase === "failed");
      setUploadError(firstFailed?.state.error ?? "Envoi impossible.");
    } else {
      setUploadError(null);
    }
  }

  function handleUpload(event: FormEvent) {
    event.preventDefault();
    setUploadError(null);
    setNotice(null);
    if (pendingFiles.length === 0) {
      setUploadError("Choisissez au moins un fichier.");
      return;
    }
    if (transferActive) {
      // A submit can still reach here (e.g. Enter in the form) even though the
      // button is disabled: say why instead of returning silently.
      setUploadError(
        "Un transfert est déjà en cours : attendez sa fin ou annulez-le avant de relancer l'import.",
      );
      return;
    }
    if (!csrf) {
      setUploadError("Session expirée. Reconnectez-vous.");
      return;
    }
    const uploadQueue = startUploadQueue(pendingFiles, transferDeps(csrf), {
      folderId: currentFolderId,
      onUpdate: handleQueueUpdate,
    });
    queueRef.current = uploadQueue;
    // The queue now owns the submitted files: clear them so anything dropped
    // during the transfer stays visible as a real backlog for the next run.
    setPendingFiles([]);
  }

  /** Directory import (#64): recreate the missing folders, then upload in place. */
  async function handleFolderUpload(selected: FileList | null) {
    if (!selected || selected.length === 0) return;
    const list = Array.from(selected);
    const relPaths = list.map((file) =>
      normalizeRelativePath(
        (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
      ),
    );
    await uploadRelPaths(list, relPaths);
  }

  /** Shared core: plan folders, create them, queue files with folder ids. */
  async function uploadRelPaths(list: File[], relPaths: string[]) {
    if (!csrf) {
      setUploadError("Session expirée. Reconnectez-vous.");
      return;
    }
    if (transferActive) return;
    setUploadError(null);
    setNotice(null);

    const plan = planFolders(relPaths);
    const tooDeep = new Set(plan.tooDeep);
    const kept = list
      .map((file, index) => ({ file, rel: relPaths[index] ?? file.name }))
      .filter((item) => !tooDeep.has(item.rel));
    if (kept.length === 0) {
      setUploadError("Aucun fichier importable dans cette sélection.");
      return;
    }

    const keyOf = (parentId: string | null, name: string) =>
      `${parentId ?? "root"}/${name}`;
    const folderIds = new Map<string, string>(); // relative path -> id
    const existing = new Map<string, string>(); // parentId/name -> id
    for (const folder of tree) {
      existing.set(keyOf(folder.parent_id, folder.name), folder.id);
    }

    let createdAny = false;
    for (const folder of plan.folders) {
      const parentId = folder.parentPath
        ? (folderIds.get(folder.parentPath) ?? currentFolderId)
        : currentFolderId;
      const key = keyOf(parentId, folder.name);
      const known = existing.get(key);
      if (known) {
        folderIds.set(folder.path, known);
        continue;
      }
      try {
        const made = await api.createFolder(
          { name: folder.name, parent_id: parentId },
          csrf,
        );
        existing.set(key, made.id);
        folderIds.set(folder.path, made.id);
        createdAny = true;
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          const all = await api.listAllFolders();
          const found = all.items.find(
            (candidate) =>
              (candidate.parent_id ?? null) === (parentId ?? null) &&
              candidate.name === folder.name,
          );
          if (found) {
            existing.set(key, found.id);
            folderIds.set(folder.path, found.id);
            continue;
          }
        }
        setUploadError("Création de l'arborescence impossible.");
        return;
      }
    }
    if (createdAny) invalidateLibrary();
    if (plan.tooDeep.length > 0) {
      setNotice(
        `${plan.tooDeep.length} fichier(s) ignoré(s) : arborescence trop profonde (max ${MAX_UPLOAD_DEPTH}).`,
      );
    }

    const entries: QueueInput[] = kept.map((item) => {
      const dir = parentDirOf(item.rel);
      return {
        file: item.file,
        folderId: dir ? (folderIds.get(dir) ?? currentFolderId) : currentFolderId,
      };
    });
    const uploadQueue = startUploadQueue(entries, transferDeps(csrf), {
      folderId: currentFolderId,
      onUpdate: handleQueueUpdate,
    });
    queueRef.current = uploadQueue;
  }

  function handleCancelUpload() {
    queueRef.current?.cancelAll();
  }

  function handleRetryFailed() {
    setUploadError(null);
    queueRef.current?.retryFailed();
  }

  function handleFilesChange(selected: FileList | File[] | null) {
    const list = selected ? Array.from(selected) : [];
    if (list.length === 0) return;
    setUploadError(null);
    setPendingFiles((current) => [...current, ...list]);
  }

  // Drop visual state (P0.3): a counter avoids flicker when crossing children.
  const [dragDepth, setDragDepth] = useState(0);

  /** Walk dropped entries: files keep their folder-relative path (P0.2).
   * Empty (0 o) files and empty folders are skipped, but counted, so the caller
   * can tell the two cases apart instead of showing one vague message. */
  async function collectDroppedEntries(items: DataTransferItemList): Promise<{
    entries: { file: File; rel: string }[];
    emptyFiles: number;
    emptyFolders: number;
  }> {
    const out: { file: File; rel: string }[] = [];
    let emptyFiles = 0;
    let emptyFolders = 0;
    const readBatch = (reader: FileSystemDirectoryReader) =>
      new Promise<FileSystemEntry[]>((resolve) => reader.readEntries(resolve));
    // Returns how many importable files this entry's subtree contributed.
    const walk = async (entry: FileSystemEntry, base: string): Promise<number> => {
      if (entry.isFile) {
        const file = await new Promise<File | null>((resolve) =>
          (entry as FileSystemFileEntry).file(resolve, () => resolve(null)),
        );
        if (!file) return 0;
        if (file.size === 0) {
          // A 0-o file is not importable: the backend refuses empty objects
          // unless the policy explicitly allows them (AURA_ALLOW_EMPTY_OBJECTS).
          emptyFiles += 1;
          return 0;
        }
        out.push({ file, rel: `${base}${entry.name}` });
        return 1;
      }
      if (entry.isDirectory) {
        const reader = (entry as FileSystemDirectoryEntry).createReader();
        let found = 0;
        for (;;) {
          const batch = await readBatch(reader);
          if (batch.length === 0) break;
          for (const child of batch) {
            found += await walk(child, `${base}${entry.name}/`);
          }
        }
        // A dropped folder with no importable file anywhere is an empty folder.
        if (found === 0) emptyFolders += 1;
        return found;
      }
      return 0;
    };
    for (const item of Array.from(items)) {
      const entry = item.webkitGetAsEntry?.();
      if (entry) {
        await walk(entry, "");
        continue;
      }
      const file = item.getAsFile();
      if (!file) continue;
      if (file.size === 0) {
        emptyFiles += 1;
      } else {
        out.push({ file, rel: file.name });
      }
    }
    return {
      entries: out.map(({ file, rel }) => ({
        file,
        rel: normalizeRelativePath(rel),
      })),
      emptyFiles,
      emptyFolders,
    };
  }

  /** Drop intake: loose files buffer, folders reuse the folder pipeline. */
  async function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragDepth(0);
    const { entries, emptyFiles, emptyFolders } = await collectDroppedEntries(
      e.dataTransfer.items,
    );
    if (entries.length === 0) {
      // Distinguish a 0-o file from an empty folder, and recall the backend
      // policy (empty objects are refused unless explicitly allowed).
      const emptyFileNote = `${emptyFiles} fichier(s) vide(s) (0 o) ignoré(s) — le backend refuse les objets vides tant que la politique ne les autorise pas`;
      let message = "Aucun fichier importable dans ce dépôt.";
      if (emptyFiles > 0 && emptyFolders > 0) {
        message = `Aucun fichier importable : ${emptyFileNote}, et ${emptyFolders} dossier(s) vide(s) ignoré(s).`;
      } else if (emptyFiles > 0) {
        message = `Aucun fichier importable : ${emptyFileNote}.`;
      } else if (emptyFolders > 0) {
        message = `Aucun fichier importable : ${emptyFolders} dossier(s) vide(s) ignoré(s) (aucun fichier à l'intérieur).`;
      }
      setUploadError(message);
      return;
    }
    const hasFolders = entries.some((entry) => entry.rel.includes("/"));
    if (transferActive) {
      const loose = entries.filter((entry) => !entry.rel.includes("/"));
      if (loose.length > 0) {
        // Buffer loose files for the next run and say so (P0.1): never
        // swallow a drop silently.
        setUploadError(null);
        setPendingFiles((current) => [
          ...current,
          ...loose.map((entry) => entry.file),
        ]);
        notify(
          "info",
          `Transfert en cours — ${loose.length} fichier(s) ajouté(s) à la file d'attente.` +
            (hasFolders ? " Les dossiers attendront la fin du transfert." : ""),
        );
      } else {
        // Folder creation needs a free queue: say so instead of swallowing.
        setUploadError(
          "Un transfert est en cours : attendez sa fin pour déposer un dossier.",
        );
      }
      return;
    }
    if (hasFolders) {
      await uploadRelPaths(
        entries.map((entry) => entry.file),
        entries.map((entry) => entry.rel),
      );
    } else {
      handleFilesChange(entries.map((entry) => entry.file));
    }
  }

  function removePendingFile(index: number) {
    setPendingFiles((current) => current.filter((_, i) => i !== index));
  }

  async function handleDownload(item: ObjectItem) {
    setActionError(null);
    if (!csrf) {
      setActionError("Session expirée. Reconnectez-vous.");
      return;
    }
    setDownloadingId(item.id);
    try {
      const intent = await api.createDownloadIntent(item.id, csrf);
      // The browser performs the direct S3 GET; it manages its own progress.
      window.open(intent.download_url, "_blank", "noopener");
      notify(
        "ok",
        `Téléchargement de « ${intent.display_name} » lancé (${formatBytes(
          intent.size_bytes,
        )}).`,
      );
    } catch (err) {
      setActionError(
        err instanceof ApiError
          ? `Téléchargement refusé : ${err.detail}`
          : "Téléchargement impossible.",
      );
    } finally {
      setDownloadingId(null);
    }
  }

  async function handleCopyReference(item: ObjectItem) {
    setActionError(null);
    try {
      await navigator.clipboard.writeText(item.id);
      setNotice(`Référence copiée : ${item.id}`);
    } catch {
      setActionError("Copie impossible dans ce navigateur.");
    }
  }

  /** Folders that cannot be a move target: the folder itself and its subtree. */
  function forbiddenTargets(rootId: string): Set<string> {
    const out = new Set<string>([rootId]);
    const queue = [rootId];
    while (queue.length > 0) {
      const node = queue.shift();
      for (const child of childrenOf.get(node ?? null) ?? []) {
        if (!out.has(child.id)) {
          out.add(child.id);
          queue.push(child.id);
        }
      }
    }
    return out;
  }

  const items = objectsQuery.data?.pages.flatMap((page) => page.items) ?? [];

  // Only deletable rows are selectable: the server refuses anything else
  // with 409, so offering them would manufacture failures.
  const selectableItems = items.filter(isDeletable);
  const selectableIds = new Set(selectableItems.map((item) => item.id));

  const currentName = currentFolderId ? byId.get(currentFolderId)?.name : null;

  const moveBlocked = moveTarget ? forbiddenTargets(moveTarget.id) : new Set<string>();
  const deletePendingId = deleteObjectMutation.isPending
    ? deleteObjectMutation.variables
    : undefined;
  const purgePendingId = purgeMutation.isPending ? purgeMutation.variables : undefined;
  const abortPendingId = abortUploadMutation.isPending
    ? abortUploadMutation.variables
    : undefined;
  const objectBusy =
    deleteObjectMutation.isPending ||
    purgeMutation.isPending ||
    abortUploadMutation.isPending ||
    moveObjectMutation.isPending ||
    downloadingId !== null;

  // -- recursive (cascade) folder deletion ---------------------------------
  /** Keep the React state and the run-loop mirror in lockstep. */
  function applyCascade(next: CascadeState | null) {
    cascadeRef.current = next;
    setCascade(next);
  }

  /** Open the cascade flow for `target`: scan its real content first. */
  function openCascade(target: { id: string; name: string }) {
    setPendingConfirm(null);
    setFolderDeleteError(null);
    setFolderDeleteWithContent(false);
    setCascadeConfirmText("");
    cascadeCancelRef.current = false;
    applyCascade({
      rootId: target.id,
      rootName: target.name,
      phase: "scanning",
      plan: null,
      ops: [],
      done: 0,
      current: null,
      failures: [],
      cancelled: false,
      scanError: null,
    });
    void runCascadeScan(target.id, target.name);
  }

  /** List (never guess) the target's content; failure is surfaced, not hidden. */
  async function runCascadeScan(rootId: string, rootName: string) {
    const current = cascadeRef.current;
    if (!current) return;
    applyCascade({ ...current, phase: "scanning", plan: null, scanError: null });
    try {
      const plan = await scanCascade(rootId, rootName);
      const live = cascadeRef.current;
      if (!live || live.rootId !== rootId || live.phase !== "scanning") return;
      applyCascade({ ...live, phase: "confirm", plan, ops: planCascadeOps(plan) });
    } catch (err) {
      const live = cascadeRef.current;
      if (!live || live.rootId !== rootId || live.phase !== "scanning") return;
      applyCascade({
        ...live,
        phase: "confirm",
        scanError:
          err instanceof ApiError
            ? `Analyse impossible : ${err.detail}`
            : "Analyse du contenu impossible.",
      });
    }
  }

  /** The typed confirmation matched: start the confirmed run. */
  function startCascade() {
    const current = cascadeRef.current;
    if (!current || current.phase !== "confirm" || !current.plan) return;
    void executeCascade([...current.ops]);
  }

  /** Run the ordered steps sequentially, recording each real server outcome. */
  async function executeCascade(opsToRun: CascadeOp[]) {
    const start = cascadeRef.current;
    if (!start) return;
    const activeCsrf = csrf;
    if (!activeCsrf) {
      notify("error", "Session expirée. Reconnectez-vous pour supprimer.");
      applyCascade({
        ...start,
        phase: "done",
        current: null,
        scanError: "Session expirée.",
      });
      return;
    }
    cascadeCancelRef.current = false;
    let done = start.done;
    let failures = start.failures;
    applyCascade({ ...start, phase: "deleting", current: null, cancelled: false });

    for (const op of opsToRun) {
      if (cascadeCancelRef.current) {
        // Cancellation lands between items: whatever the server confirmed stays
        // deleted, nothing local is faked.
        const live = cascadeRef.current ?? start;
        applyCascade({ ...live, done, failures, current: null, cancelled: true });
        break;
      }
      const key = cascadeOpKey(op);
      applyCascade({
        ...(cascadeRef.current ?? start),
        done,
        failures,
        current: op.path,
      });
      try {
        if (op.kind === "object") await api.deleteObject(op.id, activeCsrf);
        else await api.deleteFolder(op.id, activeCsrf);
        done += 1;
        failures = failures.filter((failure) => failure.key !== key);
      } catch (err) {
        const detail = err instanceof ApiError ? err.detail : "Échec de suppression.";
        failures = [
          ...failures.filter((failure) => failure.key !== key),
          { key, kind: op.kind, id: op.id, label: op.label, path: op.path, error: detail },
        ];
      }
      applyCascade({
        ...(cascadeRef.current ?? start),
        done,
        failures,
        current: null,
      });
    }

    const end = cascadeRef.current ?? start;
    invalidateLibrary();
    applyCascade({ ...end, phase: "done", current: null });

    const final = cascadeRef.current;
    if (!final) return;
    if (final.cancelled) {
      notify(
        "info",
        `Suppression interrompue : ${final.done} élément(s) supprimé(s), ${final.failures.length} échec(s).`,
      );
    } else if (final.failures.length === 0) {
      notify("ok", `Dossier et contenu supprimés : ${final.done} élément(s).`);
    } else {
      notify(
        "error",
        `${final.done} élément(s) supprimé(s), ${final.failures.length} échec(s) : ouvrez le journal pour réessayer.`,
      );
    }
  }

  function retryCascadeFailures() {
    const current = cascadeRef.current;
    if (!current || current.phase !== "done" || current.failures.length === 0) return;
    void executeCascade(
      current.failures.map((failure) => ({
        kind: failure.kind,
        id: failure.id,
        label: failure.label,
        path: failure.path,
      })),
    );
  }

  function retryCascadeItem(failure: CascadeFailure) {
    const current = cascadeRef.current;
    if (!current || current.phase !== "done") return;
    void executeCascade([
      { kind: failure.kind, id: failure.id, label: failure.label, path: failure.path },
    ]);
  }

  function abandonCascadeItem(failure: CascadeFailure) {
    const current = cascadeRef.current;
    if (!current) return;
    applyCascade({
      ...current,
      failures: current.failures.filter((entry) => entry.key !== failure.key),
    });
  }

  /** Request a stop; it is honoured between items by the run loop. */
  function cancelCascade() {
    cascadeCancelRef.current = true;
  }

  function closeCascade() {
    if (cascadeRef.current?.phase === "deleting") {
      cascadeCancelRef.current = true;
      return;
    }
    applyCascade(null);
  }

  // The cascade modal is a native <dialog> so Escape/focus trapping come for
  // free; it is shown while a cascade state exists and closed when it clears.
  useEffect(() => {
    const el = cascadeDialogRef.current;
    if (!el) return;
    if (cascade && !el.open) {
      el.showModal();
    } else if (!cascade && el.open) {
      el.close();
    }
  }, [cascade]);

  const cascadeConfirmed =
    cascade !== null &&
    cascade.plan !== null &&
    cascade.rootName.trim() !== "" &&
    cascadeConfirmText.trim() === cascade.rootName.trim();

  // The shell sidebar delegates its per-folder actions here; refreshed after
  // every render so the handlers always read the current state and mutations.
  useEffect(() => {
    folderActionsRef.current = {
      rename: (folder) => {
        setRenameName(folder.name);
        setRenameTarget(folder);
      },
      move: (folder) => setMoveTarget(folder),
      remove: (folder) => {
        setFolderDeleteWithContent(false);
        setCascadeConfirmText("");
        setPendingConfirm({
          title: "Supprimer le dossier",
          message: (
            <>
              Supprimer <strong>{folder.name}</strong> ? Un dossier contenant encore
              des fichiers ou sous-dossiers sera refusé.
            </>
          ),
          confirmLabel: "Supprimer",
          danger: true,
          folderCascade: canCascadeDelete,
          onConfirm: (withContent) => {
            if (withContent) {
              openCascade({ id: folder.id, name: folder.name });
            } else {
              deleteFolderMutation.mutate({
                id: folder.id,
                name: folder.name,
              });
            }
          },
        });
      },
    };
  });

  return (
    <div className="stack vault-workspace">
      <div className="vault-top">
      <nav className="breadcrumb vault-breadcrumb" aria-label="Fil d'Ariane">
        <button
          type="button"
          className={
            currentFolderId === null
              ? "crumb vault-crumb vault-crumb-current"
              : "crumb vault-crumb"
          }
          aria-current={currentFolderId === null ? "page" : undefined}
          onClick={() => setCurrentFolderId(null)}
        >
          <span className="vault-home-icon" aria-hidden="true">
            <svg
              viewBox="0 0 24 24"
              width="15"
              height="15"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.7"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M3 10.5 12 3l9 7.5" />
              <path d="M5.5 9.5V20h13V9.5" />
            </svg>
          </span>
          <span>Racine</span>
        </button>
        {breadcrumb.map((folder, index) => (
          <span key={folder.id} className="crumb-item">
            <span className="crumb-sep" aria-hidden="true">
              &gt;
            </span>
            <button
              type="button"
              className={
                index === breadcrumb.length - 1
                  ? "crumb vault-crumb vault-crumb-current"
                  : "crumb vault-crumb"
              }
              aria-current={index === breadcrumb.length - 1 ? "page" : undefined}
              onClick={() => setCurrentFolderId(folder.id)}
            >
              {folder.name}
            </button>
          </span>
        ))}
      </nav>

      <div className="page-head vault-page-head">
        <div className="vault-page-title">
          <h1>Fichiers</h1>
          <p className="vault-subtitle">
            Gérez, importez et organisez tous vos fichiers en toute sécurité.
          </p>
        </div>
        <div className="head-actions vault-head-actions">
          <RowMenu
            label="Plus d'actions"
            items={[
              {
                label: "Actualiser la liste",
                disabled: objectsQuery.isFetching || treeQuery.isFetching,
                onSelect: refreshLibrary,
              },
              {
                label: "Créer un dossier",
                onSelect: () => {
                  setNewFolderName("");
                  setNewFolderOpen(true);
                },
              },
            ]}
          />
          <button
            type="button"
            className="btn vault-btn-refresh"
            disabled={objectsQuery.isFetching || treeQuery.isFetching}
            onClick={refreshLibrary}
          >
            <span className="vault-refresh-icon" aria-hidden="true">
              <svg
                viewBox="0 0 24 24"
                width="15"
                height="15"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M20 11a8 8 0 1 0-2.3 5.6" />
                <path d="M20 4v7h-7" />
              </svg>
            </span>
            {objectsQuery.isFetching ? "Actualisation…" : "Actualiser"}
          </button>
          <button
            type="button"
            className="btn vault-btn-primary"
            onClick={() => {
              setNewFolderName("");
              setNewFolderOpen(true);
            }}
          >
            + Nouveau dossier
          </button>
        </div>
      </div>

      {actionError ? (
        <p role="alert" className="alert alert-error">
          {actionError}
          <button
            type="button"
            className="btn btn-ghost btn-small"
            onClick={() => setActionError(null)}
          >
            Fermer
          </button>
        </p>
      ) : null}

      {notice ? (
        <p role="status" className="alert alert-ok">
          {notice}
          <button
            type="button"
            className="btn btn-ghost btn-small"
            onClick={() => setNotice(null)}
          >
            Fermer
          </button>
        </p>
      ) : null}

      {folderDeleteError ? (
        <p role="alert" className="alert alert-error">
          Le dossier «&nbsp;{folderDeleteError.name}&nbsp;» n'est pas vide : supprimez
          ou déplacez d'abord ses fichiers et sous-dossiers, puis réessayez.
          {canCascadeDelete ? (
            <button
              type="button"
              className="btn btn-ghost btn-small"
              onClick={() => openCascade(folderDeleteError)}
            >
              Supprimer avec son contenu…
            </button>
          ) : null}
          <button
            type="button"
            className="btn btn-ghost btn-small"
            disabled={deleteFolderMutation.isPending}
            onClick={() => deleteFolderMutation.mutate(folderDeleteError)}
          >
            {deleteFolderMutation.isPending ? "Suppression…" : "Réessayer"}
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-small"
            onClick={() => setFolderDeleteError(null)}
          >
            Fermer
          </button>
        </p>
      ) : null}
      </div>

      <section
        className={`card intake vault-dropzone${
          dragDepth > 0 ? " drop-active" : ""
        }${uploadError ? " drop-refused" : ""}`}
        aria-labelledby="upload-title"
        onDragEnter={(e) => {
          e.preventDefault();
          setDragDepth((depth) => depth + 1);
        }}
        onDragLeave={() => {
          setDragDepth((depth) => Math.max(0, depth - 1));
        }}
        onDragOver={(e) => {
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
        }}
        onDrop={(e) => {
          void handleDrop(e);
        }}
      >
        <div className="vault-dropzone-main">
          <span className="vault-cloud-icon" aria-hidden="true">
            <svg
              viewBox="0 0 24 24"
              width="30"
              height="30"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M7 18h10a4 4 0 0 0 .4-8 6 6 0 0 0-11.5 1.5A3.5 3.5 0 0 0 7 18Z" />
              <path d="M12 11v6" />
              <path d="m9.5 13.5 2.5-2.5 2.5 2.5" />
            </svg>
          </span>
          <div className="vault-dropzone-text">
            <p id="upload-title" className="vault-dropzone-title">
              Importer des fichiers
              {currentName ? ` dans « ${currentName} »` : ""}
            </p>
            <p className="vault-dropzone-lead">
              Glissez-déposez vos fichiers ici ou cliquez pour sélectionner.
            </p>
            <p className="muted">
              Chaque fichier est vérifié par empreinte SHA-256 avant d'être
              déclaré disponible (taille et empreinte contrôlées par le backend
              au moment du commit).
            </p>
          </div>
        </div>
        {dragDepth > 0 ? (
          <p role="status" className="drop-hint">
            Relâchez pour importer — dossiers acceptés.
          </p>
        ) : null}
        {/* Informative only: these categories describe what the intake accepts;
            they are not controls, so they stay non-interactive text. */}
        <ul className="intake-cats" aria-label="Catégories de fichiers acceptées">
          {IMPORT_CATEGORIES.map((category) => (
            <li key={category} className="intake-cat">
              {category}
            </li>
          ))}
        </ul>
        <form onSubmit={handleUpload} className="form form-inline">
          <div className="field file-field">
            <span>Fichiers</span>
            <label className="file-picker">
              <input
                type="file"
                name="files"
                multiple
                data-testid="files-input"
                className="visually-hidden"
                disabled={transferActive}
                onChange={(e) => {
                  handleFilesChange(e.target.files);
                  e.target.value = "";
                }}
              />
              <span className="btn vault-btn-blue">Choisir des fichiers…</span>
              <span className="file-picker-name">
                {pendingFiles.length === 0
                  ? "Aucun fichier sélectionné"
                  : pendingFiles.length === 1
                    ? (pendingFiles[0]?.name ?? "")
                    : `${pendingFiles.length} fichiers sélectionnés`}
              </span>
            </label>
          </div>
          <input
            ref={dirInputRef}
            type="file"
            multiple
            data-testid="folder-input"
            className="visually-hidden"
            disabled={transferActive}
            onChange={(e) => {
              void handleFolderUpload(e.target.files);
              e.target.value = "";
            }}
            {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
          />
          <button
            type="button"
            className="btn vault-btn-folder"
            disabled={transferActive}
            onClick={() => dirInputRef.current?.click()}
          >
            <span className="vault-btn-icon" aria-hidden="true">
              <svg
                viewBox="0 0 24 24"
                width="15"
                height="15"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4l2 2.5h7A1.5 1.5 0 0 1 19 9v8.5A1.5 1.5 0 0 1 17.5 19h-13A1.5 1.5 0 0 1 3 17.5Z" />
              </svg>
            </span>
            Importer un dossier
            <span className="vault-btn-caret" aria-hidden="true">
              ▾
            </span>
          </button>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={transferActive || pendingFiles.length === 0}
          >
            {transferActive ? "Transfert…" : "Importer et valider"}
          </button>
          {transferActive ? (
            <button type="button" className="btn" onClick={handleCancelUpload}>
              Annuler
            </button>
          ) : null}
          {!transferActive && (queue.failed > 0 || queue.cancelled > 0) ? (
            <button type="button" className="btn" onClick={handleRetryFailed}>
              Réessayer les échecs
            </button>
          ) : null}
        </form>

        {pendingFiles.length > 0 ? (
          <>
            {transferActive ? (
              <p className="pending-hint" role="status">
                En attente du prochain import — le transfert en cours se termine
                d'abord.
              </p>
            ) : null}
            <ul className="pending">
              {pendingFiles.map((pending, index) => (
                <li key={`${pending.name}-${index}`} className="pending-item">
                  <span className="transfer-name">{pending.name}</span>
                  <span className="muted">{formatBytes(pending.size)}</span>
                  <button
                    type="button"
                    className="btn btn-ghost btn-small"
                    aria-label={`Retirer ${pending.name}`}
                    onClick={() => removePendingFile(index)}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          </>
        ) : null}

        {queue.items.length > 0 ? (
          <div className="transfer" data-testid="transfer-status" aria-live="polite">
            <div className="queue-head">
              <span className="muted">
                {queue.done}/{queue.items.length} transféré
                {queue.items.length > 1 ? "s" : ""}
                {queue.failed > 0 ? ` · ${queue.failed} en échec` : ""}
                {queue.cancelled > 0
                  ? ` · ${queue.cancelled} annulé${queue.cancelled > 1 ? "s" : ""}`
                  : ""}
              </span>
              {transferActive ? (
                <span className="muted">
                  jusqu'à {queue.concurrency} en parallèle
                </span>
              ) : null}
            </div>
            {transferActive &&
            queue.maxConcurrency > 1 &&
            queue.concurrency < queue.maxConcurrency ? (
              <p className="muted" role="status">
                Connexion difficile : envois limités à {queue.concurrency} à la
                fois — retour à {queue.maxConcurrency} dès que ça passe.
              </p>
            ) : null}
            <ul className="queue">
              {queue.items.map((item) => (
                <li key={item.id} className="queue-item">
                  <div className="queue-line">
                    <span className="transfer-name">{item.fileName}</span>
                    <span className="muted">{formatBytes(item.sizeBytes)}</span>
                    <span className={queuePhaseClass(item.state.phase)}>
                      {PHASE_LABEL[item.state.phase]}
                    </span>
                    {item.state.phase === "available" ? (
                      <span className="chip chip-muted">SHA-256 vérifié</span>
                    ) : null}
                    {ACTIVE_PHASES.includes(item.state.phase) ? (
                      <button
                        type="button"
                        className="btn btn-ghost btn-small"
                        aria-label={`Interrompre ${item.fileName}`}
                        onClick={() => queueRef.current?.cancelItem(item.id)}
                      >
                        ×
                      </button>
                    ) : null}
                    {item.state.phase === "failed" ||
                    item.state.phase === "cancelled" ? (
                      <button
                        type="button"
                        className="btn btn-ghost btn-small"
                        aria-label={`Relancer ${item.fileName}`}
                        onClick={() => {
                          setUploadError(null);
                          queueRef.current?.retryItem(item.id);
                        }}
                      >
                        Relancer
                      </button>
                    ) : null}
                  </div>
                  {item.state.phase === "uploading" ? (
                    <div
                      className="progress"
                      role="progressbar"
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={Math.round(item.state.progress * 100)}
                      aria-label={`Progression de ${item.fileName}`}
                    >
                      <span
                        className="progress-bar"
                        style={{ width: `${Math.round(item.state.progress * 100)}%` }}
                      />
                    </div>
                  ) : null}
                  {item.state.phase === "uploading" ? (
                    <p className="muted">
                      {formatBytes(item.state.transferredBytes)} /{" "}
                      {formatBytes(item.state.sizeBytes)} (
                      {Math.round(item.state.progress * 100)} %)
                    </p>
                  ) : null}
                  {item.state.attempts > 0 &&
                  ACTIVE_PHASES.includes(item.state.phase) ? (
                    <p className="muted">
                      Nouvelle tentative ({item.state.attempts})…
                    </p>
                  ) : null}
                  {item.state.error && item.state.phase === "failed" ? (
                    <p className="muted">{item.state.error}</p>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {uploadError ? (
          <p role="alert" className="alert alert-error">
            {uploadError}
          </p>
        ) : null}
        {queue.finished &&
        queue.items.length > 0 &&
        queue.done === queue.items.length ? (
          <p role="status" className="alert alert-ok">
            Import validé et vérifié par le backend.
          </p>
        ) : null}
        {queue.finished &&
        queue.items.length > 0 &&
        queue.cancelled === queue.items.length ? (
          <p role="status" className="alert">
            Import annulé.
          </p>
        ) : null}
      </section>

      <section className="section vault-objects-card" aria-labelledby="objects-title">
        <div className="section-head vault-objects-head">
          <h2 id="objects-title" className="vault-card-title">
            <span className="vault-card-title-icon" aria-hidden="true">
              <svg
                viewBox="0 0 24 24"
                width="16"
                height="16"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4l2 2.5h7A1.5 1.5 0 0 1 19 9v8.5A1.5 1.5 0 0 1 17.5 19h-13A1.5 1.5 0 0 1 3 17.5Z" />
              </svg>
            </span>
            Objets{currentName ? ` — ${currentName}` : " — racine"}
          </h2>
          <div className="vault-objects-tools">
            <span className="muted vault-counter" aria-live="polite">
              {items.length} objet{items.length > 1 ? "s" : ""} affiché
              {items.length > 1 ? "s" : ""}
              {objectsQuery.hasNextPage ? " (d'autres restent à charger)" : ""}
            </span>
            <div
              className="vault-view-toggle"
              role="group"
              aria-label="Mode d'affichage"
            >
              <button
                type="button"
                className={
                  viewMode === "list"
                    ? "vault-view-btn vault-view-btn-active"
                    : "vault-view-btn"
                }
                aria-pressed={viewMode === "list"}
                aria-label="Affichage en liste"
                onClick={() => setViewMode("list")}
              >
                <svg
                  viewBox="0 0 24 24"
                  width="15"
                  height="15"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  aria-hidden="true"
                >
                  <path d="M4 7h16M4 12h16M4 17h16" />
                </svg>
              </button>
              <button
                type="button"
                className={
                  viewMode === "grid"
                    ? "vault-view-btn vault-view-btn-active"
                    : "vault-view-btn"
                }
                aria-pressed={viewMode === "grid"}
                aria-label="Affichage en grille"
                onClick={() => setViewMode("grid")}
              >
                <svg
                  viewBox="0 0 24 24"
                  width="15"
                  height="15"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  aria-hidden="true"
                >
                  <path d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z" />
                </svg>
              </button>
            </div>
          </div>
        </div>

        <form
          className="filters vault-filters"
          role="search"
          onSubmit={(e) => e.preventDefault()}
          aria-label="Recherche et filtres"
        >
          <label className="field vault-search-field">
            <span className="visually-hidden">Rechercher</span>
            <span className="vault-search-icon" aria-hidden="true">
              <svg
                viewBox="0 0 24 24"
                width="15"
                height="15"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
              >
                <circle cx="11" cy="11" r="6.5" />
                <path d="m16 16 4 4" />
              </svg>
            </span>
            <input
              type="search"
              name="filter"
              placeholder="Nom du fichier…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          <label className="field">
            <span>Type</span>
            <select
              name="type"
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
            >
              {TYPE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>État</span>
            <select
              name="status"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
            >
              {STATUS_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Trier par</span>
            <select
              name="sort"
              value={sortField}
              onChange={(e) =>
                setSortField(e.target.value as NonNullable<ObjectQuery["sort"]>)
              }
            >
              {SORT_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Ordre</span>
            <select
              name="order"
              value={sortOrder}
              onChange={(e) =>
                setSortOrder(e.target.value as NonNullable<ObjectQuery["order"]>)
              }
            >
              <option value="desc">Décroissant</option>
              <option value="asc">Croissant</option>
            </select>
          </label>
          <button
            type="button"
            className="btn filters-toggle vault-filters-toggle"
            aria-expanded={advancedOpen}
            aria-controls="advanced-filters"
            onClick={() => setAdvancedOpen((value) => !value)}
          >
            Filtres
            {advancedCount > 0 ? (
              <span className="filters-count">{advancedCount}</span>
            ) : null}
          </button>
          <button
            type="button"
            className="btn btn-small"
            onClick={resetFilters}
            disabled={!hasActiveFilters}
          >
            Réinitialiser
          </button>
        </form>

        {advancedOpen ? (
          <div className="filters-advanced" id="advanced-filters">
            <label className="field">
              <span>Type MIME</span>
              <input
                type="text"
                name="contentType"
                list="content-type-options"
                placeholder="application/pdf…"
                value={typeFilter}
                onChange={(e) => setTypeFilter(e.target.value)}
              />
              <datalist id="content-type-options">
                {CONTENT_TYPE_SUGGESTIONS.map((type) => (
                  <option key={type} value={type} />
                ))}
              </datalist>
            </label>
            <label className="field">
              <span>Taille min (o)</span>
              <input
                type="number"
                name="minSize"
                min={0}
                value={minSize}
                onChange={(e) => setMinSize(e.target.value)}
              />
            </label>
            <label className="field">
              <span>Taille max (o)</span>
              <input
                type="number"
                name="maxSize"
                min={0}
                value={maxSize}
                onChange={(e) => setMaxSize(e.target.value)}
              />
            </label>
            <label className="field">
              <span>Créé après le</span>
              <input
                type="date"
                name="from"
                value={fromDate}
                onChange={(e) => setFromDate(e.target.value)}
              />
            </label>
            <label className="field">
              <span>Créé avant le</span>
              <input
                type="date"
                name="to"
                value={toDate}
                onChange={(e) => setToDate(e.target.value)}
              />
            </label>
          </div>
        ) : null}

        {objectsQuery.isError ? (
          <p role="alert" className="alert alert-error">
            Catalogue indisponible. Aucune donnée en cache n'est présentée comme
            actuelle.
            <button
              type="button"
              className="btn btn-ghost btn-small"
              onClick={() => void objectsQuery.refetch()}
            >
              Réessayer
            </button>
          </p>
        ) : objectsQuery.isPending ? (
          <div className="ledger skeleton" role="status" aria-label="Chargement du catalogue">
            <span className="visually-hidden">Chargement du catalogue…</span>
            {Array.from({ length: 6 }).map((_, index) => (
              <div className="skeleton-row" key={index} aria-hidden="true">
                <div className="skeleton-bar" />
                <div className="skeleton-bar" />
                <div className="skeleton-bar" />
                <div className="skeleton-bar" />
                <div className="skeleton-bar" />
                <div className="skeleton-bar" />
              </div>
            ))}
          </div>
        ) : items.length === 0 ? (
          <p className="empty">
            {hasActiveFilters
              ? "Aucun fichier ne correspond à ces critères."
              : "Aucun fichier dans ce dossier."}
          </p>
        ) : (
          <>
            {selectedIds.size > 0 ? (
              <p role="status" className="alert">
                {selectedIds.size} objet(s) sélectionné(s).{" "}
                <button
                  type="button"
                  className="btn btn-ghost btn-small"
                  disabled={deleteManyMutation.isPending}
                  onClick={() => setSelectedIds(new Set())}
                >
                  Tout désélectionner
                </button>{" "}
                <ConfirmAction
                  label={
                    deleteManyMutation.isPending
                      ? "Suppression…"
                      : "Supprimer la sélection"
                  }
                  title="Supprimer la sélection"
                  message={
                    <>
                      Marquer <strong>{selectedIds.size} objet(s)</strong>{" "}
                      comme supprimé(s) ? Chaque fichier est supprimé
                      individuellement et le résultat est rapporté par
                      fichier. Les octets restent jusqu'à une purge.
                    </>
                  }
                  confirmLabel="Supprimer"
                  danger
                  pending={deleteManyMutation.isPending}
                  onConfirm={() =>
                    deleteManyMutation.mutate([...selectedIds])
                  }
                />
              </p>
            ) : null}
            <div
              className={
                viewMode === "grid" ? "ledger vault-view-grid" : "ledger"
              }
              role="list"
            >
            <div className="ledger-head">
              <span>
                <input
                  type="checkbox"
                  aria-label="Tout sélectionner"
                  checked={
                    selectableItems.length > 0 &&
                    selectableItems.every((item) => selectedIds.has(item.id))
                  }
                  disabled={
                    selectableItems.length === 0 || deleteManyMutation.isPending
                  }
                  onChange={(e) => {
                    if (e.target.checked) {
                      setSelectedIds(
                        new Set(selectableItems.map((item) => item.id)),
                      );
                    } else {
                      setSelectedIds(new Set());
                    }
                  }}
                />{" "}
                <span aria-hidden="true">Nom</span>
              </span>
              <span aria-hidden="true">Type</span>
              <span aria-hidden="true">Taille</span>
              <span aria-hidden="true">État</span>
              <span aria-hidden="true">Modifié</span>
              <span aria-hidden="true" />
            </div>
            {items.map((item) => {
              const rowBusy =
                deletePendingId === item.id ||
                purgePendingId === item.id ||
                abortPendingId === item.id ||
                downloadingId === item.id;
              return (
                <div className="ledger-row" role="listitem" key={item.id}>
                  <div className="ledger-name">
                    <input
                      type="checkbox"
                      aria-label={`Sélectionner ${item.display_name}`}
                      checked={selectedIds.has(item.id)}
                      disabled={
                        !selectableIds.has(item.id) ||
                        deleteManyMutation.isPending
                      }
                      onChange={(e) => {
                        setSelectedIds((prev) => {
                          const next = new Set(prev);
                          if (e.target.checked) {
                            next.add(item.id);
                          } else {
                            next.delete(item.id);
                          }
                          return next;
                        });
                      }}
                    />{" "}
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => setDetails(item)}
                      aria-label={`Détails de ${item.display_name}`}
                    >
                      {item.display_name}
                    </button>
                  </div>
                  <div className="ledger-meta">
                    <span
                      className="ledger-type vault-badge-type"
                      title={item.content_type}
                    >
                      {typeLabel(item.content_type)}
                    </span>
                    <span className="ledger-size num">
                      {formatBytes(
                        item.status === "pending" && item.expected_size_bytes != null
                          ? item.expected_size_bytes
                          : item.size_bytes,
                      )}
                    </span>
                    <span className="ledger-status">
                      <StatusLabel status={item.status} />
                    </span>
                    <span className="ledger-date num">
                      {formatDateTime(item.updated_at)}
                    </span>
                  </div>
                  <div className="ledger-menu">
                    <RowMenu
                      label={`Actions pour ${item.display_name}`}
                      items={[
                        { label: "Détails", onSelect: () => setDetails(item) },
                        {
                          label: "Télécharger",
                          disabled:
                            item.status !== "available" || downloadingId !== null,
                          onSelect: () => void handleDownload(item),
                        },
                        {
                          label: "Copier la référence",
                          onSelect: () => void handleCopyReference(item),
                        },
                        {
                          label: "Déplacer",
                          disabled: rowBusy,
                          onSelect: () => setMoveObjectTarget(item),
                        },
                        ...(item.status === "pending"
                          ? [
                              {
                                label: "Abandonner l'envoi",
                                danger: true,
                                disabled: rowBusy,
                                onSelect: () =>
                                  setPendingConfirm({
                                    title: "Abandonner l'envoi",
                                    message: (
                                      <>
                                        Abandonner l'envoi de{" "}
                                        <strong>{item.display_name}</strong> ?
                                        Les octets partiels seront supprimés.
                                        Pour réessayer, importez à nouveau le
                                        fichier.
                                      </>
                                    ),
                                    confirmLabel: "Abandonner",
                                    danger: true,
                                    onConfirm: () =>
                                      abortUploadMutation.mutate(item.id),
                                  }),
                              },
                            ]
                          : []),
                        {
                          label: "Supprimer",
                          danger: true,
                          // Same rule as the bulk selection (available|pending):
                          // the server 409s every other status, so an enabled
                          // entry here would only manufacture a failure.
                          disabled: objectBusy || !isDeletable(item),
                          onSelect: () =>
                            setPendingConfirm({
                              title: "Supprimer l'objet",
                              message: (
                                <>
                                  Marquer <strong>{item.display_name}</strong> comme
                                  supprimé ? Les octets restent jusqu'à une purge.
                                </>
                              ),
                              confirmLabel: "Supprimer",
                              danger: true,
                              onConfirm: () => deleteObjectMutation.mutate(item.id),
                            }),
                        },
                        ...(canPurge
                          ? [
                              {
                                label: "Purger définitivement",
                                danger: true,
                                disabled: objectBusy,
                                onSelect: () =>
                                  setPendingConfirm({
                                    title: "Purger définitivement",
                                    message: (
                                      <>
                                        Supprimer définitivement{" "}
                                        <strong>{item.display_name}</strong> et ses
                                        octets ? Action irréversible.
                                      </>
                                    ),
                                    confirmLabel: "Purger",
                                    danger: true,
                                    onConfirm: () => purgeMutation.mutate(item.id),
                                  }),
                              },
                            ]
                          : []),
                      ]}
                    />
                  </div>
                </div>
                  );
            })}
          </div>
          </>
        )}
        {objectsQuery.hasNextPage ? (
          <div className="load-more">
            <button
              type="button"
              className="btn"
              disabled={objectsQuery.isFetchingNextPage}
              onClick={() => void objectsQuery.fetchNextPage()}
            >
              {objectsQuery.isFetchingNextPage ? "Chargement…" : "Charger plus"}
            </button>
          </div>
        ) : null}
      </section>

      {/* Object details side panel (only fields the API actually exposes). */}
      {details ? (
        <>
          <div
            className="drawer-scrim"
            aria-hidden="true"
            onClick={() => setDetails(null)}
          />
          <aside
            className="drawer"
            role="dialog"
            aria-modal="false"
            aria-labelledby="details-title"
          onKeyDown={(e) => {
            if (e.key === "Escape") setDetails(null);
          }}
        >
          <div className="drawer-head">
            <h2 id="details-title">{details.display_name}</h2>
            <button
              type="button"
              className="btn btn-ghost btn-small"
              onClick={() => setDetails(null)}
              aria-label="Fermer les détails"
            >
              Fermer
            </button>
          </div>
          <dl className="detail-list">
            <dt>Nom</dt>
            <dd>{details.display_name}</dd>
            <dt>Identifiant</dt>
            <dd className="mono">{details.id}</dd>
            <dt>Type MIME</dt>
            <dd>{details.content_type}</dd>
            <dt>Taille</dt>
            <dd>
              {formatBytes(details.size_bytes)}
              <span className="muted"> ({details.size_bytes} octets)</span>
            </dd>
            <dt>État</dt>
            <dd>
              <StatusLabel status={details.status} />
            </dd>
            <dt>Dossier</dt>
            <dd>{folderPath(details.folder_id)}</dd>
            <dt>Créé le</dt>
            <dd>{formatDateTime(details.created_at)}</dd>
            <dt>Modifié le</dt>
            <dd>{formatDateTime(details.updated_at)}</dd>
            <dt>Empreinte SHA-256</dt>
            <dd className="detail-hash">
              {details.checksum_sha256 ? (
                <>
                  <code className="mono">{details.checksum_sha256}</code>
                  <button
                    type="button"
                    className="btn btn-small"
                    onClick={() => {
                      const value = details.checksum_sha256;
                      if (!navigator.clipboard) {
                        setNotice("Copie impossible : contexte non sécurisé.");
                        return;
                      }
                      void navigator.clipboard.writeText(value).then(
                        () => setNotice("Empreinte SHA-256 copiée."),
                        () => setNotice("Copie refusée par le navigateur."),
                      );
                    }}
                  >
                    Copier
                  </button>
                </>
              ) : (
                "non disponible"
              )}
            </dd>
          </dl>
          <div className="drawer-actions">
            <button
              type="button"
              className="btn btn-primary"
              disabled={details.status !== "available" || downloadingId !== null}
              onClick={() => void handleDownload(details)}
            >
              {downloadingId === details.id ? "…" : "Télécharger"}
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => void handleCopyReference(details)}
            >
              Copier la référence
            </button>
            <button
              type="button"
              className="btn"
              disabled={objectBusy}
              onClick={() => setMoveObjectTarget(details)}
            >
              Déplacer
            </button>
            <ConfirmAction
              label="Supprimer"
              title="Supprimer l'objet"
              message={
                <>
                  Marquer <strong>{details.display_name}</strong> comme supprimé ?
                  Les octets restent jusqu'à une purge.
                </>
              }
              confirmLabel="Supprimer"
              danger
              className="btn drawer-actions-danger"
              disabled={objectBusy}
              pending={deletePendingId === details.id}
              onConfirm={() => deleteObjectMutation.mutate(details.id)}
            />
            {canPurge ? (
              <ConfirmAction
                label="Purger définitivement"
                title="Purger définitivement"
                message={
                  <>
                    Supprimer définitivement <strong>{details.display_name}</strong>{" "}
                    et ses octets ? Action irréversible.
                  </>
                }
                confirmLabel="Purger"
                danger
                className="btn btn-danger-text"
                disabled={objectBusy}
                pending={purgePendingId === details.id}
                onConfirm={() => purgeMutation.mutate(details.id)}
              />
            ) : null}
          </div>
          </aside>
        </>
      ) : null}

      {/* New folder */}
      <Dialog
        open={newFolderOpen}
        title="Nouveau dossier"
        onClose={() => setNewFolderOpen(false)}
      >
        <form
          className="form"
          onSubmit={(e) => {
            e.preventDefault();
            createFolderMutation.mutate(newFolderName);
          }}
        >
          <label className="field">
            <span>Nom</span>
            <input
              type="text"
              value={newFolderName}
              onChange={(e) => setNewFolderName(e.target.value)}
              required
              autoFocus
            />
          </label>
          <div className="dialog-actions">
            <button
              type="button"
              className="btn"
              onClick={() => setNewFolderOpen(false)}
            >
              Annuler
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={createFolderMutation.isPending}
            >
              Créer
            </button>
          </div>
        </form>
      </Dialog>

      {/* Rename folder */}
      <Dialog
        open={renameTarget !== null}
        title="Renommer le dossier"
        onClose={() => setRenameTarget(null)}
      >
        <form
          className="form"
          onSubmit={(e) => {
            e.preventDefault();
            if (renameTarget) {
              renameMutation.mutate({ id: renameTarget.id, name: renameName });
            }
          }}
        >
          <label className="field">
            <span>Nom</span>
            <input
              type="text"
              value={renameName}
              onChange={(e) => setRenameName(e.target.value)}
              required
              autoFocus
            />
          </label>
          <div className="dialog-actions">
            <button
              type="button"
              className="btn"
              onClick={() => setRenameTarget(null)}
            >
              Annuler
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={renameMutation.isPending}
            >
              Renommer
            </button>
          </div>
        </form>
      </Dialog>

      {/* Move folder */}
      <Dialog
        open={moveTarget !== null}
        title="Déplacer le dossier"
        onClose={() => setMoveTarget(null)}
      >
        <p className="muted">
          Choisissez le nouveau dossier parent de « {moveTarget?.name} ».
        </p>
        <ul className="move-list">
          <li>
            <button
              type="button"
              className="btn btn-small"
              disabled={moveMutation.isPending}
              onClick={() =>
                moveTarget && moveMutation.mutate({ id: moveTarget.id, parentId: null })
              }
            >
              Racine
            </button>
          </li>
          {tree
            .filter((f) => !moveBlocked.has(f.id))
            .map((f) => (
              <li key={f.id}>
                <button
                  type="button"
                  className="btn btn-small"
                  disabled={moveMutation.isPending}
                  onClick={() =>
                    moveTarget &&
                    moveMutation.mutate({ id: moveTarget.id, parentId: f.id })
                  }
                >
                  {f.name}
                </button>
              </li>
            ))}
        </ul>
      </Dialog>

      {/* Move an object into a folder */}
      <Dialog
        open={moveObjectTarget !== null}
        title="Déplacer l'objet"
        onClose={() => setMoveObjectTarget(null)}
      >
        <p className="muted">
          Choisissez le dossier de destination de «{" "}
          {moveObjectTarget?.display_name} ».
        </p>
        {tree.length === 0 ? (
          <p className="empty">
            Aucun dossier pour l'instant. Créez-en un depuis la barre d'actions.
          </p>
        ) : (
          <ul className="move-list">
            <li>
              <button
                type="button"
                className="btn btn-small"
                disabled={moveObjectMutation.isPending}
                onClick={() =>
                  moveObjectTarget &&
                  moveObjectMutation.mutate({
                    id: moveObjectTarget.id,
                    folderId: null,
                  })
                }
              >
                Racine (sans dossier)
              </button>
            </li>
            {tree.map((folder) => (
              <li key={folder.id}>
                <button
                  type="button"
                  className="btn btn-small"
                  disabled={
                    moveObjectMutation.isPending ||
                    moveObjectTarget?.folder_id === folder.id
                  }
                  onClick={() =>
                    moveObjectTarget &&
                    moveObjectMutation.mutate({
                      id: moveObjectTarget.id,
                      folderId: folder.id,
                    })
                  }
                >
                  {folder.name}
                </button>
              </li>
            ))}
          </ul>
        )}
      </Dialog>

      {/* Destructive confirmation opened from a row menu (owned by the page so
          the menu panel can unmount safely). */}
      <Dialog
        open={pendingConfirm !== null}
        title={pendingConfirm?.title ?? ""}
        onClose={() => setPendingConfirm(null)}
      >
        <div className="dialog-text">{pendingConfirm?.message}</div>
        {pendingConfirm?.folderCascade ? (
          <label className="cascade-option">
            <input
              type="checkbox"
              checked={folderDeleteWithContent}
              onChange={(event) => setFolderDeleteWithContent(event.target.checked)}
            />
            <span>Supprimer aussi tout son contenu (fichiers et sous-dossiers)</span>
          </label>
        ) : null}
        <div className="dialog-actions">
          <button
            type="button"
            className="btn"
            onClick={() => setPendingConfirm(null)}
          >
            Annuler
          </button>
          <button
            type="button"
            className={
              pendingConfirm?.danger ? "btn btn-danger" : "btn btn-primary"
            }
            onClick={() => {
              const action = pendingConfirm?.onConfirm;
              const withContent = folderDeleteWithContent;
              setPendingConfirm(null);
              action?.(withContent);
            }}
          >
            {pendingConfirm?.confirmLabel ?? "Confirmer"}
          </button>
        </div>
      </Dialog>
      <ToastStack toasts={toasts} onDismiss={dismissToast} />

      {/* Recursive (cascade) folder deletion: real listing, typed confirmation,
          live progress, explicit per-item failure log. A native <dialog> so
          Escape/focus trapping are built in; Escape is routed to an explicit
          cancel while deleting instead of closing mid-run. */}
      {cascade ? (
        <dialog
          ref={cascadeDialogRef}
          className="dialog cascade-dialog"
          aria-labelledby="cascade-title"
          onCancel={(event) => {
            if (cascade.phase === "deleting") {
              event.preventDefault();
              cancelCascade();
            } else {
              closeCascade();
            }
          }}
        >
          <div className="dialog-head">
            <h2 id="cascade-title">Supprimer le dossier et son contenu</h2>
            {cascade.phase === "scanning" || cascade.phase === "confirm" ? (
              <button
                type="button"
                className="btn btn-ghost btn-small"
                onClick={closeCascade}
                aria-label="Fermer"
              >
                Fermer
              </button>
            ) : null}
          </div>
          <div className="dialog-body">
            {cascade.phase === "scanning" ? (
              <p className="muted" role="status">
                Analyse du contenu réel du dossier…
              </p>
            ) : null}

            {cascade.phase === "confirm" ? (
              cascade.scanError ? (
                <p role="alert" className="alert alert-error">
                  {cascade.scanError}
                  <button
                    type="button"
                    className="btn btn-ghost btn-small"
                    onClick={() =>
                      void runCascadeScan(cascade.rootId, cascade.rootName)
                    }
                  >
                    Réessayer
                  </button>
                </p>
              ) : cascade.plan ? (
                <>
                  <p className="dialog-text">
                    Le dossier <strong>{cascade.rootName}</strong> contient{" "}
                    <strong>{cascade.plan.fileCount} fichier(s)</strong> et{" "}
                    <strong>{cascade.plan.folderCount} sous-dossier(s)</strong>.
                  </p>
                  <p role="alert" className="alert alert-error">
                    Suppression du contenu : chaque fichier est marqué supprimé
                    (les octets restent jusqu'à une purge), puis chaque dossier
                    vidé est supprimé, des feuilles vers la racine.
                  </p>
                  <label className="field cascade-confirm-field">
                    <span>Recopiez «&nbsp;{cascade.rootName}&nbsp;» pour confirmer</span>
                    <input
                      type="text"
                      value={cascadeConfirmText}
                      onChange={(event) =>
                        setCascadeConfirmText(event.target.value)
                      }
                      autoFocus
                      aria-label="Confirmation par recopie du nom"
                    />
                  </label>
                  <div className="dialog-actions">
                    <button type="button" className="btn" onClick={closeCascade}>
                      Annuler
                    </button>
                    <button
                      type="button"
                      className="btn btn-danger"
                      disabled={!cascadeConfirmed}
                      onClick={startCascade}
                    >
                      Supprimer le contenu et le dossier
                    </button>
                  </div>
                </>
              ) : null
            ) : null}

            {cascade.phase === "deleting" || cascade.phase === "done" ? (
              <>
                <p
                  role="status"
                  className={
                    cascade.phase === "done" &&
                    !cascade.cancelled &&
                    cascade.failures.length === 0
                      ? "alert alert-ok"
                      : "dialog-text cascade-progress-text"
                  }
                >
                  {cascade.phase === "deleting"
                    ? `Suppression en cours — ${cascade.done}/${cascade.ops.length}`
                    : cascade.cancelled
                      ? `Suppression interrompue — ${cascade.done}/${cascade.ops.length}`
                      : cascade.failures.length === 0
                        ? `Contenu supprimé et dossier supprimé — ${cascade.done}/${cascade.ops.length}`
                        : `Suppression terminée avec échecs — ${cascade.done}/${cascade.ops.length}`}
                </p>
                <div
                  className="progress"
                  role="progressbar"
                  aria-valuemin={0}
                  aria-valuemax={cascade.ops.length}
                  aria-valuenow={cascade.done}
                  aria-label="Progression de la suppression récursive"
                >
                  <span
                    className="progress-bar"
                    style={{
                      width: `${
                        cascade.ops.length > 0
                          ? Math.round((cascade.done / cascade.ops.length) * 100)
                          : 0
                      }%`,
                    }}
                  />
                </div>
                {cascade.phase === "deleting" && cascade.current ? (
                  <p className="muted">Traitement : {cascade.current}</p>
                ) : null}

                {cascade.failures.length > 0 ? (
                  <div className="cascade-log">
                    <h3 className="cascade-log-title">
                      Journal des échecs ({cascade.failures.length})
                    </h3>
                    <ul className="cascade-log-list">
                      {cascade.failures.map((failure) => (
                        <li key={failure.key} className="cascade-log-item">
                          <span
                            className={`chip ${
                              failure.kind === "folder" ? "chip-warn" : "chip-error"
                            }`}
                          >
                            {failure.kind === "folder" ? "Dossier" : "Fichier"}
                          </span>
                          <span className="cascade-log-name">{failure.label}</span>
                          <span className="muted cascade-log-path">
                            {failure.path}
                          </span>
                          <span className="muted cascade-log-error">
                            {failure.error}
                          </span>
                          {cascade.phase === "done" ? (
                            <span className="cascade-log-actions">
                              <button
                                type="button"
                                className="btn btn-ghost btn-small"
                                onClick={() => retryCascadeItem(failure)}
                              >
                                Réessayer
                              </button>
                              <button
                                type="button"
                                className="btn btn-ghost btn-small"
                                onClick={() => abandonCascadeItem(failure)}
                              >
                                Abandonner
                              </button>
                            </span>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                <div className="dialog-actions">
                  {cascade.phase === "deleting" ? (
                    <button type="button" className="btn" onClick={cancelCascade}>
                      Annuler
                    </button>
                  ) : (
                    <>
                      {cascade.failures.length > 0 ? (
                        <button
                          type="button"
                          className="btn"
                          onClick={retryCascadeFailures}
                        >
                          Réessayer les échecs
                        </button>
                      ) : null}
                      <button
                        type="button"
                        className="btn btn-primary"
                        onClick={closeCascade}
                      >
                        Fermer
                      </button>
                    </>
                  )}
                </div>
              </>
            ) : null}
          </div>
        </dialog>
      ) : null}
    </div>
  );
}
