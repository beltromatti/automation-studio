
import { useState } from "react";
import { Icon } from "./Icon";
import { jpost } from "@/lib/client";
import type { Run } from "@/lib/types";

export function RunControls({ run, onChange }: { run: Run; onChange: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState("");

  const act = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key); setErr("");
    try { await fn(); onChange(); } catch (e) { setErr(String((e as Error).message)); }
    finally { setBusy(null); }
  };
  const control = (action: string) => act(action, () => jpost(`/api/runs/${run.id}/control`, { action }));
  const cancel = () => act("cancel", () => jpost(`/api/runs/${run.id}/cancel`));

  const isActive = run.status === "running" || run.status === "starting";

  return (
    <div className="flex flex-wrap items-center gap-2">
      {isActive && (
        <>
          <button className="btn btn-secondary btn-sm" disabled={!run.browserOpen || !!busy} onClick={() => control("takeover")} title="Pause the automation and open the real browser window for you to drive">
            <Icon name="hand" size={14} /> {busy === "takeover" ? "…" : "Take control"}
          </button>
          <button className="btn btn-danger btn-sm" disabled={!!busy} onClick={cancel}>
            <Icon name="stop" size={13} /> {busy === "cancel" ? "…" : "Cancel"}
          </button>
        </>
      )}

      {run.browserOpen && !isActive && (
        <>
          {run.watch ? (
            <button className="btn btn-secondary btn-sm" disabled={!!busy} onClick={() => control("hide")}>
              <Icon name="eyeOff" size={14} /> Hide window
            </button>
          ) : (
            <button className="btn btn-secondary btn-sm" disabled={!!busy} onClick={() => control("show")}>
              <Icon name="eye" size={14} /> Show window
            </button>
          )}
          <button className="btn btn-secondary btn-sm" disabled={!!busy} onClick={() => control("resume")} title="Unpause the automation / browser">
            <Icon name="play" size={13} /> Resume
          </button>
          <button className="btn btn-danger btn-sm" disabled={!!busy} onClick={() => control("close")}>
            <Icon name="x" size={14} /> Close browser
          </button>
        </>
      )}

      {err && <span className="text-[12px] text-danger">{err}</span>}
    </div>
  );
}
