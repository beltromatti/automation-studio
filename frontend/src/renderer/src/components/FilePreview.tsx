// Shared file preview surface — a thumbnail/icon used in cells, lists and
// pickers, plus a modal that opens the full preview (image, video, audio, text
// or a generic file with a download link). Single source of truth for "how do
// we render a Studio file in the UI"; every place files appear delegates here.
import { useEffect, useMemo, useState } from "react";
import { Icon } from "./Icon";
import { FilePickerInput } from "./FilePickerInput";
import { fileUrl, jget, jpost, jpostForm, formatBytes } from "@/lib/client";
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

// In-cell editor for `file` / `file_list` dataset columns. Click to open a
// floating panel anchored to the cell with the full FilePickerInput inside,
// drop a file from the desktop onto the cell to upload + set in one motion.
// Saves via the dataset's update-cell API. Used by DatasetPage so a human can
// build / curate file-typed columns with the same fluency as text columns.
export function FileCellEditor({
  datasetId, rid, column, multiple, value, onSaved,
}: {
  datasetId: string;
  rid: number;
  column: string;          // display name (the update_cell API uses display names)
  multiple: boolean;       // file_list when true
  value: unknown;          // current cell content (id string OR JSON array string)
  onSaved: () => void;     // refresh callback the parent runs after save
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<string>(() => normalize(value, multiple));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [dragOver, setDragOver] = useState(false);

  function normalize(v: unknown, list: boolean): string {
    if (list) {
      const ids = parseFileList(v);
      return JSON.stringify(ids);
    }
    return v == null || v === "" ? "" : String(v);
  }

  // Re-seed the draft each time the panel opens so we always start from the
  // cell's current truth (the parent may have re-fetched in between).
  useEffect(() => { if (open) setDraft(normalize(value, multiple)); }, [open, value, multiple]);

  const save = async (newValue: string) => {
    setBusy(true); setErr("");
    try {
      await jpost(`/api/datasets/${datasetId}/cell`, { rid, column, value: newValue });
      onSaved();
      setOpen(false);
    } catch (e) { setErr(String((e as Error).message)); }
    finally { setBusy(false); }
  };

  // Drag-and-drop a file from the desktop directly onto the cell.
  const onDrop = async (e: React.DragEvent) => {
    setDragOver(false);
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault(); e.stopPropagation();
    setBusy(true); setErr("");
    const uploaded: string[] = [];
    try {
      for (const f of Array.from(e.dataTransfer.files)) {
        const fd = new FormData(); fd.append("file", f); fd.append("name", f.name);
        const r = await jpostForm<{ file: FileRecord }>("/api/files", fd);
        uploaded.push(r.file.id);
      }
      if (multiple) {
        const ids = parseFileList(value);
        for (const id of uploaded) if (ids.indexOf(id) < 0) ids.push(id);
        await save(JSON.stringify(ids));
      } else if (uploaded.length) {
        await save(uploaded[0]);
      }
    } catch (ex) { setErr(String((ex as Error).message)); setBusy(false); }
  };

  const ids = parseFileList(value);
  const singleId = (!multiple && value) ? String(value) : "";
  const empty = multiple ? ids.length === 0 : !singleId;

  return (
    <div className="relative"
         onDragOver={(e) => { if (e.dataTransfer?.types?.includes("Files")) { e.preventDefault(); setDragOver(true); } }}
         onDragLeave={() => setDragOver(false)}
         onDrop={onDrop}>
      <button type="button" onClick={() => setOpen((v) => !v)}
              className="w-full text-left px-1.5 py-1 rounded hover:bg-[#1a1a1a] transition-colors"
              style={{ outline: dragOver ? "1.5px dashed #3b9eff" : "none", outlineOffset: -2 }}>
        {multiple
          ? <FileChipList ids={ids} />
          : (singleId
              ? <FileChip id={singleId} />
              : <span className="text-faint text-[11.5px]">— click or drop a file —</span>)}
      </button>
      {open && (
        <div className="absolute top-full left-0 mt-1 w-105 z-30 rounded-lg shadow-2xl bg-panel border"
             style={{ borderColor: "var(--color-line)" }} onClick={(e) => e.stopPropagation()}>
          <div className="px-3 py-2 border-b flex items-center justify-between text-[12px]"
               style={{ borderColor: "var(--color-line)" }}>
            <span className="font-medium">{multiple ? "Edit attachments" : "Set file"}</span>
            <div className="flex items-center gap-1.5">
              {!empty && (
                <button type="button" onClick={() => save(multiple ? "[]" : "")} disabled={busy}
                        className="text-[11.5px] px-1.5 py-0.5 rounded hover:bg-[#1a1a1a] text-faint">
                  clear
                </button>
              )}
              <button type="button" onClick={() => setOpen(false)} className="p-1 rounded hover:bg-[#1a1a1a]">
                <Icon name="x" size={13} />
              </button>
            </div>
          </div>
          <div className="p-2">
            {multiple ? (
              <FilePickerInput multiple value={draft} onChange={(v) => { setDraft(v); save(v); }} compact />
            ) : (
              <FilePickerInput value={draft} onChange={(v) => { setDraft(v); save(v); }} compact />
            )}
          </div>
          {err && <div className="px-3 py-1.5 text-[11.5px]" style={{ color: "#ff6363" }}>{err}</div>}
          {dragOver && (
            <div className="px-3 py-1.5 text-[11.5px] text-running">drop file(s) here to upload + attach</div>
          )}
        </div>
      )}
    </div>
  );
}
