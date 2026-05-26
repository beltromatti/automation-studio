import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { jget, jpost, jdel, timeAgo } from "@/lib/client";
import type { AgentDef, AgentEngine, AgentSession, EngineInfo, Profile } from "@/lib/types";

const STATUS_COLOR: Record<string, string> = {
  starting: "#3b9eff", running: "#3b9eff", idle: "#f5a623",
  done: "#2bd576", failed: "#ff5c5c", canceled: "#6e6e6e",
};

function Pill({ status }: { status: string }) {
  const c = STATUS_COLOR[status] ?? "#6e6e6e";
  const live = status === "running" || status === "starting";
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] px-2 py-0.5 rounded-md" style={{ background: `${c}1e`, color: c }}>
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: c, animation: live ? "pulse 1.2s infinite" : undefined }} />
      {status}
    </span>
  );
}

export default function AgentsPage() {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<AgentDef[]>([]);
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [engines, setEngines] = useState<Record<AgentEngine, EngineInfo> | null>(null);
  const [agentId, setAgentId] = useState("");
  const [profileId, setProfileId] = useState("ephemeral");
  const [prompt, setPrompt] = useState("");
  const [watch, setWatch] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<AgentDef | "new" | null>(null);
  const stopped = useRef(false);

  const loadAgents = useCallback(async () => {
    const d = await jget<{ agents: AgentDef[] }>("/api/agents").catch(() => ({ agents: [] }));
    if (stopped.current) return;
    setAgents(d.agents);
    setAgentId((cur) => cur || d.agents[0]?.id || "");
  }, []);
  const loadSessions = useCallback(async () => {
    const d = await jget<{ sessions: AgentSession[] }>("/api/agents/sessions").catch(() => ({ sessions: [] }));
    if (!stopped.current) setSessions(d.sessions);
  }, []);

  useEffect(() => {
    stopped.current = false;
    loadAgents();
    jget<{ profiles: Profile[] }>("/api/profiles").then((d) => setProfiles(d.profiles)).catch(() => {});
    jget<{ engines: Record<AgentEngine, EngineInfo> }>("/api/agents/engines").then((d) => setEngines(d.engines)).catch(() => {});
    loadSessions();
    const t = setInterval(loadSessions, 3000);
    return () => { stopped.current = true; clearInterval(t); };
  }, [loadAgents, loadSessions]);

  const selectedAgent = agents.find((a) => a.id === agentId);
  const wantsBrowser = selectedAgent?.scopes.includes("browser");
  const engineMissing = selectedAgent && engines && !engines[selectedAgent.engine]?.available;

  // a browser agent needs a persistent profile; force off ephemeral
  useEffect(() => {
    if (wantsBrowser && profileId === "ephemeral") setProfileId(profiles[0]?.id ?? "ephemeral");
  }, [wantsBrowser, profileId, profiles]);

  const launch = async () => {
    if (!agentId || !prompt.trim()) return;
    setBusy(true); setError("");
    try {
      const { session } = await jpost<{ session: AgentSession }>("/api/agents/sessions", { agentId, profileId, prompt, watch });
      navigate(`/agents/sessions/${session.id}`);
    } catch (e) { setError(String((e as Error).message)); setBusy(false); }
  };

  return (
    <>
      <Header
        title="Agents"
        sub="AI agents that drive the app and the browser — powered by your local Claude Code / Codex"
        actions={<button className="btn btn-primary btn-sm" onClick={() => setEditing("new")}><Icon name="plus" size={14} /> New agent</button>}
      />
      <div className="px-7 py-6 max-w-[980px]">
        {engines && !engines.codex.available && !engines.claude.available && (
          <div className="card p-4 mb-4 text-[12.5px]" style={{ borderColor: "#4a2424", color: "#ff8d8d" }}>
            Neither Codex nor Claude Code was found. Install one and sign in (with your subscription) to run agents.
          </div>
        )}

        {/* Launch */}
        <div className="card p-5 mb-6">
          <div className="text-[13px] font-medium mb-3 flex items-center gap-2"><Icon name="sparkles" size={15} /> Launch an agent</div>
          <div className="grid grid-cols-2 gap-3 mb-3">
            <div className="flex flex-col gap-1.5">
              <label className="label">Agent</label>
              <select className="input appearance-none" value={agentId} onChange={(e) => setAgentId(e.target.value)}>
                {agents.map((a) => <option key={a.id} value={a.id}>{a.name} · {a.engine}{a.scopes.includes("browser") ? " · browser" : ""}</option>)}
              </select>
            </div>
            <div className="flex flex-col gap-1.5">
              <label className="label">Profile {wantsBrowser && <span className="text-faint">(persistent — agent owns the browser)</span>}</label>
              <select className="input appearance-none" value={profileId} onChange={(e) => setProfileId(e.target.value)}>
                {!wantsBrowser && <option value="ephemeral">Ephemeral — throwaway</option>}
                {profiles.map((p) => <option key={p.id} value={p.id}>{p.name}{p.open ? " — window open" : ""}</option>)}
              </select>
            </div>
          </div>
          <div className="flex flex-col gap-1.5 mb-3">
            <label className="label">Task</label>
            <textarea className="input" style={{ height: 76, padding: 10, lineHeight: 1.5 }}
                      placeholder="Tell the agent what to do — e.g. “Run LinkedIn People for data engineers in Milan, capture into a dataset, dedup by profile URL, then project the links into a connect-input dataset.”"
                      value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          </div>
          {selectedAgent?.systemPrompt && <div className="text-[11px] text-faint mb-2 line-clamp-2">Skills: {selectedAgent.systemPrompt}</div>}
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 cursor-pointer select-none" onClick={() => setWatch((w) => !w)}>
              <span className="w-9 h-5 rounded-full relative transition-colors" style={{ background: watch ? "#0072f5" : "#2a2a2a" }}>
                <span className="absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all" style={{ left: watch ? 18 : 2 }} />
              </span>
              <span className="text-[12.5px]">Watch the browser <span className="text-faint">(headed)</span></span>
            </label>
            <button className="btn btn-primary ml-auto" disabled={busy || !prompt.trim() || !!engineMissing} onClick={launch}>
              <Icon name="play" size={14} /> {busy ? "Launching…" : "Launch agent"}
            </button>
          </div>
          {engineMissing && <div className="text-[12px] text-danger mt-2">{selectedAgent?.engine} is not installed/available on this machine.</div>}
          {error && <div className="text-[12px] text-danger mt-2">{error}</div>}
        </div>

        {/* Sessions */}
        <div className="text-[13px] font-medium mb-2.5 flex items-center gap-2"><Icon name="terminal" size={15} /> Sessions</div>
        <div className="flex flex-col gap-2 mb-7">
          {sessions.length === 0 && <div className="card p-6 text-center text-[12.5px] text-faint">No agent sessions yet.</div>}
          {sessions.map((s) => (
            <Link key={s.id} to={`/agents/sessions/${s.id}`} className="card card-hover p-3.5 flex items-center gap-3">
              <Pill status={s.status} />
              <span className="text-[13px] font-medium">{s.agentName}</span>
              <span className="text-[11px] text-faint">{s.engine}</span>
              <span className="text-[11.5px] text-muted truncate flex-1">{s.prompt}</span>
              <span className="text-[11px] text-faint mono shrink-0">{s.profileName} · {timeAgo(s.createdAt)}</span>
            </Link>
          ))}
        </div>

        {/* Definitions */}
        <div className="text-[13px] font-medium mb-2.5 flex items-center gap-2"><Icon name="bot" size={15} /> Your agents</div>
        <div className="flex flex-col gap-2">
          {agents.map((a) => (
            <div key={a.id} className="card p-3.5 flex items-center gap-3">
              <span className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0" style={{ background: "#0072f518", color: "#3b9eff" }}><Icon name={a.icon || "sparkles"} size={15} /></span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-medium">{a.name}</span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded text-faint" style={{ background: "#161616" }}>{a.engine}</span>
                  {a.scopes.map((sc) => <span key={sc} className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: "#0072f518", color: "#3b9eff" }}>{sc}</span>)}
                  {a.builtin && <span className="text-[10px] px-1.5 py-0.5 rounded text-faint" style={{ background: "#161616" }}>built-in</span>}
                </div>
                {a.systemPrompt && <div className="text-[11px] text-faint mt-0.5 truncate">{a.systemPrompt}</div>}
              </div>
              <button className="btn btn-secondary btn-sm" onClick={() => setEditing(a)}><Icon name="pencil" size={13} /></button>
              {!a.builtin && <button className="btn btn-secondary btn-sm" onClick={async () => { await jdel(`/api/agents/${a.id}`); loadAgents(); }}><Icon name="trash" size={13} /></button>}
            </div>
          ))}
        </div>
      </div>

      {editing && <AgentEditor agent={editing} engines={engines} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); loadAgents(); }} />}
    </>
  );
}

