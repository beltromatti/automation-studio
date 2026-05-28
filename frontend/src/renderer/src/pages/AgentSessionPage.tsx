import { Link, useParams } from "react-router-dom";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { jget, jpost, duration, timeAgo, untilTime, BACKEND_URL } from "@/lib/client";
import type { AgentEvent, AgentSession } from "@/lib/types";

const STATUS_COLOR: Record<string, string> = {
  starting: "#3b9eff", queued: "#9aa0a6", running: "#3b9eff",
  waiting: "#f5a623", scheduled: "#9b8cff",
  done: "#2bd576", failed: "#ff5c5c", stopped: "#6e6e6e",
  // legacy fallback (older sessions persisted as "canceled" before the rename)
  canceled: "#6e6e6e",
};
const IN_FLIGHT = ["starting", "queued", "running"]; // a turn is running (or waiting for a profile)
const AT_REST = ["done", "failed", "stopped", "waiting", "scheduled"];

function unwrap(result?: string): string {
  if (!result) return "";
  try {
    const o = JSON.parse(result);
    if (o && Array.isArray(o.content) && o.content[0]?.text) return String(o.content[0].text);
    return result;
  } catch { return result; }
}

// Pre-compute, per event index: is this `message` the LAST message of its turn?
// If so, what was the turn's elapsed time (start = the user/wake system event that
// kicked the turn off; end = the next status event after this message). We mark
// only end-of-turn messages — mid-turn messages don't get the time pill.
type TurnInfo = { endOfTurn: boolean; elapsedSec?: number };
function computeTurnInfo(events: AgentEvent[]): Record<number, TurnInfo> {
  const out: Record<number, TurnInfo> = {};
  // walk forward, tracking the most recent turn-start timestamp
  let turnStartT: number | undefined;
  const isTurnStart = (e: AgentEvent) =>
    e.kind === "system" && (e as any).role === "user";
  const isRest = (e: AgentEvent) =>
    e.kind === "status" && AT_REST.includes(((e as any).status as string) || "");
  for (let i = 0; i < events.length; i++) {
    const e = events[i];
    if (isTurnStart(e)) turnStartT = (e as any).t;
    if (e.kind !== "message") continue;
    // is this the LAST message of its turn? look forward — if we hit another message
    // / tool_call / reasoning before a rest-status, it's NOT the last.
    let endOfTurn = false; let elapsed: number | undefined;
    for (let j = i + 1; j < events.length; j++) {
      const f = events[j];
      if (f.kind === "message" || f.kind === "tool_call" || f.kind === "reasoning") {
        break;
      }
      if (isRest(f)) {
        endOfTurn = true;
        if (turnStartT) elapsed = Math.max(0, ((f as any).t || 0) - turnStartT);
        break;
      }
    }
    out[i] = { endOfTurn, elapsedSec: elapsed };
  }
  return out;
}

