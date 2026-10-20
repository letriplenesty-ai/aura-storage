/**
 * Upload journal: remembers which files of an import are still to send, so a
 * page reload, a closed tab or a sleeping laptop does not lose a long import
 * (hundreds of gigabytes take hours). Stored in IndexedDB, in this browser only.
 *
 * Browsers do not let a page reopen a file by itself after a reload, so the
 * journal keeps metadata only (path, size, last modification, target folder).
 * To resume, the user selects the same files or folder again; they are matched
 * by fingerprint and only the files not yet confirmed by the server are sent.
 * File contents are never stored.
 */

export interface JournalEntry {
  /** Fingerprint: owner, relative path, size and last modification. */
  key: string;
  owner: string;
  rel: string;
  size: number;
  folderId: string | null;
}

const DB_NAME = "aura-upload-journal";
const STORE = "files";

/** Same file at the same place: path, size and mtime all equal. */
export function fingerprint(owner: string, rel: string, file: File): string {
  return `${owner}\u0000${rel}\u0000${file.size}\u0000${file.lastModified}`;
}

function openDb(): Promise<IDBDatabase | null> {
  return new Promise((resolve) => {
    if (typeof indexedDB === "undefined") {
      resolve(null);
      return;
    }
    let request: IDBOpenDBRequest;
    try {
      request = indexedDB.open(DB_NAME, 1);
    } catch {
      resolve(null);
      return;
    }
    request.onupgradeneeded = () => {
      const store = request.result.createObjectStore(STORE, { keyPath: "key" });
      store.createIndex("owner", "owner");
    };
    request.onsuccess = () => resolve(request.result);
    // Private windows or blocked storage: resuming is a convenience, the
    // import itself keeps working without it.
    request.onerror = () => resolve(null);
    request.onblocked = () => resolve(null);
  });
}

async function run<T>(
  mode: IDBTransactionMode,
  body: (store: IDBObjectStore) => IDBRequest<T> | void,
): Promise<T | undefined> {
  const db = await openDb();
  if (!db) return undefined;
  return new Promise((resolve) => {
    try {
      const tx = db.transaction(STORE, mode);
      const request = body(tx.objectStore(STORE));
      tx.oncomplete = () => {
        db.close();
        resolve(request ? request.result : undefined);
      };
      tx.onerror = () => {
        db.close();
        resolve(undefined);
      };
    } catch {
      db.close();
      resolve(undefined);
    }
  });
}

/** Record files about to be sent (an existing entry is simply refreshed). */
export async function addEntries(entries: JournalEntry[]): Promise<void> {
  if (entries.length === 0) return;
  await run("readwrite", (store) => {
    for (const entry of entries) store.put(entry);
  });
}

/** Forget files: confirmed by the server, or cancelled by the user. */
export async function removeEntries(keys: string[]): Promise<void> {
  if (keys.length === 0) return;
  await run("readwrite", (store) => {
    for (const key of keys) store.delete(key);
  });
}

/** Files of this account still to send. */
export async function pendingEntries(owner: string): Promise<JournalEntry[]> {
  const result = await run<JournalEntry[]>("readonly", (store) =>
    store.index("owner").getAll(owner) as IDBRequest<JournalEntry[]>,
  );
  return result ?? [];
}

export async function clearOwner(owner: string): Promise<void> {
  const keys = (await pendingEntries(owner)).map((entry) => entry.key);
  await removeEntries(keys);
}
