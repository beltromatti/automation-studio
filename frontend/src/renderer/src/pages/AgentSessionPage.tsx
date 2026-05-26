import { Link, useParams } from "react-router-dom";
import { useCallback, useEffect, useRef, useState } from "react";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { jget, jpost, duration, timeAgo } from "@/lib/client";
import type { AgentEvent, AgentSession } from "@/lib/types";

const STATUS_COLOR: Record<string, string> = {
  starting: "#3b9eff", running: "#3b9eff", idle: "#f5a623",
  done: "#2bd576", failed: "#ff5c5c", canceled: "#6e6e6e",
};
const TERMINAL = ["done", "failed", "canceled"];

function unwrap(result?: string): string {
  if (!result) return "";
  try {
    const o = JSON.parse(result);
    if (o && Array.isArray(o.content) && o.content[0]?.text) return String(o.content[0].text);
    return result;
  } catch { return result; }
}

export default function AgentSessionPage() {
  const { id = "" } = useParams<{ id: string }>();
  const [s, setS] = useState<AgentSession | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [steer, setSteer] = useState("");
  const [busy, setBusy] = useState(false);
  const liveRef = useRef(true);
  const endRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async () => {
    try {
      const d = await jget<{ session: AgentSession; events: AgentEvent[] }>(`/api/agents/sessions/${id}`);
      setS(d.session); setEvents(d.events);
      liveRef.current = !TERMINAL.includes(d.session.status);
    } catch {}
  }, [id]);

  useEffect(() => {
    let stop = false;
    const loop = async () => {
      if (stop || document.visibilityState !== "visible") return;
      await refresh();
      const active = s?.status === "running" || s?.status === "starting";
      if (!stop) setTimeout(loop, active ? 1200 : liveRef.current ? 3000 : 8000);
    };
    loop();
    return () => { stop = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refresh]);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [events.length]);

  const sendSteer = async () => {
    const m = steer.trim();
    if (!m) return;
    setBusy(true);
    try { await jpost(`/api/agents/sessions/${id}/steer`, { message: m }); setSteer(""); await refresh(); }
    finally { setBusy(false); }
  };
  const stop = async () => { setBusy(true); try { await jpost(`/api/agents/sessions/${id}/stop`); await refresh(); } finally { setBusy(false); } };

  if (!s) return (<><Header title="…" /><div className="px-7 py-6 text-faint text-[13px]">Loading agent session…</div></>);

  const c = STATUS_COLOR[s.status] ?? "#6e6e6e";
  const active = s.status === "running" || s.status === "starting";
  const usage = s.usage as Record<string, number> | null;
  const cost = usage?.total_cost_usd;
  const inTok = usage?.input_tokens; const outTok = usage?.output_tokens;

  return (
    <>
      <Header
        title={<span className="flex items-center gap-2"><Link to="/agents" className="text-muted hover:text-fg">Agents</Link><Icon name="chevronRight" size={14} /><span className="font-semibold">{s.agentName}</span></span>}
        actions={
          <>
            {s.controlPort && <span className="text-[11px] px-2 py-1 rounded-md" style={{ background: "#f5a62312", color: "#f5a623" }}><Icon name="globe" size={12} /> browser :{s.controlPort}</span>}
            {!TERMINAL.includes(s.status) && <button className="btn btn-danger btn-sm" disabled={busy} onClick={stop}><Icon name="square" size={13} /> Stop</button>}
          </>
        }
      />
      <div className="px-7 py-6 max-w-[1000px]">
        <div className="card p-4 mb-4 flex items-center gap-3 flex-wrap">
          <span className="inline-flex items-center gap-1.5 text-[12px] px-2 py-0.5 rounded-md" style={{ background: `${c}1e`, color: c }}>
            <span className="w-1.5 h-1.5 rounded-full" style={{ background: c }} /> {s.status}
          </span>
          <span className="text-[11.5px] text-faint">{s.engine}</span>
          <span className="inline-flex items-center gap-1 text-[11.5px] px-2 py-0.5 rounded-md" style={{ background: "#161616" }}><Icon name="user" size={12} /> {s.profileName}</span>
          {s.scopes.map((sc) => <span key={sc} className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: "#0072f518", color: "#3b9eff" }}>{sc}</span>)}
          <span className="text-[11px] text-faint mono ml-auto">
            {s.turns} turn{s.turns !== 1 ? "s" : ""}
            {inTok != null && ` · ${(inTok / 1000).toFixed(1)}k in / ${((outTok ?? 0) / 1000).toFixed(1)}k out`}
            {cost != null && ` · $${cost.toFixed(4)}`}
            {` · ${active ? duration(s.startedAt) : timeAgo(s.createdAt)}`}
          </span>
        </div>

        {s.runIds.length > 0 && (
          <div className="flex items-center gap-2 mb-4 text-[12px] text-faint flex-wrap">
            <Icon name="terminal" size={13} /> launched runs:
            {s.runIds.map((r) => <Link key={r} to={`/runs/${r}`} className="mono text-running hover:underline">{r}</Link>)}
          </div>
        )}

        <div className="flex flex-col gap-2.5">
          {events.map((e, i) => <EventRow key={i} e={e} />)}
          <div ref={endRef} />
        </div>

        {!TERMINAL.includes(s.status) && (
          <div className="sticky bottom-0 mt-4 pt-3 bg-bg/90 backdrop-blur">
            <div className="flex items-end gap-2">
              <textarea className="input" style={{ height: 44, padding: "11px 12px", resize: "none" }}
                        placeholder={active ? "Steer the agent (runs after the current turn)…" : "Send a follow-up to continue this agent…"}
                        value={steer} onChange={(e) => setSteer(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendSteer(); } }} />
              <button className="btn btn-primary" disabled={busy || !steer.trim()} onClick={sendSteer}><Icon name="send" size={14} /> Send</button>
            </div>
          </div>
        )}
      </div>
    </>
  );
}

