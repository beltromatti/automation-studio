// Files page — the binary-data peer of the Data page. Grid view of every file
// in the Studio store, with upload (button + drag-and-drop on the page),
// filters (mime / source / tag / search), preview modal, rename, tags edit,
// delete (with safety-check against dataset references). Everything wired to
// the same /api/files endpoints the MCP server uses, so what a human does here
// and what an agent does via MCP are literally the same actions.
import { useCallback, useEffect, useRef, useState } from "react";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { FileThumb, FilePreviewModal, fileIcon } from "@/components/FilePreview";
import { jget, jpostForm, jpost, jdel, fileUrl, formatBytes, timeAgo } from "@/lib/client";
import type { FileList, FileRecord, FileReference } from "@/lib/types";

const MIME_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "All types" },
  { value: "image/*", label: "Images" },
  { value: "video/*", label: "Video" },
  { value: "audio/*", label: "Audio" },
  { value: "application/pdf", label: "PDF" },
  { value: "text/*", label: "Text" },
  { value: "application/json", label: "JSON" },
];

export default function FilesPage() {
  const [files, setFiles] = useState<FileRecord[]>([]);
  const [count, setCount] = useState(0);
  const [mime, setMime] = useState("");
  const [source, setSource] = useState("");
  const [tag, setTag] = useState("");
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<FileRecord | null>(null);
  const [editing, setEditing] = useState<FileRecord | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const stopped = useRef(false);

  const load = useCallback(async () => {
    const qs = new URLSearchParams();
    if (mime) qs.set("mime", mime);
    if (source) qs.set("source", source);
    if (tag) qs.set("tag", tag);
    if (search) qs.set("search", search);
    qs.set("limit", "300");
    try {
      const d = await jget<FileList>(`/api/files?${qs}`);
      if (!stopped.current) { setFiles(d.files); setCount(d.count); }
    } catch (e) { setError(String((e as Error).message)); }
  }, [mime, source, tag, search]);

  useEffect(() => {
    stopped.current = false;
    load();
    const t = setInterval(load, 4000);
    return () => { stopped.current = true; clearInterval(t); };
  }, [load]);

  const upload = async (fileList: FileList_DOM) => {
    if (!fileList || fileList.length === 0) return;
    setBusy(true); setError("");
    try {
      for (const f of Array.from(fileList)) {
        const fd = new FormData();
        fd.append("file", f);
        if (f.name) fd.append("name", f.name);
        await jpostForm("/api/files", fd);
      }
      await load();
    } catch (e) { setError(String((e as Error).message)); }
    finally { setBusy(false); if (fileInputRef.current) fileInputRef.current.value = ""; }
  };

  // Drag-and-drop the whole page area as an upload target. Visible "drop here"
  // hint highlights when files are over the page.
  useEffect(() => {
    const onDragOver = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes("Files")) {
        e.preventDefault();
        setDragOver(true);
      }
    };
    const onDragLeave = (e: DragEvent) => {
      // ignore inner dragleave bubbling
      if ((e as DragEvent & { relatedTarget?: unknown }).relatedTarget == null) setDragOver(false);
    };
    const onDrop = (e: DragEvent) => {
      if (!e.dataTransfer?.files?.length) return;
      e.preventDefault();
      setDragOver(false);
      upload(e.dataTransfer.files as unknown as FileList_DOM);
    };
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("drop", onDrop);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onDelete = async (f: FileRecord) => {
    setError("");
    try {
      const r = await jdel<{ ok?: boolean; refs?: FileReference[]; error?: string }>(`/api/files/${f.id}`);
      if (r.ok) { await load(); return; }
      if (r.refs && r.refs.length) {
        const msg = `"${f.name}" is referenced by ${r.refs.length} dataset cell${r.refs.length > 1 ? "s" : ""}: `
          + r.refs.slice(0, 3).map((x) => `${x.datasetName}/${x.column}#${x.rid}`).join(", ")
          + (r.refs.length > 3 ? "…" : "")
          + ". Delete anyway?";
        if (!confirm(msg)) return;
        await jdel(`/api/files/${f.id}?force=1`);
        await load();
        return;
      }
      throw new Error(r.error || "delete failed");
    } catch (e) { setError(String((e as Error).message)); }
  };

  return (
    <>
      <Header
        title="Files"
        sub={`${count} file${count === 1 ? "" : "s"} in the store. Drop files anywhere on this page to upload.`}
        actions={
          <div className="flex items-center gap-2">
            <input ref={fileInputRef} type="file" multiple className="hidden"
                   onChange={(e) => e.target.files && upload(e.target.files as unknown as FileList_DOM)} />
            <button className="btn btn-primary" onClick={() => fileInputRef.current?.click()} disabled={busy}>
              <Icon name="upload" size={14} className="inline mr-1.5 align-text-bottom" />
              {busy ? "Uploading…" : "Upload"}
            </button>
          </div>
        }
      />

      {/* Filters */}
      <div className="px-6 py-3 border-b flex flex-wrap gap-2" style={{ borderColor: "var(--color-line)" }}>
        <select className="input w-36" value={mime} onChange={(e) => setMime(e.target.value)}>
          {MIME_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        <input className="input flex-1 min-w-45" placeholder="Search by name…"
               value={search} onChange={(e) => setSearch(e.target.value)} />
        <input className="input w-32" placeholder="tag" value={tag} onChange={(e) => setTag(e.target.value)} />
        <input className="input w-44" placeholder="source prefix (run:, browser:, …)"
               value={source} onChange={(e) => setSource(e.target.value)} />
      </div>

      {error && (
        <div className="mx-6 my-3 px-3 py-2 rounded text-[12px]"
             style={{ background: "#3a1010", border: "1px solid #531818" }}>{error}</div>
      )}

      <div className="p-6 grid gap-3"
           style={{ gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))" }}>
        {files.map((f) => (
          <div key={f.id} className="rounded-lg border bg-panel flex flex-col overflow-hidden group"
               style={{ borderColor: "var(--color-line)" }}>
            <div className="aspect-4/3 flex items-center justify-center bg-card cursor-pointer relative"
                 onClick={() => setPreview(f)}>
              {f.mime.startsWith("image/") ? (
                <img src={fileUrl(f.id)} alt={f.name} className="w-full h-full object-cover" draggable={false} />
              ) : (
                <Icon name={fileIcon(f.mime)} size={42} />
              )}
            </div>
            <div className="p-2.5 flex flex-col gap-1.5 min-h-0">
              <div className="text-[12px] font-medium truncate" title={f.name}>{f.name}</div>
              <div className="text-[10.5px] text-faint flex items-center gap-1.5 flex-wrap">
                <span>{f.mime.split("/")[0]}</span>
                <span>·</span>
                <span>{formatBytes(f.size)}</span>
                <span>·</span>
                <span>{timeAgo(f.created_at)}</span>
              </div>
              {f.tags?.length ? (
                <div className="flex flex-wrap gap-1">
                  {f.tags.slice(0, 4).map((t) => (
                    <span key={t} className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: "#161616" }}>#{t}</span>
                  ))}
                  {f.tags.length > 4 && <span className="text-[10px] text-faint">+{f.tags.length - 4}</span>}
                </div>
              ) : null}
              <div className="flex items-center gap-1 mt-1 opacity-0 group-hover:opacity-100 transition-opacity">
                <button onClick={() => setEditing(f)} className="p-1 rounded hover:bg-[#1a1a1a]" title="Rename / tags">
                  <Icon name="pencil" size={13} />
                </button>
                <a href={fileUrl(f.id, "download")} download={f.name} className="p-1 rounded hover:bg-[#1a1a1a]" title="Download">
                  <Icon name="download" size={13} />
                </a>
                <button onClick={() => onDelete(f)} className="p-1 rounded hover:bg-[#1a1a1a] ml-auto" title="Delete">
                  <Icon name="trash" size={13} />
                </button>
              </div>
            </div>
          </div>
        ))}
        {files.length === 0 && (
          <div className="col-span-full text-center text-faint text-[13px] py-12">
            <FileThumb file={{ id: "", sha256: "", ext: "", name: "", mime: "image/empty", size: 0,
                                created_at: 0, tags: [], path: "" }} size={48} />
            <div className="mt-3">No files yet — upload one or run a workflow that captures media.</div>
          </div>
        )}
      </div>

      {dragOver && (
        <div className="fixed inset-0 z-40 pointer-events-none flex items-center justify-center"
             style={{ background: "rgba(0, 114, 245, 0.10)", border: "2px dashed #3b9eff" }}>
          <div className="px-4 py-2 rounded-md text-[13px] font-medium"
               style={{ background: "#0072f5", color: "#fff" }}>Drop to upload</div>
        </div>
      )}

      {preview && <FilePreviewModal file={preview} onClose={() => setPreview(null)} />}
      {editing && <EditFileModal file={editing} onClose={async (changed) => { setEditing(null); if (changed) await load(); }} />}
    </>
  );
}

