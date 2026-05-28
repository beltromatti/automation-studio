
import { Link, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { Icon } from "./Icon";
import { useChromeGate } from "./DependencyModal";
import { jget, jpost, jpostForm } from "@/lib/client";
import type { Dataset, FileList as Files, FileRecord, Profile, PublicWorkflow, Run } from "@/lib/types";
import { FileThumb, fileIcon } from "./FilePreview";

export function RunForm({ workflow }: { workflow: PublicWorkflow }) {
  const navigate = useNavigate();
  const { guard, modal } = useChromeGate();
  const [values, setValues] = useState<Record<string, unknown>>(() => {
    const v: Record<string, unknown> = {};
    for (const p of workflow.params) v[p.name] = p.default ?? (p.type === "boolean" ? false : "");
    return v;
  });
  const [watch, setWatch] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [profiles, setProfiles] = useState<Profile[]>([]);
  // Workflows that use a login default to the profile they were built around (the
  // workflow's declared `profileName`, falling back to "default" for any older auth
  // workflow without one); everything else starts on the throwaway Ephemeral profile.
  const [profileId, setProfileId] = useState<string>(
    workflow.profileName || (workflow.needsAuth ? "default" : "ephemeral"),
  );
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [dest, setDest] = useState<string>(""); // "" none | dataset id | "__new__"
  const [newDsName, setNewDsName] = useState(`${workflow.name} results`);
  const [inputDatasetId, setInputDatasetId] = useState<string>("");
  const [scheduleMin, setScheduleMin] = useState(0); // 0 = run now; else minutes from now
  const consumesInput = (workflow.inputContract ?? []).length > 0;

  useEffect(() => {
    jget<{ profiles: Profile[] }>("/api/profiles")
      .then((d) => {
        setProfiles(d.profiles);
        setProfileId((cur) =>
          cur === "ephemeral" || d.profiles.some((p) => p.id === cur)
            ? cur
            : d.profiles[0]?.id ?? "ephemeral",
        );
      })
      .catch(() => {});
    jget<{ datasets: Dataset[] }>("/api/datasets").then((d) => setDatasets(d.datasets)).catch(() => {});
  }, []);

  const selected = profiles.find((p) => p.id === profileId);

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      let datasetId: string | undefined;
      if (dest === "__new__") {
        const { dataset } = await jpost<{ dataset: Dataset }>("/api/datasets", {
          name: newDsName.trim() || `${workflow.name} results`,
          columns: workflow.outputContract ?? [],
        });
        datasetId = dataset.id;
      } else if (dest) {
        datasetId = dest;
      }
      const { run } = await jpost<{ run: Run }>("/api/runs", { workflowId: workflow.id, params: values, watch, profileId, datasetId,
        inputDatasetId: inputDatasetId || undefined, inSeconds: scheduleMin > 0 ? scheduleMin * 60 : undefined });
      navigate(`/runs/${run.id}`);
    } catch (e) {
      setError(String((e as Error).message));
      setBusy(false);
    }
  };

  return (
    <div className="card p-5 max-w-[560px]">
      <div className="flex flex-col gap-4">
        {consumesInput && (
          <div className="flex flex-col gap-1.5">
            <label className="label">Input dataset<span className="text-danger"> *</span> <span className="text-faint">— rows fed to this workflow ({(workflow.inputContract ?? []).map((c) => c.name).join(", ")})</span></label>
            <div className="relative flex items-center">
              <span className="absolute left-3 pointer-events-none text-faint"><Icon name="database" size={14} /></span>
              <select className="input appearance-none" style={{ paddingLeft: 32 }} value={inputDatasetId} onChange={(e) => setInputDatasetId(e.target.value)}>
                <option value="">— pick a dataset to process —</option>
                {datasets.map((d) => <option key={d.id} value={d.id}>{d.name} ({d.rowCount ?? 0} rows)</option>)}
              </select>
              <Icon name="chevronRight" size={14} className="absolute right-3 rotate-90 pointer-events-none text-faint" />
            </div>
            <span className="text-[11px] text-faint">This workflow runs over each row of the chosen dataset. Build one by projecting another workflow's output (Data → a dataset → Project).</span>
          </div>
        )}
        {workflow.params.map((p) => (
          <div key={p.name} className="flex flex-col gap-1.5">
            <label className="label">{p.label}{p.required && <span className="text-danger"> *</span>}</label>
            {p.type === "boolean" ? (
              <button
                onClick={() => setValues((v) => ({ ...v, [p.name]: !v[p.name] }))}
                className="btn btn-secondary self-start"
              >
                {values[p.name] ? "On" : "Off"}
              </button>
            ) : p.type === "select" ? (
              <select
                className="input appearance-none"
                value={String(values[p.name] ?? "")}
                onChange={(e) => setValues((v) => ({ ...v, [p.name]: e.target.value }))}
              >
                {(p.options ?? []).map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            ) : p.type === "file" ? (
              <FilePickerInput
                value={String(values[p.name] ?? "")}
                onChange={(id) => setValues((v) => ({ ...v, [p.name]: id }))}
              />
            ) : p.type === "file_list" ? (
              <FilePickerInput
                multiple
                value={String(values[p.name] ?? "")}
                onChange={(ids) => setValues((v) => ({ ...v, [p.name]: ids }))}
              />
            ) : (
              <input
                className="input"
                type={p.type === "number" ? "number" : "text"}
                placeholder={p.placeholder}
                value={String(values[p.name] ?? "")}
                onChange={(e) =>
                  setValues((v) => ({ ...v, [p.name]: p.type === "number" ? Number(e.target.value) : e.target.value }))
                }
                onKeyDown={(e) => e.key === "Enter" && guard(submit)}
              />
            )}
            {p.help && <span className="text-[11px] text-faint">{p.help}</span>}
          </div>
        ))}

        <div className="flex flex-col gap-1.5">
          <label className="label">Browser profile</label>
          <div className="relative flex items-center">
            <span
              className="absolute left-3 w-2.5 h-2.5 rounded-full pointer-events-none"
              style={{ background: selected?.color ?? "#3a3a3a" }}
            />
            <select
              className="input appearance-none"
              style={{ paddingLeft: 30 }}
              value={profileId}
              onChange={(e) => setProfileId(e.target.value)}
            >
              <option value="ephemeral">Ephemeral — fresh throwaway, no saved login</option>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                  {p.open ? " — window open" : ""}
                </option>
              ))}
            </select>
            <Icon name="chevronRight" size={14} className="absolute right-3 rotate-90 pointer-events-none text-faint" />
          </div>
          <span className="text-[11px] text-faint">
            {profileId === "ephemeral" ? (
              "A new throwaway profile is spawned for the run and deleted after — no login, nothing kept. Ephemeral runs always run in parallel."
            ) : (
              <>
                Uses {selected?.name ?? "this profile"}'s saved logins & cookies, which keep building up over time.{" "}
                <Link to="/profiles" className="text-running hover:underline">Open it</Link> to log in or set things up.
                Runs on the same profile run one at a time (others queue).
              </>
            )}
          </span>
        </div>

        <div className="flex flex-col gap-1.5">
          <label className="label">Append results to dataset <span className="text-faint">(optional)</span></label>
          <div className="relative flex items-center">
            <span className="absolute left-3 pointer-events-none text-faint"><Icon name="database" size={14} /></span>
            <select className="input appearance-none" style={{ paddingLeft: 32 }} value={dest} onChange={(e) => setDest(e.target.value)}>
              <option value="">— don't capture (results stay on the run) —</option>
              <option value="__new__">＋ New dataset…</option>
              {datasets.map((d) => (
                <option key={d.id} value={d.id}>{d.name} ({d.rowCount ?? 0} rows)</option>
              ))}
            </select>
            <Icon name="chevronRight" size={14} className="absolute right-3 rotate-90 pointer-events-none text-faint" />
          </div>
          {dest === "__new__" && (
            <input className="input mt-1" placeholder="New dataset name" value={newDsName} onChange={(e) => setNewDsName(e.target.value)} />
          )}
          {dest && (
            <span className="text-[11px] text-faint">
              On success the result is appended to {dest === "__new__" ? "a new dataset" : "the selected dataset"} (deduped by its key). Build up leads across runs, then project an input for the next workflow.
            </span>
          )}
        </div>

        <label className="flex items-center gap-2.5 mt-1 cursor-pointer select-none" onClick={() => setWatch((w) => !w)}>
          <span className="w-9 h-5 rounded-full relative transition-colors" style={{ background: watch ? "#0072f5" : "#2a2a2a" }}>
            <span className="absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all" style={{ left: watch ? 18 : 2 }} />
          </span>
          <span className="text-[13px]">Watch live <span className="text-faint">— open the browser window during the run</span></span>
        </label>

        <label className="flex items-center gap-2 text-[13px] text-muted">
          <Icon name="clock" size={14} />
          <span>Schedule:</span>
          <input type="number" min={0} step={1} value={scheduleMin}
                 onChange={(e) => setScheduleMin(Math.max(0, Number(e.target.value) || 0))}
                 className="input" style={{ width: 72, height: 30 }} />
          <span className="text-faint">{scheduleMin > 0 ? `minutes from now` : "minutes from now (0 = run now)"}</span>
        </label>

        {error && <div className="text-[12px] text-danger">{error}</div>}

        <button onClick={() => guard(submit)} disabled={busy || (consumesInput && !inputDatasetId)} className="btn btn-primary mt-1 self-start">
          <Icon name={scheduleMin > 0 ? "clock" : "play"} size={14} /> {busy ? "Starting…" : scheduleMin > 0 ? `Schedule in ${scheduleMin}m` : "Run workflow"}
        </button>
      </div>
      {modal}
    </div>
  );
}


// Picker for `file` / `file_list` workflow params. Single mode returns the file
// id as a string; multi mode returns a JSON array string. Sources files from
// the store and lets the user upload a new one inline (which becomes selected).
type FilePickerProps =
  | { multiple?: false; value: string; onChange: (id: string) => void }
  | { multiple: true;  value: string; onChange: (ids: string) => void };
function FilePickerInput(props: FilePickerProps) {
  const [files, setFiles] = useState<FileRecord[]>([]);
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const fileRef = useState<HTMLInputElement | null>(null);
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
      <div className="grid gap-1.5 max-h-44 overflow-y-auto"
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
