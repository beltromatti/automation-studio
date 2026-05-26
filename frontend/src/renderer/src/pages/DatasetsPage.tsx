import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { jget, jpost, jdel, timeAgo } from "@/lib/client";
import type { Dataset, DatasetSource } from "@/lib/types";

const SOURCE_LABEL: Record<DatasetSource["kind"], string> = {
  manual: "manual",
  import: "imported",
  run: "from run",
  project: "projection",
  merge: "merged",
};

export default function DatasetsPage() {
  const navigate = useNavigate();
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const stopped = useRef(false);

  const load = useCallback(async () => {
    try {
      const d = await jget<{ datasets: Dataset[] }>("/api/datasets");
      if (!stopped.current) setDatasets(d.datasets);
    } catch {}
  }, []);

  useEffect(() => {
    stopped.current = false;
    load();
    const t = setInterval(load, 5000);
    return () => { stopped.current = true; clearInterval(t); };
  }, [load]);

  const create = async () => {
    const name = newName.trim();
    if (!name) return;
    setError("");
    try {
      const { dataset } = await jpost<{ dataset: Dataset }>("/api/datasets", { name, source: { kind: "manual" } });
      setNewName(""); setCreating(false);
      navigate(`/data/${dataset.id}`);
    } catch (e) { setError(String((e as Error).message)); }
  };

  const importCsv = async (file: File) => {
    setBusy(true); setError("");
    try {
      const text = await file.text();
      const name = file.name.replace(/\.csv$/i, "");
      const res = await jpost<{ datasetId: string }>("/api/datasets/import", { name, csv: text });
      await load();
      navigate(`/data/${res.datasetId}`);
    } catch (e) { setError(String((e as Error).message)); }
    finally { setBusy(false); if (fileRef.current) fileRef.current.value = ""; }
  };

  const toggle = (id: string) =>
    setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });

  const mergeSelected = async () => {
    if (selected.size < 2) return;
    setBusy(true); setError("");
    try {
      const ids = [...selected];
      const name = `Merged (${ids.length})`;
      const { dataset } = await jpost<{ dataset: Dataset }>("/api/datasets/merge", { ids, name });
      setSelected(new Set());
      navigate(`/data/${dataset.id}`);
    } catch (e) { setError(String((e as Error).message)); }
    finally { setBusy(false); }
  };

  const del = async (id: string) => {
    await jdel(`/api/datasets/${id}`).catch(() => {});
    setSelected((s) => { const n = new Set(s); n.delete(id); return n; });
    load();
  };

  return (
    <>
      <Header
        title="Data"
        sub="Persistent, cross-run tables — capture results, clean & combine them, prep inputs for the next workflow"
        actions={
          <>
            {selected.size >= 2 && (
              <button className="btn btn-secondary btn-sm" disabled={busy} onClick={mergeSelected}>
                <Icon name="merge" size={14} /> Merge {selected.size}
              </button>
            )}
            <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => fileRef.current?.click()}>
              <Icon name="download" size={14} /> Import CSV
            </button>
            <button className="btn btn-primary btn-sm" onClick={() => setCreating((c) => !c)}>
              <Icon name="plus" size={14} /> New dataset
            </button>
            <input ref={fileRef} type="file" accept=".csv,text/csv" className="hidden"
                   onChange={(e) => { const f = e.target.files?.[0]; if (f) importCsv(f); }} />
          </>
        }
      />
      <div className="px-7 py-6 max-w-[920px]">
        <div className="card p-4 mb-3 flex items-start gap-3 text-[12.5px] text-muted leading-relaxed">
          <Icon name="database" size={16} />
          <div>
            A dataset is a persistent, spreadsheet-like table that lives across runs and workflows. Capture a run's
            results into one (<span className="text-fg">append</span>, with dedup), edit cells, filter, dedup, then{" "}
            <span className="text-fg">project</span> a few columns into a tidy input for the next workflow, or{" "}
            <span className="text-fg">merge</span> several together. CSV is just for import/export — inside the app
            everything is live SQL.
          </div>
        </div>

        {creating && (
          <div className="card p-4 mb-4 flex items-center gap-2">
            <input autoFocus className="input" placeholder="Dataset name — e.g. LinkedIn leads"
                   value={newName} onChange={(e) => setNewName(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter") create(); if (e.key === "Escape") { setCreating(false); setNewName(""); } }} />
            <button className="btn btn-primary" onClick={create} disabled={!newName.trim()}>Create</button>
            <button className="btn btn-secondary" onClick={() => { setCreating(false); setNewName(""); }}>Cancel</button>
          </div>
        )}

        {error && <div className="text-[12px] text-danger mb-4">{error}</div>}

        <div className="flex flex-col gap-2.5">
          {datasets.map((d) => (
            <div key={d.id} className="card card-hover p-4 flex items-center gap-4">
              <button
                onClick={() => toggle(d.id)}
                className="w-5 h-5 rounded-md shrink-0 flex items-center justify-center"
                style={{ border: `1px solid ${selected.has(d.id) ? "#0072f5" : "var(--color-line-strong)"}`,
                         background: selected.has(d.id) ? "#0072f5" : "transparent" }}
                title="Select for merge"
              >
                {selected.has(d.id) && <Icon name="check" size={13} />}
              </button>
              <span className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0" style={{ background: "#0072f518", color: "#3b9eff" }}>
                <Icon name="database" size={16} />
              </span>
              <Link to={`/data/${d.id}`} className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-[13.5px] font-medium truncate">{d.name}</span>
                  {d.source && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded text-faint" style={{ background: "#161616" }}>
                      {SOURCE_LABEL[d.source.kind] ?? d.source.kind}
                    </span>
                  )}
                </div>
                <div className="text-[11px] text-faint mono mt-0.5 truncate">
                  {d.rowCount ?? 0} rows · {d.columns.length} cols
                  {d.dedupKeys.length > 0 && ` · key: ${d.dedupKeys.join(", ")}`}
                  {" · "}updated {timeAgo(d.updatedAt)}
                </div>
              </Link>
              <button className="btn btn-secondary btn-sm shrink-0" onClick={() => del(d.id)} title="Delete dataset">
                <Icon name="trash" size={13} />
              </button>
            </div>
          ))}
          {datasets.length === 0 && (
            <div className="card p-10 text-center text-[13px] text-faint flex flex-col items-center gap-2">
              <Icon name="database" size={22} />
              No datasets yet. Create one, import a CSV, or capture a run's results from its page.
            </div>
          )}
        </div>
      </div>
    </>
  );
}
