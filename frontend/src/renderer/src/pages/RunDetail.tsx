import { Link, useParams } from "react-router-dom";
import { useCallback, useEffect, useRef, useState } from "react";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { StatusPill } from "@/components/StatusPill";
import { ProgressBar } from "@/components/ProgressBar";
import { LogView } from "@/components/LogView";
import { CsvTable } from "@/components/CsvTable";
import { RunControls } from "@/components/RunControls";
import { jget, duration, timeAgo } from "@/lib/client";
import type { Run } from "@/lib/types";

export default function RunDetail() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<Run | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const liveRef = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const d = await jget<{ run: Run; logs: string[] }>(`/api/runs/${id}`);
      setRun(d.run);
      setLogs(d.logs);
      liveRef.current = !["succeeded", "canceled"].includes(d.run.status) || d.run.browserOpen;
      if (d.run.status === "failed" && !d.run.browserOpen) liveRef.current = false;
    } catch {}
  }, [id]);

  useEffect(() => {
    let stop = false;
    const loop = async () => {
      if (stop || document.visibilityState !== "visible") return;
      await refresh();
      if (!stop) setTimeout(loop, liveRef.current ? 1200 : 5000);
    };
    loop();
    const onVis = () => { if (document.visibilityState === "visible") loop(); };
    document.addEventListener("visibilitychange", onVis);
    return () => { stop = true; document.removeEventListener("visibilitychange", onVis); };
  }, [refresh]);

  if (!run) return (<><Header title="…" /><div className="px-7 py-6 text-faint text-[13px]">Loading run…</div></>);

  const active = run.status === "running" || run.status === "starting";
  const showResults = run.rows != null || run.status === "succeeded" || run.status === "controlled" || run.status === "failed";

  return (
    <>
      <Header
        title={
          <span className="flex items-center gap-2">
            <Link to="/runs" className="text-muted hover:text-fg">Runs</Link>
            <Icon name="chevronRight" size={14} />
            <span className="mono text-[13px]">{run.id}</span>
          </span>
        }
        actions={<RunControls run={run} onChange={refresh} />}
      />
      <div className="px-7 py-6 max-w-[1100px]">
        <div className="card p-5 mb-5">
          <div className="flex items-center gap-3 mb-4 flex-wrap">
            <StatusPill status={run.status} />
            <span className="text-[14px] font-semibold">{run.workflowName}</span>
            <span className="text-[12px] text-faint mono ml-auto">
              {active ? `running ${duration(run.startedAt)}` : `${duration(run.startedAt, run.finishedAt)} · ${timeAgo(run.createdAt)}`}
            </span>
          </div>

          <div className="flex flex-wrap gap-2 mb-4">
            {Object.entries(run.params).map(([k, v]) => (
              <span key={k} className="text-[11.5px] px-2 py-1 rounded-md mono" style={{ background: "#161616", border: "1px solid var(--color-line)" }}>
                <span className="text-faint">{k}:</span> {String(v)}
              </span>
            ))}
          </div>

          <ProgressBar progress={run.progress ?? undefined} active={active} />

          {run.browserOpen && (
            <div className="mt-4 flex items-center gap-2 text-[12px] rounded-lg px-3 py-2" style={{ background: "#f5a62312", color: "#f5a623" }}>
              <Icon name={run.watch ? "eye" : "eyeOff"} size={14} />
              Browser session live on :{run.serverPort} ({run.watch ? "visible window" : "headless"})
              {run.status === "controlled" && " — paused, you're driving it"}
            </div>
          )}

          {run.error && (
            <div className="mt-4 text-[12.5px] rounded-lg px-3 py-2.5" style={{ background: "#ff5c5c12", color: "#ff8d8d", border: "1px solid #4a2424" }}>
              <span className="font-medium">Error:</span> {run.error}
            </div>
          )}

          {run.lastUrl && (
            <a href={run.lastUrl} target="_blank" rel="noreferrer" className="mt-3 inline-flex items-center gap-1.5 text-[12px] text-running hover:underline break-all">
              <Icon name="external" size={13} /> {run.lastUrl}
            </a>
          )}
        </div>

        <div className="mb-6">
          <div className="flex items-center gap-2 mb-2.5 text-[13px] font-medium"><Icon name="terminal" size={15} /> Live output</div>
          <LogView lines={logs} />
        </div>

        {showResults && (
          <div>
            <div className="flex items-center gap-2 mb-2.5 text-[13px] font-medium"><Icon name="layers" size={15} /> Results</div>
            <CsvTable runId={run.id} />
          </div>
        )}
      </div>
    </>
  );
}