export default function AgentSessionPage() {
  const { id = "" } = useParams<{ id: string }>();
  const [s, setS] = useState<AgentSession | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [steer, setSteer] = useState("");
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);   // follow the agent (auto-scroll) while pinned to bottom
  const [showJump, setShowJump] = useState(false);

  // Session metadata refresh (status/usage/pendingSteers/threadId/runIds). The
  // SSE stream is the authoritative source for events; metadata isn't carried by
  // events, so we refetch it on mount, on visibility/focus, and after mutations.
  // Note: we deliberately IGNORE the `events` field of this response — SSE replays
  // the full backlog on connect, and mixing both would duplicate every event.
  const refreshMeta = useCallback(async () => {
    try {
      const d = await jget<{ session: AgentSession }>(`/api/agents/sessions/${id}`);
      setS(d.session);
    } catch {}
  }, [id]);

  // ----- SSE: the chat streams live; no polling. The stream replays the backlog
  // on connect, emits an `event: ready` marker after replay, then keeps pushing
  // live events — so the UI never freezes when the window loses focus, and we
  // never miss an event on (re)connect.
  // IMPORTANT: must use the absolute backend URL (the renderer is served from
  // electron-vite in dev / file:// in prod — a bare /api path won't reach the
  // Python backend on its own port).
  useEffect(() => {
    let stopped = false;
    let es: EventSource | null = null;
    refreshMeta();
    const url = `${BACKEND_URL}/api/agents/sessions/${id}/events/stream`;
    const connect = () => {
      if (stopped) return;
      // Clear local events ONLY here (each (re)connect rebuilds from the replay,
      // never duplicating against a previous backlog).
      setEvents([]);
      try { es = new EventSource(url); }
      catch { return; }
      es.onmessage = (msg) => {
        try {
          const ev: AgentEvent = JSON.parse(msg.data);
          setEvents((arr) => [...arr, ev]);
          // status events: keep `s` in sync so the bar/labels update live
          if (ev.kind === "status" && (ev as any).status) {
            setS((curr) => (curr ? { ...curr, status: (ev as any).status } : curr));
          }
        } catch {}
      };
      es.onerror = () => {
        // EventSource auto-retries on transient errors; on a hard close we close
        // + reopen with a small delay so we don't hot-loop.
        try { es?.close(); } catch {}
        es = null;
        if (!stopped) setTimeout(connect, 1500);
      };
    };
    connect();
    // refresh metadata whenever the window regains focus (status/usage may have
    // moved while we were away — SSE handles events, this catches the rest)
    const onVis = () => { if (document.visibilityState === "visible") refreshMeta(); };
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("focus", refreshMeta);
    return () => {
      stopped = true;
      try { es?.close(); } catch {}
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("focus", refreshMeta);
    };
  }, [id, refreshMeta]);

  // periodic light metadata refresh (every 6s) so usage/turns/pendingSteers stay
  // in sync — events are live, but these aren't carried by events
  useEffect(() => {
    const t = setInterval(refreshMeta, 6000);
    return () => clearInterval(t);
  }, [refreshMeta]);

  const turnInfo = useMemo(() => computeTurnInfo(events), [events]);

  const scrollToBottom = (behavior: ScrollBehavior) => {
    const el = scrollRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior });
  };
  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    stickRef.current = nearBottom;
    setShowJump(!nearBottom);
  };
  useEffect(() => { if (stickRef.current) scrollToBottom("auto"); }, [events.length]);

  const sendSteer = async () => {
    const m = steer.trim();
    if (!m) return;
    setBusy(true);
    try {
      await jpost(`/api/agents/sessions/${id}/steer`, { message: m });
      setSteer("");
      await refreshMeta();
    } finally { setBusy(false); }
  };
  const removeSteer = async (index: number) => {
    try { await jpost(`/api/agents/sessions/${id}/cancel-steer`, { index }); await refreshMeta(); } catch {}
  };
  const stop = async () => {
    setBusy(true);
    try { await jpost(`/api/agents/sessions/${id}/stop`); await refreshMeta(); }
    finally { setBusy(false); }
  };

  if (!s) return (<><Header title="…" /><div className="px-7 py-6 text-faint text-[13px]">Loading agent session…</div></>);

  const c = STATUS_COLOR[s.status] ?? "#6e6e6e";
  const inFlight = IN_FLIGHT.includes(s.status);
  const usage = s.usage as Record<string, number> | null;
  const cost = usage?.total_cost_usd;
  const inTok = usage?.input_tokens; const outTok = usage?.output_tokens;
  const queued = (s as any).pendingSteers as string[] | undefined;

  return (
    <div className="h-full flex flex-col min-h-0">
      <Header
        title={<span className="flex items-center gap-2"><Link to="/agents" className="text-muted hover:text-fg">Agents</Link><Icon name="chevronRight" size={14} /><span className="font-semibold">{s.agentName}</span></span>}
        actions={
          <>
            {s.controlPort && <span className="text-[11px] px-2 py-1 rounded-md" style={{ background: "#f5a62312", color: "#f5a623" }}><Icon name="globe" size={12} /> browser :{s.controlPort}</span>}
            {inFlight && <button className="btn btn-danger btn-sm" disabled={busy} onClick={stop}><Icon name="square" size={13} /> Stop</button>}
          </>
        }
      />

      {/* status bar — fixed */}
      <div className="shrink-0 px-7 pt-4 pb-3 border-b" style={{ borderColor: "var(--color-line)" }}>
        <div className="max-w-[1000px] flex items-center gap-3 flex-wrap">
          <span className="inline-flex items-center gap-1.5 text-[12px] px-2 py-0.5 rounded-md" style={{ background: `${c}1e`, color: c }}>
            <span className="w-1.5 h-1.5 rounded-full" style={{ background: c, animation: inFlight ? "pulse 1.2s infinite" : undefined }} /> {s.status}
          </span>
          <span className="text-[11.5px] text-faint">{s.engine}</span>
          <span className="inline-flex items-center gap-1 text-[11.5px] px-2 py-0.5 rounded-md" style={{ background: "#161616" }}><Icon name="user" size={12} /> {s.profileName}</span>
          {s.scopes.map((sc) => <span key={sc} className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: "#0072f518", color: "#3b9eff" }}>{sc}</span>)}
          {s.runIds.length > 0 && (
            <span className="text-[11px] text-faint flex items-center gap-1">
              <Icon name="terminal" size={12} /> {s.runIds.map((r) => <Link key={r} to={`/runs/${r}`} className="mono text-running hover:underline">{r}</Link>)}
            </span>
          )}
          <span className="text-[11px] text-faint mono ml-auto">
            {s.turns} turn{s.turns !== 1 ? "s" : ""}
            {inTok != null && ` · ${(inTok / 1000).toFixed(1)}k in / ${((outTok ?? 0) / 1000).toFixed(1)}k out`}
            {cost != null && ` · $${cost.toFixed(4)}`}
            {` · ${inFlight ? duration(s.startedAt) : timeAgo(s.createdAt)}`}
          </span>
        </div>
        {s.status === "failed" && s.error && (
          <div className="max-w-[1000px] mt-2 text-[12.5px] rounded-lg px-3 py-2" style={{ background: "#ff5c5c12", color: "#ff8d8d", border: "1px solid #4a2424" }}>
            <span className="font-medium">Agent failed:</span> {s.error}
            <span className="text-faint"> — send a message to retry/continue.</span>
          </div>
        )}
        {s.status === "waiting" && (
          <div className="max-w-[1000px] mt-2 text-[12.5px] rounded-lg px-3 py-2 flex items-center gap-2" style={{ background: "#f5a62312", color: "#f5c06a", border: "1px solid #4a3a1a" }}>
            <Icon name="clock" size={13} />
            <span><span className="font-medium">Paused</span> — a workflow it launched is still running; it keeps the profile and will be woken automatically when the run finishes.</span>
          </div>
        )}
        {s.status === "scheduled" && (
          <div className="max-w-[1000px] mt-2 text-[12.5px] rounded-lg px-3 py-2 flex items-center gap-2" style={{ background: "#9b8cff12", color: "#b9adff", border: "1px solid #322a55" }}>
            <Icon name="clock" size={13} />
            <span><span className="font-medium">Scheduled</span> — wakes {untilTime(s.scheduledAt)} to continue{s.scheduledPrompt ? `: "${s.scheduledPrompt}"` : ""}. The profile is free meanwhile.</span>
          </div>
        )}
      </div>

      {/* transcript — the only scrolling zone; auto-follows the agent */}
      <div ref={scrollRef} onScroll={onScroll} className="flex-1 min-h-0 overflow-y-auto px-7 py-4">
        <div className="max-w-[1000px] flex flex-col gap-2.5">
          {events.map((e, i) => <EventRow key={i} e={e} turnInfo={turnInfo[i]} />)}
        </div>
        {showJump && (
          <div className="sticky bottom-0 flex justify-center pt-3 pointer-events-none">
            <button
              onClick={() => { stickRef.current = true; setShowJump(false); scrollToBottom("smooth"); }}
              className="btn btn-secondary btn-sm pointer-events-auto"
              style={{ boxShadow: "0 4px 16px #000a" }}>
              <Icon name="chevronRight" size={13} className="rotate-90" /> Jump to latest
            </button>
          </div>
        )}
      </div>

      {/* message bar — fixed bottom. Steer while running, or reactivate a rested session. */}
      <div className="shrink-0 border-t px-7 py-3 bg-panel" style={{ borderColor: "var(--color-line)" }}>
        <div className="max-w-[1000px]">
          {/* queued steers (when running) — compact list above the input with X to remove */}
          {queued && queued.length > 0 && (
            <div className="mb-2 flex flex-col gap-1.5">
              <div className="text-[10.5px] text-faint uppercase tracking-wider flex items-center gap-1.5">
                <Icon name="layers" size={11} /> {queued.length} queued message{queued.length === 1 ? "" : "s"} — will run after the current turn
              </div>
              {queued.map((q, i) => (
                <div key={i} className="rounded-lg px-3 py-1.5 flex items-center gap-2 text-[12px]"
                     style={{ background: "#0072f514", border: "1px solid #0072f540" }}>
                  <span className="flex-1 truncate" title={q}>{q}</span>
                  <button className="text-faint hover:text-fg" title="Remove" onClick={() => removeSteer(i)}>
                    <Icon name="x" size={13} />
                  </button>
                </div>
              ))}
            </div>
          )}
          {!inFlight && (
            <div className="text-[11px] text-faint mb-1.5 flex items-center gap-1.5">
              <Icon name="refresh" size={11} />
              {s.status === "done" ? "This turn is done — send a message to continue (the agent's thread is kept)."
                : s.status === "waiting" ? "Paused on a running workflow — it'll wake itself when that finishes, or send a message to steer it now."
                : s.status === "scheduled" ? `Scheduled to wake ${untilTime(s.scheduledAt)} — or send a message to continue it now.`
                : `Session ${s.status} — send a message to reactivate and continue it.`}
            </div>
          )}
          <div className="flex items-end gap-2">
            <textarea className="input" style={{ height: 44, padding: "11px 12px", resize: "none" }}
                      placeholder={inFlight ? "Steer the agent — runs after the current turn…" : "Send a message to continue this session…"}
                      value={steer} onChange={(e) => setSteer(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendSteer(); } }} />
            <button className="btn btn-primary" disabled={busy || !steer.trim()} onClick={sendSteer}>
              <Icon name={inFlight ? "send" : "play"} size={14} /> {inFlight ? "Send" : "Continue"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- event row(s)

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="text-[11px] text-faint hover:text-fg inline-flex items-center gap-1"
      title="Copy raw message"
      onClick={async () => {
        try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1200); } catch {}
      }}>
      <Icon name="copy" size={11} /> {copied ? "copied" : "copy"}
    </button>
  );
}

