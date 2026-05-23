import { Link } from "react-router-dom";
import { StatusPill } from "./StatusPill";
import { timeAgo, duration } from "@/lib/client";
import type { Run } from "@/lib/types";

export function RunRow({ run }: { run: Run }) {
  const q = run.params?.query ? String(run.params.query) : "";
  return (
    <Link to={`/runs/${run.id}`} className="card card-hover flex items-center gap-4 px-4 py-3">
      <StatusPill status={run.status} size="sm" />
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium truncate">
          {run.workflowName}
          {q && <span className="text-muted font-normal"> · {q}</span>}
        </div>
        <div className="text-[11px] text-faint mono truncate">
          {run.id} · {timeAgo(run.createdAt)}
          {run.rows != null && ` · ${run.rows} rows`}
        </div>
      </div>
      {run.progress && (run.status === "running" || run.status === "starting") && (
        <div className="text-[11px] mono text-faint shrink-0">
          {run.progress.collected}/{run.progress.total}
        </div>
      )}
      <div className="text-[11px] text-faint shrink-0 w-14 text-right">{duration(run.startedAt, run.finishedAt)}</div>
      {run.watch && run.browserOpen && (
        <span className="text-[10px] px-1.5 py-0.5 rounded shrink-0" style={{ background: "#f5a62320", color: "#f5a623" }}>
          visible
        </span>
      )}
    </Link>
  );
}