function EditFileModal({ file, onClose }: { file: FileRecord; onClose: (changed: boolean) => void }) {
  const [name, setName] = useState(file.name);
  const [tags, setTags] = useState((file.tags || []).join(", "));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const save = async () => {
    setBusy(true); setErr("");
    try {
      if (name && name !== file.name) await jpost(`/api/files/${file.id}/rename`, { name });
      const newTags = tags.split(",").map((t) => t.trim()).filter(Boolean);
      const wasTags = (file.tags || []).join("|");
      if (newTags.join("|") !== wasTags) await jpost(`/api/files/${file.id}/tags`, { tags: newTags });
      onClose(true);
    } catch (e) { setErr(String((e as Error).message)); }
    finally { setBusy(false); }
  };
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,0.6)" }}
         onClick={() => onClose(false)}>
      <div className="bg-panel border rounded-lg shadow-2xl flex flex-col w-105"
           style={{ borderColor: "var(--color-line)" }} onClick={(e) => e.stopPropagation()}>
        <div className="px-4 py-3 border-b flex items-center gap-2" style={{ borderColor: "var(--color-line)" }}>
          <FileThumb file={file} size={28} /> <span className="text-[13px] font-medium truncate">{file.name}</span>
        </div>
        <div className="p-4 flex flex-col gap-3">
          <div>
            <label className="block text-[11px] text-faint mb-1">Name</label>
            <input className="input w-full" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div>
            <label className="block text-[11px] text-faint mb-1">Tags (comma-separated)</label>
            <input className="input w-full" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="screenshot, reddit, …" />
          </div>
          {err && <div className="text-[12px]" style={{ color: "#ff6363" }}>{err}</div>}
          <div className="flex gap-2 justify-end mt-1">
            <button className="btn" onClick={() => onClose(false)}>Cancel</button>
            <button className="btn btn-primary" onClick={save} disabled={busy}>{busy ? "Saving…" : "Save"}</button>
          </div>
        </div>
      </div>
    </div>
  );
}

// Alias to avoid the (DOM) FileList name shadowing the type from types.ts in the
// upload signature. JS's FileList is global; Studio's `FileList` is our own.
type FileList_DOM = globalThis.FileList;
