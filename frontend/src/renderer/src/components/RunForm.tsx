
import { useNavigate } from "react-router-dom";
import { useState } from "react";
import { Icon } from "./Icon";
import { jpost } from "@/lib/client";
import type { PublicWorkflow, Run } from "@/lib/types";

export function RunForm({ workflow }: { workflow: PublicWorkflow }) {
  const navigate = useNavigate();
  const [values, setValues] = useState<Record<string, unknown>>(() => {
    const v: Record<string, unknown> = {};
    for (const p of workflow.params) v[p.name] = p.default ?? (p.type === "boolean" ? false : "");
    return v;
  });
  const [watch, setWatch] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const { run } = await jpost<{ run: Run }>("/api/runs", { workflowId: workflow.id, params: values, watch });
      navigate(`/runs/${run.id}`);
    } catch (e) {
      setError(String((e as Error).message));
      setBusy(false);
    }
  };

  return (
    <div className="card p-5 max-w-[560px]">
      <div className="flex flex-col gap-4">
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
            ) : (
              <input
                className="input"
                type={p.type === "number" ? "number" : "text"}
                placeholder={p.placeholder}
                value={String(values[p.name] ?? "")}
                onChange={(e) =>
                  setValues((v) => ({ ...v, [p.name]: p.type === "number" ? Number(e.target.value) : e.target.value }))
                }
                onKeyDown={(e) => e.key === "Enter" && submit()}
              />
            )}
            {p.help && <span className="text-[11px] text-faint">{p.help}</span>}
          </div>
        ))}

        <label className="flex items-center gap-2.5 mt-1 cursor-pointer select-none" onClick={() => setWatch((w) => !w)}>
          <span className="w-9 h-5 rounded-full relative transition-colors" style={{ background: watch ? "#0072f5" : "#2a2a2a" }}>
            <span className="absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all" style={{ left: watch ? 18 : 2 }} />
          </span>
          <span className="text-[13px]">Watch live <span className="text-faint">— open the browser window during the run</span></span>
        </label>

        {error && <div className="text-[12px] text-danger">{error}</div>}

        <button onClick={submit} disabled={busy} className="btn btn-primary mt-1 self-start">
          <Icon name="play" size={14} /> {busy ? "Starting…" : "Run workflow"}
        </button>
      </div>
    </div>
  );
}
