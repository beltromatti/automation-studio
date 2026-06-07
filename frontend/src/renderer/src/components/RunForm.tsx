
import { Link, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { Icon } from "./Icon";
import { useChromeGate } from "./DependencyModal";
import { jget, jpost } from "@/lib/client";
import type { Dataset, Profile, PublicWorkflow, Run } from "@/lib/types";
import { FilePickerInput } from "./FilePickerInput";

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
  const [timeoutMin, setTimeoutMin] = useState(Math.max(1, Math.round((workflow.timeoutSec ?? 3600) / 60)));
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
        inputDatasetId: inputDatasetId || undefined, inSeconds: scheduleMin > 0 ? scheduleMin * 60 : undefined,
        timeoutSec: Math.max(30, Math.round(timeoutMin * 60)) });
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

        <label className="flex items-center gap-2 text-[13px] text-muted">
          <Icon name="clock" size={14} />
          <span>Timeout:</span>
          <input type="number" min={1} step={1} value={timeoutMin}
                 onChange={(e) => setTimeoutMin(Math.max(1, Number(e.target.value) || 1))}
                 className="input" style={{ width: 82, height: 30 }} />
          <span className="text-faint">minutes</span>
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
