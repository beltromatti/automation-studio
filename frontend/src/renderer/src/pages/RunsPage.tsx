import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { RunRow } from "@/components/RunRow";
import { jpost } from "@/lib/client";
import { useRuns } from "@/components/RunsProvider";
import type { Settings } from "@/lib/types";

export default function RunsPage() {
  const { runs, settings, refresh } = useRuns();

  const setConc = async (n: number) => {
    await jpost<Settings>("/api/settings", { maxConcurrency: n });
    refresh();
  };

  return (
    <>
      <Header
        title="Runs"
        sub={`${runs.length} total`}
        actions={
          <div className="flex items-center gap-2 text-[12px] text-muted">
            <span>Max concurrency</span>
            <div className="flex items-center card overflow-hidden" style={{ height: 32 }}>
              <button className="px-2.5 h-full hover:bg-elevated" onClick={() => setConc(settings.maxConcurrency - 1)}>−</button>
              <span className="px-2 mono text-fg">{settings.maxConcurrency}</span>
              <button className="px-2.5 h-full hover:bg-elevated" onClick={() => setConc(settings.maxConcurrency + 1)}>+</button>
            </div>
          </div>
        }
      />
      <div className="px-7 py-6 max-w-[900px]">
        <div className="flex flex-col gap-2">
          {runs.map((r) => <RunRow key={r.id} run={r} />)}
          {runs.length === 0 && (
            <div className="card p-10 text-center text-[13px] text-faint flex flex-col items-center gap-2">
              <Icon name="terminal" size={22} />
              No runs yet.
            </div>
          )}
        </div>
      </div>
    </>
  );
}
