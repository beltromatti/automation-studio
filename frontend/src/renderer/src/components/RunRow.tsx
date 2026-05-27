import { Link } from "react-router-dom";
import { StatusPill } from "./StatusPill";
import { useRuns } from "./RunsProvider";
import { timeAgo, duration } from "@/lib/client";
import type { Run } from "@/lib/types";

const ACTIVE = ["running", "starting", "controlled"];
const isEphemeral = (id: string) => id === "ephemeral" || id === "temporary" || !id;

export function RunRow({ run }: { run: Run }) {
  const { runs } = useRuns();
  const q = run.params?.query ? String(run.params.query) : "";
  const active = ACTIVE.includes(run.status);
  // A persistent profile runs one run at a time; show when this one waits behind another.
  const waiting =
    run.status === "queued" &&
    !isEphemeral(run.profileId) &&
    runs.some((r) => r.id !== run.id && r.profileId === run.profileId && ACTIVE.includes(r.status));

  return (
    <Link to={`/runs/${run.id}`} className="card card-hover flex items-center gap-4 px-4 py-3">
      <StatusPill status={run.status} size="sm" />
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium truncate">
          {run.workflowName}
          {q && <span className="text-muted font-normal"> · {q}</span>}
        </div>
        <div className="text-[11px] text-faint mono truncate">
          {run.id}
          {run.startedAt && ` · took ${duration(run.startedAt, run.finishedAt)}`}
          {run.rows != null && ` · ${run.rows} rows`}
          {waiting && ` · waiting for ${run.profileName}`}
        </div>
      </div>
      {run.profileName && !isEphemeral(run.profileId) && (
        <span className="hidden sm:inline-flex items-center gap-1.5 text-[11px] text-muted shrink-0">
          <span className="w-2 h-2 rounded-full" style={{ background: "#0072f5" }} />
          {run.profileName}
        </span>
      )}
      {run.progress && (run.status === "running" || run.status === "starting") && (
        <div className="text-[11px] mono text-faint shrink-0">
          {run.progress.collected}/{run.progress.total}
        </div>
      )}
      <div className="text-[11px] text-faint shrink-0 w-20 text-right">{active ? duration(run.startedAt) : timeAgo(run.createdAt)}</div>
      {run.watch && run.browserOpen && (
        <span className="text-[10px] px-1.5 py-0.5 rounded shrink-0" style={{ background: "#f5a62320", color: "#f5a623" }}>
          visible
        </span>
      )}
    </Link>
  );
}
