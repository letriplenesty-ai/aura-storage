import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, hasAdminAccess } from "../lib/api";
import type { FolderItem } from "../lib/api";
import { useAuth, useSession } from "../lib/auth";
import { FolderMark } from "./FolderMark";
import { RowMenu } from "./RowMenu";
import { VaultMark } from "./VaultMark";

/** Byte size -> a human label ("3.4 Mo"), matching the API/explorer convention.
 * The shell is now the single place that reports storage usage. */
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

/** French label for the signed-in principal (no fictional display name). */
function principalLabel(role: string | null | undefined, kind: string | undefined): string {
  if (kind !== undefined && kind !== "user") return "Identité de service";
  switch (role) {
    case "admin":
      return "Administrateur";
    case "operator":
      return "Opérateur";
    case "user":
      return "Utilisateur";
    default:
      return "Compte";
  }
}

/** Catalogue status -> French label for the real object breakdown. */
const STATUS_FR: Record<string, string> = {
  available: "Disponibles",
  pending: "En cours",
  deleted: "Supprimés",
  expired: "Expirés",
  failed: "Échecs",
  aborted: "Abandonnés",
};

const STATUS_ORDER = ["available", "pending", "deleted", "expired", "failed", "aborted"];

/** Folder flows the files page registers so the shell's sidebar can trigger
 * them: the page keeps ownership of their state, dialogs and toasts. */
export interface FolderActions {
  rename: (folder: FolderItem) => void;
  move: (folder: FolderItem) => void;
  remove: (folder: { id: string; name: string }) => void;
}

export interface FolderNavValue {
  /** Currently opened folder (null = root), shared with the files page. */
  currentFolderId: string | null;
  setCurrentFolderId: (id: string | null) => void;
  /** Non-null once the files page has registered its folder handlers. */
  folderActions: FolderActions | null;
  registerFolderActions: (actions: FolderActions | null) => void;
}

const FolderNavContext = createContext<FolderNavValue | null>(null);

/** Read the shell-owned folder selection and register the page-owned actions. */
export function useFolderNav(): FolderNavValue {
  const value = useContext(FolderNavContext);
  if (!value) {
    throw new Error("useFolderNav must be used inside the vault shell.");
  }
  return value;
}

const USAGE_DATE_FORMAT = new Intl.DateTimeFormat("fr-FR", {
  dateStyle: "medium",
  timeStyle: "short",
});

/** Epoch seconds -> localized label; invalid/zero values render an em dash. */
function formatDateTime(epoch: number | undefined): string {
  if (epoch === undefined || !Number.isFinite(epoch) || epoch <= 0) return "—";
  return USAGE_DATE_FORMAT.format(new Date(epoch * 1000));
}

/** Close a disclosure on outside click and on Escape. */
function useDismiss(
  open: boolean,
  setOpen: (value: boolean) => void,
) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    function handlePointer(event: MouseEvent) {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    }
    function handleKey(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", handlePointer);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handlePointer);
      document.removeEventListener("keydown", handleKey);
    };
  }, [open, setOpen]);
  return ref;
}

function FolderNavIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M3 7a3 3 0 0 1 3-3h3.1a2 2 0 0 1 1.6.8l1 1.3H18a3 3 0 0 1 3 3V17a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3z"
        stroke="currentColor"
        strokeWidth="1.6"
      />
    </svg>
  );
}

function AdminNavIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="3.2" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M12 3v2.2M12 18.8V21M3 12h2.2M18.8 12H21M5.6 5.6l1.6 1.6M16.8 16.8l1.6 1.6M18.4 5.6l-1.6 1.6M7.2 16.8l-1.6 1.6"
        stroke="currentColor"
        strokeWidth="1.6"
      />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="11" cy="11" r="6.5" stroke="currentColor" strokeWidth="1.7" />
      <path d="M16 16l4.5 4.5" stroke="currentColor" strokeWidth="1.7" />
    </svg>
  );
}

function BellIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6Z"
        stroke="currentColor"
        strokeWidth="1.6"
      />
      <path d="M10 19a2 2 0 0 0 4 0" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

function MenuIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" strokeWidth="1.7" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="1.7" />
    </svg>
  );
}

function ChevronIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M7 10l5 5 5-5" stroke="currentColor" strokeWidth="1.7" />
    </svg>
  );
}

