import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../lib/api";
import type { ObjectItem, PreviewIntentResponse } from "../lib/api";
import { previewKind } from "../lib/preview";

/** Above this size the browser does real work; warn instead of pulling it silently. */
const PREVIEW_WARN_BYTES = 50 * 1024 * 1024;

type Status = "loading" | "ready" | "error";

/**
 * Inline preview in a native <dialog> (Escape closes, focus stays inside).
 * The media source is the short-lived presigned URL, used directly and never
 * logged. There is no download fallback here: the API refuses types it does not
 * allow-list (415) and the message says so.
 */
export function PreviewModal({
  item,
  csrf,
  onClose,
  onDownload,
}: {
  item: ObjectItem | null;
  csrf: string | null;
  onClose: () => void;
  onDownload: (item: ObjectItem) => void;
}) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const [status, setStatus] = useState<Status>("loading");
  const [intent, setIntent] = useState<PreviewIntentResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const open = item !== null;

  useEffect(() => {
    const el = dialogRef.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    else if (!open && el.open) el.close();
  }, [open]);

  useEffect(() => {
    if (!item) return;
    setIntent(null);
    setError(null);
    if (!csrf) {
      setStatus("error");
      setError("Session expirée. Reconnectez-vous.");
      return;
    }
    let cancelled = false;
    setStatus("loading");
    api.createPreviewIntent(item.id, csrf).then(
      (result) => {
        if (cancelled) return;
        setIntent(result);
        setStatus("ready");
      },
      (err: unknown) => {
        if (cancelled) return;
        setError(
          err instanceof ApiError
            ? err.status === 415
              ? "Ce type de fichier ne peut pas être affiché dans le navigateur."
              : `Aperçu indisponible : ${err.detail}`
            : "Aperçu indisponible.",
        );
        setStatus("error");
      },
    );
    return () => {
      cancelled = true;
    };
  }, [item, csrf]);

  const kind = previewKind(intent?.content_type ?? item?.content_type);
  const size = intent?.size_bytes ?? item?.size_bytes ?? 0;

  return (
    <dialog
      ref={dialogRef}
      className="dialog preview-dialog"
      onClose={onClose}
      aria-labelledby="preview-title"
    >
      <div className="dialog-head">
        <h2 id="preview-title">{item ? `Aperçu — ${item.display_name}` : "Aperçu"}</h2>
        <button
          type="button"
          className="btn btn-ghost btn-small"
          onClick={onClose}
          aria-label="Fermer l'aperçu"
        >
          Fermer
        </button>
      </div>
      <div className="dialog-body preview-body">
        {status === "loading" ? (
          <p className="muted" role="status">
            Chargement de l'aperçu…
          </p>
        ) : null}

        {status === "error" ? (
          <div className="preview-fallback">
            <p role="alert" className="alert alert-error">
              {error}
            </p>
            {item && item.status === "available" ? (
              <button type="button" className="btn" onClick={() => onDownload(item)}>
                Télécharger
              </button>
            ) : null}
          </div>
        ) : null}

        {status === "ready" && item && intent && kind ? (
          <>
            {size > PREVIEW_WARN_BYTES ? (
              <p role="status" className="alert preview-warn">
                Fichier volumineux : l'aperçu peut solliciter fortement le navigateur.
              </p>
            ) : null}
            {kind === "image" ? (
              <img
                className="preview-media"
                src={intent.preview_url}
                alt={`Aperçu de ${item.display_name}`}
              />
            ) : kind === "audio" ? (
              <audio className="preview-audio" src={intent.preview_url} controls preload="metadata">
                Votre navigateur ne peut pas lire cet audio.
              </audio>
            ) : (
              <iframe
                className="preview-frame"
                src={intent.preview_url}
                title={`Aperçu PDF de ${item.display_name}`}
                referrerPolicy="no-referrer"
              />
            )}
          </>
        ) : null}
      </div>
    </dialog>
  );
}
