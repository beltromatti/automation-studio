import type { RunStatus } from "@/lib/types";

const MAP: Record<RunStatus, { label: string; color: string; dot: string }> = {
  queued: { label: "Queued", color: "#a1a1a1", dot: "#6e6e6e" },
  starting: { label: "Starting", color: "#3b9eff", dot: "#3b9eff" },
  running: { label: "Running", color: "#3b9eff", dot: "#3b9eff" },
  succeeded: { label: "Succeeded", color: "#2bd576", dot: "#2bd576" },
  failed: { label: "Failed", color: "#ff5c5c", dot: "#ff5c5c" },
  canceled: { label: "Canceled", color: "#a1a1a1", dot: "#6e6e6e" },
  controlled: { label: "You're driving", color: "#f5a623", dot: "#f5a623" },
};

export function StatusPill({ status, size = "md" }: { status: RunStatus; size?: "sm" | "md" }) {
  const s = MAP[status] ?? MAP.queued;
  const pulse = status === "running" || status === "starting";
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border font-medium"
      style={{
        color: s.color,
        borderColor: s.color + "40",
        background: s.color + "14",
        fontSize: size === "sm" ? 11 : 12,
        padding: size === "sm" ? "2px 8px" : "3px 10px",
      }}
    >
      <span
        className={pulse ? "animate-pulse" : ""}
        style={{ width: 6, height: 6, borderRadius: 99, background: s.dot, display: "inline-block" }}
      />
      {s.label}
    </span>
  );
}