export function Layout({ children }: { children: React.ReactNode }) {
  const { csrf, logout } = useAuth();
  const session = useSession();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const canAdmin = hasAdminAccess(session.data?.role);
  /** Only fire catalogue/usage reads once the server has confirmed a session. */
  const signedIn = session.data !== undefined;

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [notifOpen, setNotifOpen] = useState(false);
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");
  const [folderError, setFolderError] = useState<string | null>(null);
  const [creatingFolder, setCreatingFolder] = useState(false);

  // Folder selection + the page-registered actions, shared with the files page
  // so the sidebar navigates the same folder the object list is scoped to.
  const [currentFolderId, setCurrentFolderId] = useState<string | null>(null);
  const [folderActions, setFolderActions] = useState<FolderActions | null>(null);
  const registerFolderActions = useCallback(
    (actions: FolderActions | null) => setFolderActions(actions),
    [],
  );
  const folderNav = useMemo<FolderNavValue>(
    () => ({
      currentFolderId,
      setCurrentFolderId,
      folderActions,
      registerFolderActions,
    }),
    [currentFolderId, folderActions, registerFolderActions],
  );

  const searchRef = useRef<HTMLInputElement | null>(null);
  const hamburgerRef = useRef<HTMLButtonElement | null>(null);

  const profileMenuRef = useDismiss(profileOpen, setProfileOpen);
  const notifMenuRef = useDismiss(notifOpen, setNotifOpen);

  // Same query keys as the explorer: the shell and the page share one fetch.
  const treeQuery = useQuery({
    queryKey: ["folders", "all"],
    queryFn: () => api.listAllFolders(),
    enabled: signedIn,
  });
  // The sidebar lists every folder flat (root included), like the mock: the
  // current folder is highlighted wherever it sits in the tree.
  const allFolders = treeQuery.data?.items ?? [];

  // Real storage usage, admin:read only — never fabricated for other roles.
  const usageQuery = useQuery({
    queryKey: ["admin", "usage"],
    queryFn: () => api.adminUsage(),
    enabled: canAdmin,
    retry: false,
  });
  const usage = usageQuery.data;
  const byStatus = usage?.totals.objects_by_status ?? {};
  const statusRows = [
    ...STATUS_ORDER.filter((key) => (byStatus[key] ?? 0) > 0),
    ...Object.keys(byStatus).filter((key) => !STATUS_ORDER.includes(key)),
  ];

  // Ctrl+K / Cmd+K focuses the global search field.
  useEffect(() => {
    function handleKey(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && (event.key === "k" || event.key === "K")) {
        event.preventDefault();
        const input = searchRef.current;
        if (input) {
          input.focus();
          input.select();
        }
      }
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, []);

  // Escape closes the mobile drawer and returns focus to its trigger.
  useEffect(() => {
    if (!drawerOpen) return;
    function handleKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setDrawerOpen(false);
        hamburgerRef.current?.focus();
      }
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [drawerOpen]);

  async function handleLogout() {
    setProfileOpen(false);
    await logout();
    await navigate({ to: "/login" });
  }

  async function submitNewFolder(event: FormEvent) {
    event.preventDefault();
    const name = newFolderName.trim();
    if (!name || !csrf) return;
    setCreatingFolder(true);
    setFolderError(null);
    try {
      await api.createFolder({ name, parent_id: currentFolderId }, csrf);
      setNewFolderName("");
      setNewFolderOpen(false);
      await queryClient.invalidateQueries({ queryKey: ["folders"] });
    } catch (err) {
      setFolderError(
        err instanceof ApiError ? err.detail : "Création du dossier impossible.",
      );
    } finally {
      setCreatingFolder(false);
    }
  }

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    // The only search surface is the files workspace; return there on submit.
    void navigate({ to: "/" });
  }

  const label = principalLabel(session.data?.role, session.data?.kind);
  const avatarInitial = label.charAt(0).toUpperCase();

  return (
    <FolderNavContext.Provider value={folderNav}>
    <div className="vault-shell">
      <aside
        id="vault-sidebar"
        className={drawerOpen ? "vault-sidebar vault-sidebar-open" : "vault-sidebar"}
        aria-label="Barre latérale"
      >
        <div className="vault-brand">
          <VaultMark />
          <span className="vault-brand-name">
            AURA <span className="vault-brand-accent">Vault</span>
          </span>
          <button
            type="button"
            className="vault-icon-btn vault-focus vault-drawer-close"
            aria-label="Masquer la navigation"
            onClick={() => {
              setDrawerOpen(false);
              hamburgerRef.current?.focus();
            }}
          >
            <CloseIcon />
          </button>
        </div>

        <nav className="vault-nav" aria-label="Navigation principale">
          <Link to="/" activeOptions={{ exact: true }} className="vault-nav-item vault-focus">
            <FolderNavIcon />
            Fichiers
          </Link>
          {canAdmin ? (
            <Link to="/admin" className="vault-nav-item vault-focus">
              <AdminNavIcon />
              Administration
            </Link>
          ) : null}
        </nav>

        <section className="vault-folders" aria-labelledby="vault-folders-label">
          <div className="vault-folders-head">
            <p className="vault-section-label" id="vault-folders-label">
              Dossiers
            </p>
            <button
              type="button"
              className="vault-icon-btn vault-focus"
              aria-label="Créer un dossier"
              aria-expanded={newFolderOpen}
              onClick={() => {
                setFolderError(null);
                setNewFolderOpen((open) => !open);
              }}
            >
              +
            </button>
          </div>
          {newFolderOpen ? (
            <form className="vault-folders-new" onSubmit={(e) => void submitNewFolder(e)}>
              <input
                type="text"
                autoFocus
                value={newFolderName}
                onChange={(e) => setNewFolderName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setNewFolderOpen(false);
                }}
                aria-label="Nom du nouveau dossier"
                placeholder="Nom du dossier…"
              />
              <button
                type="submit"
                className="vault-icon-btn vault-focus"
                aria-label="Valider la création"
                disabled={creatingFolder || newFolderName.trim() === ""}
              >
                ✓
              </button>
            </form>
          ) : null}
          {folderError ? (
            <p role="alert" className="vault-folders-note">
              {folderError}
            </p>
          ) : null}
          <ul className="vault-folder-list">
            <li
              className={
                currentFolderId === null
                  ? "vault-folder-item vault-folder-item-current"
                  : "vault-folder-item"
              }
            >
              <button
                type="button"
                className="vault-folder-open vault-focus"
                aria-current={currentFolderId === null ? "true" : undefined}
                onClick={() => setCurrentFolderId(null)}
              >
                <span className="vault-folder-icon" aria-hidden="true">
                  <FolderMark size={16} />
                </span>
                <span className="vault-folder-name">Racine</span>
              </button>
            </li>
            {allFolders.map((folder) => (
              <li
                key={folder.id}
                className={
                  currentFolderId === folder.id
                    ? "vault-folder-item vault-folder-item-current"
                    : "vault-folder-item"
                }
              >
                <button
                  type="button"
                  className="vault-folder-open vault-focus"
                  aria-current={currentFolderId === folder.id ? "true" : undefined}
                  onClick={() => setCurrentFolderId(folder.id)}
                >
                  <span className="vault-folder-icon" aria-hidden="true">
                    <FolderMark size={16} />
                  </span>
                  <span className="vault-folder-name">{folder.name}</span>
                </button>
                {folderActions ? (
                  <RowMenu
                    label={`Actions pour le dossier ${folder.name}`}
                    items={[
                      {
                        label: "Renommer",
                        onSelect: () => folderActions.rename(folder),
                      },
                      {
                        label: "Déplacer",
                        onSelect: () => folderActions.move(folder),
                      },
                      {
                        label: "Supprimer",
                        danger: true,
                        onSelect: () =>
                          folderActions.remove({ id: folder.id, name: folder.name }),
                      },
                    ]}
                  />
                ) : null}
              </li>
            ))}
          </ul>
          {treeQuery.isPending && signedIn ? (
            <p className="vault-folders-note">Chargement…</p>
          ) : null}
          {treeQuery.isError ? (
            <p className="vault-folders-note">Dossiers indisponibles.</p>
          ) : null}
          {signedIn && !treeQuery.isPending && !treeQuery.isError && allFolders.length === 0 ? (
            <p className="vault-folders-note">Aucun dossier</p>
          ) : null}
        </section>

        <div className="vault-usage">
          <p className="vault-section-label">Stockage</p>
          {canAdmin ? (
            <div className="vault-usage-card">
              {usage ? (
                <>
                  <div className="vault-ring-wrap">
                    <svg
                      className="vault-ring"
                      viewBox="0 0 42 42"
                      role="img"
                      aria-label="Espace utilisé (capacité totale non configurée)"
                    >
                      <circle
                        className="vault-ring-track"
                        cx="21"
                        cy="21"
                        r="16"
                        fill="none"
                        strokeWidth="4"
                      />
                    </svg>
                    <span className="vault-ring-value num">
                      {formatBytes(usage.totals.stored_bytes)}
                    </span>
                  </div>
                  <p className="vault-usage-note">Espace utilisé</p>
                  <p className="vault-usage-note">
                    {usage.totals.available_count} objet(s) vérifié(s). Capacité totale
                    non configurée : aucun pourcentage affiché.
                  </p>
                  <p className="vault-usage-note">
                    Usage réel (admin), mesuré le {formatDateTime(usage.measured_at)}.
                  </p>
                  {statusRows.length > 0 ? (
                    <ul className="vault-usage-list">
                      {statusRows.map((key) => (
                        <li key={key} className="vault-usage-row">
                          <span>{STATUS_FR[key] ?? key}</span>
                          <span className="vault-usage-count num">
                            {byStatus[key]}
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </>
              ) : usageQuery.isError ? (
                <p className="vault-usage-note">
                  Espace utilisé indisponible.{" "}
                  <button
                    type="button"
                    className="vault-focus"
                    onClick={() => void usageQuery.refetch()}
                  >
                    Réessayer
                  </button>
                </p>
              ) : (
                <p className="vault-usage-note">Mesure en cours…</p>
              )}
            </div>
          ) : (
            <div className="vault-usage-card">
              <p className="vault-usage-note">
                Données de stockage réservées aux administrateurs.
              </p>
            </div>
          )}
        </div>
      </aside>

      {drawerOpen ? (
        <div
          className="vault-scrim"
          aria-hidden="true"
          onClick={() => setDrawerOpen(false)}
        />
      ) : null}

      <div className="vault-main">
        <header className="vault-topbar">
          <button
            ref={hamburgerRef}
            type="button"
            className="vault-icon-btn vault-focus vault-hamburger"
            aria-label="Ouvrir la navigation"
            aria-controls="vault-sidebar"
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen(true)}
          >
            <MenuIcon />
          </button>

          <form className="vault-search" role="search" onSubmit={submitSearch}>
            <SearchIcon />
            <input
              ref={searchRef}
              type="search"
              aria-label="Recherche globale"
              placeholder="Rechercher un fichier, un dossier…"
            />
            <kbd className="vault-kbd">Ctrl K</kbd>
          </form>

          <div className="vault-topbar-actions">
            <div className="menu" ref={notifMenuRef}>
              <button
                type="button"
                className="vault-icon-btn vault-focus"
                aria-label="Notifications"
                aria-expanded={notifOpen}
                onClick={() => setNotifOpen((open) => !open)}
              >
                <BellIcon />
              </button>
              {notifOpen ? (
                <div className="menu-panel">
                  <p className="vault-menu-status">Aucune notification.</p>
                </div>
              ) : null}
            </div>

            {session.data ? (
              <div className="menu" ref={profileMenuRef}>
                <button
                  type="button"
                  className="vault-profile vault-focus"
                  aria-label={`Compte : ${label}`}
                  aria-expanded={profileOpen}
                  onClick={() => setProfileOpen((open) => !open)}
                >
                  <span className="vault-avatar" aria-hidden="true">
                    {avatarInitial}
                  </span>
                  <span className="vault-profile-meta">
                    <span className="vault-profile-name">{label}</span>
                    <span className="vault-profile-sub">Stockage AURA</span>
                  </span>
                  <ChevronIcon />
                </button>
                {profileOpen ? (
                  <div className="menu-panel">
                    <p className="vault-menu-status">
                      {csrf ? "Coffre déverrouillé" : "Coffre verrouillé"}
                    </p>
                    <button
                      type="button"
                      className="menu-item"
                      onClick={() => void handleLogout()}
                    >
                      Déconnexion
                    </button>
                  </div>
                ) : null}
              </div>
            ) : (
              <Link to="/login" className="vault-topbar-link vault-focus">
                Connexion
              </Link>
            )}
          </div>
        </header>

        <main className="content">{children}</main>
      </div>
    </div>
    </FolderNavContext.Provider>
  );
}
