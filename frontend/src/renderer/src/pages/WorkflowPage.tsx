import { Link, useParams } from "react-router-dom";
import { useEffect, useState } from "react";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { RunForm } from "@/components/RunForm";
import { RunRow } from "@/components/RunRow";
import { jget } from "@/lib/client";
import { useRuns } from "@/components/RunsProvider";
import type { PublicWorkflow } from "@/lib/types";

export default function WorkflowPage() {
  const { id } = useParams<{ id: string }>();
  const [workflow, setWorkflow] = useState<PublicWorkflow | null>(null);
  const { runs: allRuns } = useRuns();
  const runs = allRuns.filter((r) => r.workflowId === id);

  useEffect(() => {
    jget<{ workflows: PublicWorkflow[] }>("/api/workflows")
      .then((d) => setWorkflow(d.workflows.find((w) => w.id === id) ?? null))
      .catch(() => {});
  }, [id]);

  if (!workflow) {
    return (<><Header title="…" /><div className="px-7 py-6 text-faint text-[13px]">Loading…</div></>);
  }

  return (
    <>
      <Header
        title={
          <span className="flex items-center gap-2">
            <Link to="/" className="text-muted hover:text-fg">Workflows</Link>
            <Icon name="chevronRight" size={14} />
            {workflow.name}
          </span>
        }
        sub={workflow.module}
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
    </>
  );
}
