// Reusable file picker — sources from the Studio store, lets the user upload
// inline, returns the selection in two shapes:
//
//   single: `value` is a file id (or "" when nothing selected). `onChange(id)`.
//   multi:  `value` is a JSON-array string of ids. `onChange(jsonStr)`.
//
// Used by RunForm (workflow `file` / `file_list` params), AgentLaunchPage
// (files attached at launch) and AgentSessionPage (files attached to a steer).
// All three sites talk to the same /api/files endpoints + share the same
// rendering for thumbnails + selection chips.
import { useEffect, useState } from "react";
import { Icon } from "./Icon";
import { FileThumb } from "./FilePreview";
import { jget, jpostForm } from "@/lib/client";
import type { FileList as Files, FileRecord } from "@/lib/types";

export type FilePickerProps =
  | { multiple?: false; value: string; onChange: (id: string) => void; compact?: boolean }
  | { multiple: true; value: string; onChange: (ids: string) => void; compact?: boolean };

export function FilePickerInput(props: FilePickerProps) {
  const [files, setFiles] = useState<FileRecord[]>([]);
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const fileRef = useState<HTMLInputElement | null>(null);
  const compact = !!props.compact;
  const selectedIds = props.multiple
    ? (() => { try { const j = JSON.parse(props.value); return Array.isArray(j) ? j.map(String) : []; } catch { return []; } })()
    : (props.value ? [props.value] : []);

  useEffect(() => {
    const qs = new URLSearchParams();
    if (search) qs.set("search", search);
    qs.set("limit", "60");
    jget<Files>(`/api/files?${qs}`).then((d) => setFiles(d.files)).catch(() => {});
  }, [search]);

  const toggle = (id: string) => {
    if (props.multiple) {
      const cur = selectedIds.slice();
      const i = cur.indexOf(id);
      if (i >= 0) cur.splice(i, 1); else cur.push(id);
      props.onChange(JSON.stringify(cur));
    } else {
      props.onChange(selectedIds[0] === id ? "" : id);
    }
  };

  const upload = async (list: FileList | null) => {
    if (!list || list.length === 0) return;
    setBusy(true); setErr("");
    const uploaded: string[] = [];
    try {
      for (const f of Array.from(list)) {
        const fd = new FormData(); fd.append("file", f); fd.append("name", f.name);
        const r = await jpostForm<{ file: FileRecord }>("/api/files", fd);
        uploaded.push(r.file.id);
      }
      if (props.multiple) {
        const cur = selectedIds.slice();
        for (const id of uploaded) if (cur.indexOf(id) < 0) cur.push(id);
        props.onChange(JSON.stringify(cur));
      } else if (uploaded.length) {
        props.onChange(uploaded[0]);
      }
      const d = await jget<Files>(`/api/files?limit=60`);
      setFiles(d.files);
    } catch (e) { setErr(String((e as Error).message)); }
    finally { setBusy(false); }
  };

  return (
    <div className="flex flex-col gap-2 p-2 rounded border" style={{ borderColor: "var(--color-line)" }}>
      <div className="flex items-center gap-2">
        <input className="input flex-1" placeholder="Search files…" value={search}
               onChange={(e) => setSearch(e.target.value)} />
        <input ref={(r) => { fileRef[0] = r; }} type="file" multiple={props.multiple} className="hidden"
               onChange={(e) => upload(e.target.files)} />
        <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => fileRef[0]?.click()}>
          <Icon name="upload" size={12} className="inline mr-1 align-text-bottom" />
          {busy ? "Uploading…" : "Upload"}
        </button>
      </div>
      {err && <div className="text-[12px]" style={{ color: "#ff6363" }}>{err}</div>}
      {selectedIds.length > 0 && (
        <div className="flex flex-wrap gap-1.5 pb-1 border-b" style={{ borderColor: "var(--color-line)" }}>
          {selectedIds.map((id) => {
            const f = files.find((x) => x.id === id);
            return (
              <span key={id} className="inline-flex items-center gap-1.5 px-1.5 py-0.5 rounded"
                    style={{ background: "#0c1c2c", border: "1px solid #1a3550" }}>
                {f ? <FileThumb file={f} size={18} /> : <Icon name="file" size={12} />}
                <span className="text-[11px] truncate max-w-40">{f?.name ?? id}</span>
                <button type="button" onClick={() => toggle(id)} className="opacity-70 hover:opacity-100">
                  <Icon name="x" size={11} />
                </button>
              </span>
            );
          })}
        </div>
      )}
      <div className={`grid gap-1.5 overflow-y-auto ${compact ? "max-h-28" : "max-h-44"}`}
           style={{ gridTemplateColumns: "repeat(auto-fill, minmax(120px, 1fr))" }}>
        {files.map((f) => {
          const sel = selectedIds.includes(f.id);
          return (
            <button key={f.id} type="button" onClick={() => toggle(f.id)}
                    className="flex items-center gap-1.5 p-1.5 rounded text-left"
                    style={{ background: sel ? "#0c1c2c" : "#0c0c0c",
                             border: sel ? "1px solid #1a3550" : "1px solid var(--color-line)" }}>
              <FileThumb file={f} size={22} />
              <span className="text-[11px] truncate flex-1">{f.name}</span>
            </button>
          );
        })}
        {files.length === 0 && (
          <div className="col-span-full text-center text-faint text-[12px] py-4">No files yet — upload one above.</div>
        )}
      </div>
    </div>
  );
}

// Compact inline picker for a steer chat input: a paperclip button that
// reveals a small floating panel. Returns the same multi shape (JSON array
// string in `value`). Renders selected chips inline so the user always sees
// what's attached even when the panel is closed.
export function FileAttachPopover({ value, onChange }: { value: string; onChange: (ids: string) => void }) {
  const [open, setOpen] = useState(false);
  const selectedIds = (() => {
    try { const j = JSON.parse(value); return Array.isArray(j) ? j.map(String) : []; } catch { return []; }
  })();
  return (
    <div className="relative">
      <button type="button" onClick={() => setOpen((v) => !v)}
              title={`Attach files (${selectedIds.length} selected)`}
              className="p-1.5 rounded hover:bg-[#1a1a1a] inline-flex items-center gap-1"
              style={{ color: selectedIds.length ? "#3b9eff" : "var(--color-muted)" }}>
        <Icon name="paperclip" size={15} />
        {selectedIds.length > 0 && <span className="text-[11px]">{selectedIds.length}</span>}
      </button>
      {open && (
        <div className="absolute bottom-full mb-2 right-0 w-96 z-30 rounded-lg shadow-2xl bg-panel border"
             style={{ borderColor: "var(--color-line)" }}>
          <div className="px-3 py-2 border-b text-[12px] flex items-center justify-between"
               style={{ borderColor: "var(--color-line)" }}>
            <span className="font-medium">Attach files</span>
            <button type="button" onClick={() => setOpen(false)} className="opacity-70 hover:opacity-100">
              <Icon name="x" size={14} />
            </button>
          </div>
          <div className="p-2">
            <FilePickerInput multiple value={value} onChange={onChange} compact />
          </div>
        </div>
      )}
    </div>
  );
}
