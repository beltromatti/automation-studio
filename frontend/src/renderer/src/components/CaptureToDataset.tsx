import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Icon } from "./Icon";
import { jget, jpost } from "@/lib/client";
import type { Dataset } from "@/lib/types";

// Capture a finished run's result CSV into a Dataset (new or existing). Mirrors the
// run-form's forward binding, for results you decide to keep after the fact.
export function CaptureToDataset({ runId, defaultName }: { runId: string; defaultName: string }) {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [dest, setDest] = useState<string>("__new__");
  const [name, setName] = useState(defaultName);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<{ id: string; inserted: number; skipped: number } | null>(null);
  const [error, setError] = useState("");

  useEffect(() => { jget<{ datasets: Dataset[] }>("/api/datasets").then((d) => setDatasets(d.datasets)).catch(() => {}); }, []);

  const save = async () => {
    setBusy(true); setError(""); setDone(null);
    try {
      const body = dest === "__new__" ? { name } : { datasetId: dest };
      const res = await jpost<{ datasetId: string; inserted: number; skipped: number }>(`/api/runs/${runId}/to-dataset`, body);
      setDone({ id: res.datasetId, inserted: res.inserted, skipped: res.skipped });
    } catch (e) { setError(String((e as Error).message)); }
    finally { setBusy(false); }
  };

  if (done) {
    return (
      <div className="flex items-center gap-2 text-[12px] text-success">
        <Icon name="check" size={14} /> +{done.inserted} rows ({done.skipped} dup skipped) →
        <Link to={`/data/${done.id}`} className="text-running hover:underline">open dataset</Link>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2">
      <select className="input appearance-none" style={{ height: 30, width: 220, paddingLeft: 10 }} value={dest} onChange={(e) => setDest(e.target.value)}>
        <option value="__new__">＋ New dataset…</option>
        {datasets.map((d) => <option key={d.id} value={d.id}>{d.name} ({d.rowCount ?? 0})</option>)}
      </select>
      {dest === "__new__" && (
        <input className="input" style={{ height: 30, width: 200 }} value={name} onChange={(e) => setName(e.target.value)} placeholder="Dataset name" />
      )}
      <button className="btn btn-secondary btn-sm" disabled={busy} onClick={save}>
        <Icon name="database" size={13} /> {busy ? "Saving…" : "Capture"}
      </button>
      {error && <span className="text-[11px] text-danger">{error}</span>}
    </div>
  );
}
