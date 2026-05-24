import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { RunRow } from "@/components/RunRow";
import { useRuns } from "@/components/RunsProvider";

const ACTIVE = ["running", "starting", "controlled"];

export default function RunsPage() {
  const { runs } = useRuns();
  const active = runs.filter((r) => ACTIVE.includes(r.status)).length;
  const queued = runs.filter((r) => r.status === "queued").length;

  // No concurrency knob: runs on the same persistent profile go one at a time
  // (the rest queue), and everything else runs in parallel automatically.
  return (
    <>
      <Header
        title="Runs"
        sub={`${runs.length} total`}
        actions={
          <div className="flex items-center gap-3 text-[12px] text-muted">
            {active > 0 && (
              <span className="inline-flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full" style={{ background: "#3b9eff" }} /> {active} active
              </span>
            )}
            {queued > 0 && <span className="text-faint">{queued} queued</span>}
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
