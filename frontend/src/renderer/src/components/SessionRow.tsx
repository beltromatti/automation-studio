import { Link } from "react-router-dom";
import { Icon } from "./Icon";
import { timeAgo, duration } from "@/lib/client";
import type { AgentSession } from "@/lib/types";

const STATUS_COLOR: Record<string, string> = {
  starting: "#3b9eff", queued: "#9aa0a6", running: "#3b9eff", idle: "#f5a623",
  done: "#2bd576", failed: "#ff5c5c", canceled: "#6e6e6e",
};

export function SessionPill({ status }: { status: string }) {
  const c = STATUS_COLOR[status] ?? "#6e6e6e";
  const live = status === "running" || status === "starting";
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] px-2 py-0.5 rounded-md shrink-0" style={{ background: `${c}1e`, color: c }}>
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: c, animation: live ? "pulse 1.2s infinite" : undefined }} />
      {status}
    </span>
  );
}

// The session equivalent of RunRow — one agent session as a compact, clickable row.
export function SessionRow({ session: s }: { session: AgentSession }) {
  const active = s.status === "running" || s.status === "starting";
  return (
    <Link to={`/agents/sessions/${s.id}`} className="card card-hover flex items-center gap-4 px-4 py-3">
      <SessionPill status={s.status} />
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium truncate">
          {s.agentName}
          <span className="text-muted font-normal"> · {s.engine}</span>
        </div>
        <div className="text-[11px] text-faint mono truncate">
          {s.prompt || "—"}
          {s.turns > 1 && ` · ${s.turns} turns`}
        </div>
      </div>
      {s.controlPort ? (
        <span className="hidden sm:inline-flex items-center gap-1 text-[11px] shrink-0" style={{ color: "#f5a623" }}>
          <Icon name="globe" size={12} /> browser
        </span>
      ) : (
        <span className="hidden sm:inline-flex items-center gap-1.5 text-[11px] text-muted shrink-0">
          <span className="w-2 h-2 rounded-full" style={{ background: "#0072f5" }} /> {s.profileName}
        </span>
      )}
      <div className="text-[11px] text-faint shrink-0 w-20 text-right">
        {active ? duration(s.startedAt) : timeAgo(s.createdAt)}
      </div>
    </Link>
  );
}
