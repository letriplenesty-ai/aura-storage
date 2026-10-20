/**
 * Content types the API will serve inline (mirror of PREVIEWABLE_CONTENT_TYPES
 * in api/main.py). This only decides whether to offer "Aperçu"; the API stays
 * authoritative and answers 415 for anything else.
 */
const PREVIEWABLE = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/avif",
  "application/pdf",
  "audio/mpeg",
  "audio/mp3",
  "audio/wav",
  "audio/x-wav",
  "audio/ogg",
  "audio/webm",
  "audio/mp4",
  "audio/aac",
  "audio/flac",
]);

export type PreviewKind = "image" | "audio" | "pdf";

function baseType(contentType: string | undefined): string {
  return (contentType ?? "").split(";")[0]?.trim().toLowerCase() ?? "";
}

export function isPreviewable(contentType: string | undefined): boolean {
  return PREVIEWABLE.has(baseType(contentType));
}

export function previewKind(contentType: string | undefined): PreviewKind | null {
  const type = baseType(contentType);
  if (!PREVIEWABLE.has(type)) return null;
  if (type.startsWith("image/")) return "image";
  if (type.startsWith("audio/")) return "audio";
  return "pdf";
}
