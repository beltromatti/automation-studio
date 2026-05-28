// Shared file preview surface — a thumbnail/icon used in cells, lists and
// pickers, plus a modal that opens the full preview (image, video, audio, text
// or a generic file with a download link). Single source of truth for "how do
// we render a Studio file in the UI"; every place files appear delegates here.
import { useEffect, useMemo, useState } from "react";
import { Icon } from "./Icon";
import { fileUrl, jget, formatBytes } from "@/lib/client";
import type { FileRecord } from "@/lib/types";

export function fileIcon(mime: string): string {
  const m = (mime || "").toLowerCase();
  if (m.startsWith("image/")) return "image";
  if (m.startsWith("video/")) return "film";
  if (m.startsWith("audio/")) return "music";
  if (m === "application/pdf") return "file";
  if (m.startsWith("text/") || m === "application/json" || m === "application/xml") return "code";
  return "file";
}

export function FileThumb({ file, size = 36, onClick }:
                          { file: FileRecord; size?: number; onClick?: () => void }) {
  const isImage = (file.mime || "").startsWith("image/");
  const sty: React.CSSProperties = {
    width: size, height: size, borderRadius: 6, background: "#161616",
    border: "1px solid var(--color-line)", cursor: onClick ? "pointer" : "default",
    overflow: "hidden", flexShrink: 0, display: "flex", alignItems: "center", justifyContent: "center",
  };
  return (
    <div style={sty} onClick={onClick} title={file.name}>
      {isImage ? (
        <img src={fileUrl(file.id, "preview")} alt={file.name}
             style={{ width: "100%", height: "100%", objectFit: "cover" }} draggable={false} />
      ) : (
        <Icon name={fileIcon(file.mime)} size={Math.max(14, size - 18)} />
      )}
    </div>
  );
}

export function FilePreviewModal({ file, onClose }: { file: FileRecord; onClose: () => void }) {
  const [text, setText] = useState<string | null>(null);
  const [textual, setTextual] = useState<boolean | null>(null);

  useEffect(() => {
    if (!file) return;
    let stopped = false;
    jget<{ text: string | null; textual: boolean }>(`/api/files/${file.id}/view`)
      .then((d) => { if (!stopped) { setText(d.text); setTextual(d.textual); } })
      .catch(() => { if (!stopped) setTextual(false); });
    return () => { stopped = true; };
  }, [file]);

  const m = file.mime.toLowerCase();
  const isImage = m.startsWith("image/");
  const isVideo = m.startsWith("video/");
  const isAudio = m.startsWith("audio/");
  const isPdf = m === "application/pdf";
  const src = fileUrl(file.id, "preview");

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center"
         style={{ background: "rgba(0,0,0,0.7)" }} onClick={onClose}>
      <div className="bg-panel border rounded-lg shadow-2xl flex flex-col"
           style={{ borderColor: "var(--color-line)", width: "min(900px, 92vw)", maxHeight: "92vh" }}
           onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-3 px-4 py-3 border-b" style={{ borderColor: "var(--color-line)" }}>
          <Icon name={fileIcon(file.mime)} size={18} />
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-medium truncate">{file.name}</div>
            <div className="text-[11px] text-faint mt-0.5">
              {file.mime} · {formatBytes(file.size)} · id {file.id}
              {file.tags?.length ? <> · {file.tags.map((t) => `#${t}`).join(" ")}</> : null}
            </div>
          </div>
          <a href={fileUrl(file.id, "download")} download={file.name}
             className="px-2.5 py-1 text-[12px] rounded-md border hover:bg-[#1a1a1a]"
             style={{ borderColor: "var(--color-line)" }}>
            <Icon name="download" size={13} className="inline mr-1 align-text-bottom" />Download
          </a>
          <button onClick={onClose} className="px-2 py-1 rounded-md hover:bg-[#1a1a1a]">
            <Icon name="x" size={16} />
          </button>
        </div>
        <div className="flex-1 overflow-auto p-4 flex items-center justify-center">
          {isImage && <img src={src} alt={file.name} className="max-w-full max-h-[70vh] rounded" />}
          {isVideo && <video src={src} controls className="max-w-full max-h-[70vh] rounded" />}
          {isAudio && <audio src={src} controls className="w-full" />}
          {isPdf && (
            <iframe src={src} title={file.name} className="w-full"
                    style={{ minHeight: "70vh", border: 0, background: "#fff" }} />
          )}
          {!isImage && !isVideo && !isAudio && !isPdf && textual && text != null && (
            <pre className="w-full text-[12px] font-mono whitespace-pre-wrap break-words p-3 rounded"
                 style={{ background: "#0c0c0c", maxHeight: "70vh", overflow: "auto" }}>{text}</pre>
          )}
          {!isImage && !isVideo && !isAudio && !isPdf && textual === false && (
            <div className="text-center text-faint text-[13px] py-12">
              <Icon name={fileIcon(file.mime)} size={48} />
              <div className="mt-3">No inline preview for this type — use Download.</div>
            </div>
          )}
          {textual === null && !isImage && !isVideo && !isAudio && !isPdf && (
            <div className="text-faint text-[13px]">loading…</div>
          )}
        </div>
      </div>
    </div>
  );
}

// One-stop "click to preview" wrapper that fetches the record if only an id is
// known (e.g. inside a dataset cell).
export function FileChip({ id, onChange }: { id: string; onChange?: () => void }) {
  const [rec, setRec] = useState<FileRecord | null>(null);
  const [open, setOpen] = useState(false);
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    if (!id) return;
    let stopped = false;
    jget<{ file: FileRecord }>(`/api/files/${id}`)
      .then((d) => { if (!stopped) setRec(d.file); })
      .catch(() => { if (!stopped) setMissing(true); });
    return () => { stopped = true; };
  }, [id]);
  if (missing || !id) {
    return <span className="text-faint font-mono text-[11px]">{id || "—"} (missing)</span>;
  }
  if (!rec) {
    return <span className="text-faint font-mono text-[11px]">{id}…</span>;
  }
  return (
    <>
      <span className="inline-flex items-center gap-2 cursor-pointer rounded-md px-1 py-0.5 hover:bg-[#1a1a1a]"
            onClick={() => setOpen(true)}>
        <FileThumb file={rec} size={24} />
        <span className="text-[12px] truncate" title={rec.name}>{rec.name}</span>
      </span>
      {open && <FilePreviewModal file={rec} onClose={() => { setOpen(false); onChange?.(); }} />}
    </>
  );
}

// Render a `file_list` cell: stack of small thumbnails + count chip.
export function FileChipList({ ids }: { ids: string[] }) {
  if (!ids || ids.length === 0) return <span className="text-faint">—</span>;
  return (
    <span className="inline-flex items-center gap-1">
      {ids.slice(0, 4).map((id) => <FileChip key={id} id={id} />)}
      {ids.length > 4 && <span className="text-[11px] text-faint">+{ids.length - 4}</span>}
    </span>
  );
}

// Parse a cell value that's supposed to be a file_list (JSON array of ids).
export function parseFileList(v: unknown): string[] {
  if (Array.isArray(v)) return v.map(String);
  if (typeof v === "string" && v.trim().startsWith("[")) {
    try { const j = JSON.parse(v); return Array.isArray(j) ? j.map(String) : []; } catch { return []; }
  }
  return [];
}
