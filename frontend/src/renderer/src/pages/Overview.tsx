import { Link } from "react-router-dom";
import { useCallback, useEffect, useState } from "react";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { RunRow } from "@/components/RunRow";
import { WorkflowEditor } from "@/components/WorkflowEditor";
import { jget, jdel } from "@/lib/client";
import { useRuns } from "@/components/RunsProvider";
import type { PublicWorkflow } from "@/lib/types";

export default function Overview() {
  const [workflows, setWorkflows] = useState<PublicWorkflow[]>([]);
  const [editing, setEditing] = useState<PublicWorkflow | "new" | null>(null);
  const { runs } = useRuns();

  const load = useCallback(() => {
    jget<{ workflows: PublicWorkflow[] }>("/api/workflows").then((d) => setWorkflows(d.workflows)).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  return (
    <>
      <Header title="Workflows" sub="Ready-to-run automations — built-in or your own"
        actions={<button className="btn btn-primary btn-sm" onClick={() => setEditing("new")}><Icon name="plus" size={14} /> New workflow</button>} />
      <div className="px-7 py-6 max-w-[1100px]">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {workflows.map((w) => {
            const last = runs.find((r) => r.workflowId === w.id);
            return (
              <div key={w.id} className="card card-hover p-5 flex flex-col">
                <div className="flex items-start gap-3">
                  <div className="w-10 h-10 rounded-lg flex items-center justify-center shrink-0" style={{ background: "#161616", border: "1px solid var(--color-line)" }}>
                    <Icon name={w.icon} size={19} />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <h3 className="text-[15px] font-semibold">{w.name}</h3>
                      {w.builtin ? (
                        <span className="text-[10px] px-1.5 py-0.5 rounded text-faint" style={{ background: "#161616" }}>built-in</span>
                      ) : (
                        <span className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: "#0072f518", color: "#3b9eff" }}>{w.createdBy === "agent" ? "agent-made" : "custom"}</span>
                      )}
                      {w.needsAuth && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: "#f5a62318", color: "#f5a623" }}>uses login</span>
                      )}
                    </div>
                    <p className="text-[12.5px] text-muted mt-1 leading-relaxed">{w.description}</p>
                  </div>
                </div>
                <div className="flex items-center justify-between mt-4 pt-4 border-t" style={{ borderColor: "var(--color-line)" }}>
                  <span className="text-[11px] text-faint mono">{last ? `last run ${last.status}` : "never run"}</span>
                  <div className="flex items-center gap-1.5">
                    <button className="btn btn-secondary btn-sm" title={w.builtin ? "Duplicate (built-ins can't be edited in place)" : "Edit"} onClick={() => setEditing(w)}><Icon name="pencil" size={13} /></button>
                    {!w.builtin && (
                      <button className="btn btn-secondary btn-sm" title="Delete" onClick={async () => { if (confirm(`Delete workflow "${w.name}"?`)) { await jdel(`/api/workflows/${w.id}`); load(); } }}><Icon name="trash" size={13} /></button>
                    )}
                    <Link to={`/workflows/${w.id}`} className="btn btn-primary btn-sm"><Icon name="play" size={13} /> Run</Link>
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        <div className="mt-9">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-[14px] font-semibold">Recent runs</h2>
            <Link to="/runs" className="text-[12px] text-muted hover:text-fg flex items-center gap-1">
              View all <Icon name="chevronRight" size={13} />
            </Link>
          </div>
          <div className="flex flex-col gap-2">
            {runs.slice(0, 6).map((r) => <RunRow key={r.id} run={r} />)}
            {runs.length === 0 && <div className="card p-8 text-center text-[13px] text-faint">No runs yet — pick a workflow above.</div>}
          </div>
        </div>
      </div>
      {editing && <WorkflowEditor workflow={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />}
    </>
  );
}
