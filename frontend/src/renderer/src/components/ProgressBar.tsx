import type { Progress } from "@/lib/types";

export function ProgressBar({ progress, active }: { progress?: Progress; active: boolean }) {
  const total = progress?.total ?? 0;
  const collected = progress?.collected ?? 0;
  const pct = total > 0 ? Math.min(100, Math.round((collected / total) * 100)) : active ? 8 : 0;
  return (
    <div className="w-full">
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[12px] text-muted">{progress?.message || (active ? "working…" : "—")}</span>
        <span className="text-[12px] mono text-faint">{total > 0 ? `${collected}/${total}` : ""}</span>
      </div>
      <div className="h-1.5 w-full rounded-full overflow-hidden" style={{ background: "#1c1c1c" }}>
        <div
          className={active ? "transition-all duration-500" : ""}
          style={{
            width: `${pct}%`,
            height: "100%",
            background: active ? "var(--color-running)" : "var(--color-success)",
            boxShadow: active ? "0 0 10px var(--color-running)" : "none",
          }}
        />
      </div>
    </div>
  );
}