function AgentEditor({ agent, engines, onClose, onSaved }: {
  agent: AgentDef | "new"; engines: Record<AgentEngine, EngineInfo> | null; onClose: () => void; onSaved: () => void;
}) {
  const isNew = agent === "new";
  const a = isNew ? null : (agent as AgentDef);
  const [name, setName] = useState(a?.name ?? "");
  const [engine, setEngine] = useState<AgentEngine>(a?.engine ?? "codex");
  const [systemPrompt, setSystemPrompt] = useState(a?.systemPrompt ?? "");
  const [scopes, setScopes] = useState<string[]>(a?.scopes ?? ["studio"]);
  const [error, setError] = useState("");
  const toggle = (s: string) => setScopes((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]));
  const save = async () => {
    if (!name.trim()) { setError("Name is required."); return; }
    try {
      const body = { name, engine, systemPrompt, scopes: scopes.includes("studio") ? scopes : ["studio", ...scopes] };
      if (isNew) await jpost("/api/agents", body);
      else await jpost(`/api/agents/${a!.id}/update`, body);
      onSaved();
    } catch (e) { setError(String((e as Error).message)); }
  };
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "#000000aa" }} onClick={onClose}>
      <div className="card p-5" style={{ width: 560, maxWidth: "92vw" }} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <div className="text-[14px] font-semibold">{isNew ? "New agent" : `Edit ${a!.name}`}</div>
          <button className="text-faint hover:text-fg" onClick={onClose}><Icon name="x" size={16} /></button>
        </div>
        <label className="label">Name</label>
        <input className="input mt-1 mb-3" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Lead Hunter" />
        <label className="label">Engine</label>
        <select className="input appearance-none mt-1 mb-3" value={engine} onChange={(e) => setEngine(e.target.value as AgentEngine)}>
          <option value="codex">Codex {engines && !engines.codex.available ? "(not installed)" : ""}</option>
          <option value="claude">Claude Code {engines && !engines.claude.available ? "(not installed)" : ""}</option>
        </select>
        <label className="label">Capabilities</label>
        <div className="flex gap-2 mt-1 mb-3">
          <span className="btn btn-secondary btn-sm" style={{ opacity: 0.6, cursor: "default" }}><Icon name="check" size={12} /> studio (always)</span>
          <button className="btn btn-sm" style={{ borderColor: scopes.includes("browser") ? "#0072f5" : "var(--color-line-strong)", color: scopes.includes("browser") ? "#3b9eff" : "var(--color-fg)", border: "1px solid" }} onClick={() => toggle("browser")}>
            <Icon name="globe" size={12} /> browser {scopes.includes("browser") ? "✓" : ""}
          </button>
        </div>
        <label className="label">Skills / system prompt</label>
        <textarea className="input mt-1 mb-3" style={{ height: 120, padding: 10, lineHeight: 1.5 }} value={systemPrompt}
                  onChange={(e) => setSystemPrompt(e.target.value)} placeholder="Describe the agent's role, expertise and how it should work…" />
        {error && <div className="text-[12px] text-danger mb-2">{error}</div>}
        <button className="btn btn-primary btn-sm" onClick={save}><Icon name="check" size={13} /> {isNew ? "Create agent" : "Save"}</button>
      </div>
    </div>
  );
}
