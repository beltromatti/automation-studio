import { Link, useNavigate, useParams } from "react-router-dom";
import { useCallback, useEffect, useState } from "react";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { RunForm } from "@/components/RunForm";
import { RunRow } from "@/components/RunRow";
import { WorkflowEditor } from "@/components/WorkflowEditor";
import { jget, jdel } from "@/lib/client";
import { useRuns } from "@/components/RunsProvider";
import type { PublicWorkflow } from "@/lib/types";

export default function WorkflowPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [workflow, setWorkflow] = useState<PublicWorkflow | null>(null);
  const [editing, setEditing] = useState(false);
  const { runs: allRuns } = useRuns();
  const runs = allRuns.filter((r) => r.workflowId === id);

  const load = useCallback(() => {
    jget<{ workflows: PublicWorkflow[] }>("/api/workflows")
      .then((d) => setWorkflow(d.workflows.find((w) => w.id === id) ?? null))
      .catch(() => {});
  }, [id]);
  useEffect(() => { load(); }, [load]);

  const del = async () => {
    if (!workflow || workflow.builtin) return;
    if (!confirm(`Delete workflow "${workflow.name}"?`)) return;
    await jdel(`/api/workflows/${workflow.id}`).catch(() => {});
    navigate("/");
  };

  if (!workflow) {
    return (<><Header title="…" /><div className="px-7 py-6 text-faint text-[13px]">Loading…</div></>);
  }

  return (
    <>
      <Header
        title={
          <span className="flex items-center gap-2">
            <Link to="/workflows" className="text-muted hover:text-fg">Workflows</Link>
            <Icon name="chevronRight" size={14} />
            {workflow.name}
            {!workflow.builtin && <span className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: "#0072f518", color: "#3b9eff" }}>{workflow.createdBy === "agent" ? "agent-made" : "custom"}</span>}
          </span>
        }
        sub={workflow.builtin ? workflow.module : `custom · ${workflow.module}`}
        actions={
          <>
            <button className="btn btn-secondary btn-sm" onClick={() => setEditing(true)}><Icon name="pencil" size={13} /> {workflow.builtin ? "Duplicate" : "Edit"}</button>
            {!workflow.builtin && <button className="btn btn-secondary btn-sm" onClick={del}><Icon name="trash" size={13} /></button>}
          </>
        }
      />
      <div className="px-7 py-6 max-w-[1000px]">
        <p className="text-[13px] text-muted max-w-[620px] leading-relaxed mb-5">{workflow.description}</p>
        <RunForm workflow={workflow} />

        {runs.length > 0 && (
          <div className="mt-9">
            <h2 className="text-[14px] font-semibold mb-3">History</h2>
            <div className="flex flex-col gap-2 max-w-[720px]">
              {runs.slice(0, 12).map((r) => <RunRow key={r.id} run={r} />)}
            </div>
          </div>
        )}
      </div>
      {editing && <WorkflowEditor workflow={workflow} onClose={() => setEditing(false)} onSaved={(nid) => { setEditing(false); if (nid && nid !== workflow.id) navigate(`/workflows/${nid}`); else load(); }} />}
    </>
  );
}