function MessageMarkdown({ text }: { text: string }) {
  // ChatGPT-style: GFM + math (KaTeX). Light styling via prose-ish utility classes.
  return (
    <div className="md text-[13px] leading-relaxed">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

function fmtSec(n: number): string {
  if (n < 60) return `${n.toFixed(n < 10 ? 1 : 0)}s`;
  const m = Math.floor(n / 60); const s = Math.floor(n % 60);
  return `${m}m ${s}s`;
}

function EventRow({ e, turnInfo }: { e: AgentEvent; turnInfo?: TurnInfo }) {
  // -- user-typed message (a steer or the launch prompt) — pinned right, solid blue
  if (e.kind === "system" && (e as any).role === "user") {
    return (
      <div className="self-end max-w-[80%] rounded-xl px-3.5 py-2 text-[12.5px] whitespace-pre-wrap"
           style={{ background: "#0072f5", color: "#fff" }}>{e.text}</div>
    );
  }
  // -- notification (🔔) — distinct from a plain system note: blue tint card with bell
  if (e.kind === "system" && (e.text || "").startsWith("🔔")) {
    const txt = (e.text || "").replace(/^🔔\s*/, "");
    return (
      <div className="rounded-lg px-3 py-2 text-[12.5px] flex items-start gap-2"
           style={{ background: "#0072f514", color: "#9ec8ff", border: "1px solid #0072f540" }}>
        <Icon name="alert" size={13} />
        <span><span className="font-medium">Notification:</span> {txt}</span>
      </div>
    );
  }
  // -- preempting / paused / various meta system notes
  if (e.kind === "system") return (
    <div className="text-[11px] text-faint flex items-center gap-1.5"><Icon name="dot" size={10} /> {e.text}</div>
  );
  // -- agent message — markdown + KaTeX + copy button; if end-of-turn, also show elapsed
  if (e.kind === "message") {
    const raw = (e as any).text as string || "";
    return (
      <div className="card p-3.5">
        <MessageMarkdown text={raw} />
        <div className="mt-1.5 pt-1.5 flex items-center gap-3 text-[11px] text-faint" style={{ borderTop: "1px solid var(--color-line)" }}>
          <CopyButton text={raw} />
          {turnInfo?.endOfTurn && turnInfo.elapsedSec != null && (
            <span className="ml-auto inline-flex items-center gap-1"><Icon name="clock" size={11} /> turn took {fmtSec(turnInfo.elapsedSec)}</span>
          )}
        </div>
      </div>
    );
  }
  if (e.kind === "reasoning") return <div className="text-[11.5px] text-faint italic px-1 whitespace-pre-wrap">{(e as any).text}</div>;
  if (e.kind === "status") return <div className="text-[11px] text-faint flex items-center gap-1.5"><Icon name="clock" size={11} /> {(e as any).status}{(e as any).text ? ` — ${(e as any).text}` : ""}</div>;
  if (e.kind === "usage") return null;
  if (e.kind === "error") return <div className="text-[12.5px] rounded-lg px-3 py-2" style={{ background: "#ff5c5c12", color: "#ff8d8d", border: "1px solid #4a2424" }}>{(e as any).text}</div>;
  if (e.kind === "tool_call") {
    const isBrowser = ((e as any).tool || "").startsWith("browser_");
    return (
      <div className="flex items-start gap-2 text-[12px]">
        <span className="shrink-0 mt-0.5" style={{ color: isBrowser ? "#f5a623" : "#3b9eff" }}><Icon name={isBrowser ? "globe" : "wand"} size={13} /></span>
        <div className="min-w-0">
          <span className="mono font-medium">{(e as any).tool}</span>
          {(e as any).args != null && Object.keys((e as any).args as object).length > 0 && (
            <span className="text-faint mono"> {JSON.stringify((e as any).args).slice(0, 160)}</span>
          )}
        </div>
      </div>
    );
  }
  if (e.kind === "tool_result") {
    const txt = unwrap((e as any).result);
    return (
      <details className="text-[11.5px] ml-5">
        <summary className="cursor-pointer select-none flex items-center gap-1.5" style={{ color: (e as any).ok ? "#2bd576" : "#ff8d8d" }}>
          <Icon name={(e as any).ok ? "check" : "alert"} size={12} /> {(e as any).ok ? "result" : "error"} <span className="text-faint">({txt.length} chars)</span>
        </summary>
        <pre className="mono text-faint mt-1 whitespace-pre-wrap break-words" style={{ maxHeight: 200, overflow: "auto" }}>{txt}</pre>
      </details>
    );
  }
  return null;
}
