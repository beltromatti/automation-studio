import { useCallback, useEffect, useRef, useState } from "react";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { SessionRow } from "@/components/SessionRow";
import { jget } from "@/lib/client";
import type { AgentSession } from "@/lib/types";

const ACTIVE = ["running", "starting", "queued"];

export default function AgentSessionsPage() {
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const stopped = useRef(false);

  const load = useCallback(async () => {
    const d = await jget<{ sessions: AgentSession[] }>("/api/agents/sessions").catch(() => ({ sessions: [] }));
    if (!stopped.current) setSessions(d.sessions);
  }, []);

  useEffect(() => {
    stopped.current = false;
    load();
    const t = setInterval(load, 3000);
    return () => { stopped.current = true; clearInterval(t); };
  }, [load]);

  const active = sessions.filter((s) => ACTIVE.includes(s.status)).length;
  const failed = sessions.filter((s) => s.status === "failed").length;

  return (
    <>
      <Header
        title="Sessions"
        sub="Agent runs — live transcripts you can watch, steer and continue any time"
        actions={
          <div className="flex items-center gap-3 text-[12px] text-muted">
            {active > 0 && (
              <span className="inline-flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full" style={{ background: "#3b9eff" }} /> {active} active
              </span>
            )}
            {failed > 0 && <span style={{ color: "#ff8d8d" }}>{failed} failed</span>}
            <span className="text-faint mono">{sessions.length} total</span>
          </div>
        }
      />
      <div className="px-7 py-6 max-w-[900px]">
        <div className="flex flex-col gap-2">
          {sessions.map((s) => <SessionRow key={s.id} session={s} />)}
          {sessions.length === 0 && (
            <div className="card p-10 text-center text-[13px] text-faint flex flex-col items-center gap-2">
              <Icon name="terminal" size={22} />
              No agent sessions yet — launch an agent.
            </div>
          )}
        </div>
      </div>
    </>
  );
}
