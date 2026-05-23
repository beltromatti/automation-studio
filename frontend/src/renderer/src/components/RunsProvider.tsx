
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { jget } from "@/lib/client";
import type { Run, Settings } from "@/lib/types";

interface RunsCtx { runs: Run[]; settings: Settings; refresh: () => void }
const Ctx = createContext<RunsCtx>({ runs: [], settings: { maxConcurrency: 1 }, refresh: () => {} });
export const useRuns = () => useContext(Ctx);

const ACTIVE = ["running", "starting", "controlled"];

// One shared poller for the whole app (Sidebar + all pages read from here),
// so we don't hit /api/runs from three components at once. It backs off when
// nothing is running and pauses entirely while the tab is hidden — no idle spam.
export function RunsProvider({ children }: { children: React.ReactNode }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [settings, setSettings] = useState<Settings>({ maxConcurrency: 1 });
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stopped = useRef(false);

  const schedule = useCallback((ms: number) => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(tick, ms);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const tick = useCallback(async () => {
    if (stopped.current || document.visibilityState !== "visible") return;
    let delay = 6000;
    try {
      const d = await jget<{ runs: Run[]; settings: Settings }>("/api/runs");
      setRuns(d.runs);
      setSettings(d.settings);
      if (d.runs.some((r) => ACTIVE.includes(r.status))) delay = 2000;
    } catch {}
    if (!stopped.current && document.visibilityState === "visible") schedule(delay);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schedule]);

  useEffect(() => {
    stopped.current = false;
    tick();
    const onVis = () => { if (document.visibilityState === "visible") tick(); };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      stopped.current = true;
      if (timer.current) clearTimeout(timer.current);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [tick]);

  const refresh = useCallback(() => tick(), [tick]);
  return <Ctx.Provider value={{ runs, settings, refresh }}>{children}</Ctx.Provider>;
}