function EventRow({ e }: { e: AgentEvent }) {
  if (e.kind === "system" && e.role === "user") {
    return (
      <div className="self-end max-w-[80%] rounded-xl px-3.5 py-2 text-[12.5px]" style={{ background: "#0072f5", color: "#fff" }}>{e.text}</div>
    );
  }
  if (e.kind === "system") return <div className="text-[11px] text-faint flex items-center gap-1.5"><Icon name="dot" size={10} /> {e.text}</div>;
  if (e.kind === "message") return <div className="card p-3.5 text-[13px] leading-relaxed whitespace-pre-wrap">{e.text}</div>;
  if (e.kind === "reasoning") return <div className="text-[11.5px] text-faint italic px-1 whitespace-pre-wrap">{e.text}</div>;
  if (e.kind === "status") return <div className="text-[11px] text-faint flex items-center gap-1.5"><Icon name="clock" size={11} /> {e.status}{e.text ? ` — ${e.text}` : ""}</div>;
  if (e.kind === "usage") return null;
  if (e.kind === "error") return <div className="text-[12.5px] rounded-lg px-3 py-2" style={{ background: "#ff5c5c12", color: "#ff8d8d", border: "1px solid #4a2424" }}>{e.text}</div>;
  if (e.kind === "tool_call") {
    const isBrowser = (e.tool || "").startsWith("browser_");
    return (
      <div className="flex items-start gap-2 text-[12px]">
        <span className="shrink-0 mt-0.5" style={{ color: isBrowser ? "#f5a623" : "#3b9eff" }}><Icon name={isBrowser ? "globe" : "wand"} size={13} /></span>
        <div className="min-w-0">
          <span className="mono font-medium">{e.tool}</span>
          {e.args != null && Object.keys(e.args as object).length > 0 && (
            <span className="text-faint mono"> {JSON.stringify(e.args).slice(0, 160)}</span>
          )}
        </div>
      </div>
    );
  }
  if (e.kind === "tool_result") {
    const txt = unwrap(e.result);
    return (
      <details className="text-[11.5px] ml-5">
        <summary className="cursor-pointer select-none flex items-center gap-1.5" style={{ color: e.ok ? "#2bd576" : "#ff8d8d" }}>
          <Icon name={e.ok ? "check" : "alert"} size={12} /> {e.ok ? "result" : "error"} <span className="text-faint">({txt.length} chars)</span>
        </summary>
        <pre className="mono text-faint mt-1 whitespace-pre-wrap break-words" style={{ maxHeight: 200, overflow: "auto" }}>{txt}</pre>
      </details>
    );
  }
  return null;
}
