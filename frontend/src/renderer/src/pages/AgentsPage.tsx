import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { jget, jpost, jdel, timeAgo } from "@/lib/client";
import type { AgentDef, AgentEngine, AgentSession, EngineInfo } from "@/lib/types";

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
  const [engines, setEngines] = useState<Record<AgentEngine, EngineInfo> | null>(null);
  const [editing, setEditing] = useState<AgentDef | "new" | null>(null);
  const stopped = useRef(false);

  const loadAgents = useCallback(async () => {
    const d = await jget<{ agents: AgentDef[] }>("/api/agents").catch(() => ({ agents: [] }));
    if (!stopped.current) setAgents(d.agents);
  }, []);
  const loadSessions = useCallback(async () => {
    const d = await jget<{ sessions: AgentSession[] }>("/api/agents/sessions").catch(() => ({ sessions: [] }));
    if (!stopped.current) setSessions(d.sessions);
  }, []);

  useEffect(() => {
    stopped.current = false;
    loadAgents();
    jget<{ engines: Record<AgentEngine, EngineInfo> }>("/api/agents/engines").then((d) => setEngines(d.engines)).catch(() => {});
    loadSessions();
    const t = setInterval(loadSessions, 3000);
    return () => { stopped.current = true; clearInterval(t); };
  }, [loadAgents, loadSessions]);

  return (
    <>
      <Header
        title="Agents"
        sub="AI agents that drive the app and the browser — powered by your local Claude Code / Codex"
        actions={<button className="btn btn-primary btn-sm" onClick={() => setEditing("new")}><Icon name="plus" size={14} /> New agent</button>}
      />
      <div className="px-7 py-6 max-w-[1100px]">
        {engines && !engines.codex.available && !engines.claude.available && (
          <div className="card p-4 mb-5 text-[12.5px]" style={{ borderColor: "#4a2424", color: "#ff8d8d" }}>
            Neither Codex nor Claude Code was found. Install one and sign in (with your subscription) to run agents.
          </div>
        )}

        {/* Agent cards — same footprint as workflow cards, customised for agents */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {agents.map((a) => {
            const last = sessions.find((s) => s.agentId === a.id);
            const missing = engines && !engines[a.engine]?.available;
            return (
              <div key={a.id} className="card card-hover p-5 flex flex-col">
                <div className="flex items-start gap-3">
                  <div className="w-10 h-10 rounded-lg flex items-center justify-center shrink-0" style={{ background: "#0072f518", color: "#3b9eff" }}>
                    <Icon name={a.icon || "sparkles"} size={19} />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <h3 className="text-[15px] font-semibold">{a.name}</h3>
                      <span className="text-[10px] px-1.5 py-0.5 rounded text-faint" style={{ background: "#161616" }}>{a.engine}</span>
                      {a.scopes.includes("browser") && <span className="text-[10px] px-1.5 py-0.5 rounded inline-flex items-center gap-1" style={{ background: "#f5a62318", color: "#f5a623" }}><Icon name="globe" size={10} /> browser</span>}
                      {a.builtin && <span className="text-[10px] px-1.5 py-0.5 rounded text-faint" style={{ background: "#161616" }}>built-in</span>}
                    </div>
                    <p className="text-[12.5px] text-muted mt-1 leading-relaxed line-clamp-2">{a.description || a.systemPrompt || "No description."}</p>
                  </div>
                </div>
                <div className="flex items-center justify-between mt-4 pt-4 border-t" style={{ borderColor: "var(--color-line)" }}>
                  <span className="text-[11px] text-faint mono">{last ? `last ${last.status}` : "never launched"}{missing ? " · engine missing" : ""}</span>
                  <div className="flex items-center gap-1.5">
                    <button className="btn btn-secondary btn-sm" onClick={() => setEditing(a)} title="Edit agent"><Icon name="pencil" size={13} /></button>
                    {!a.builtin && <button className="btn btn-secondary btn-sm" title="Delete agent" onClick={async () => { if (confirm(`Delete agent "${a.name}"?`)) { await jdel(`/api/agents/${a.id}`); loadAgents(); } }}><Icon name="trash" size={13} /></button>}
                    <Link to={`/agents/${a.id}`} className="btn btn-primary btn-sm" aria-disabled={!!missing} onClick={(e) => { if (missing) e.preventDefault(); }}>
                      <Icon name="play" size={13} /> Launch
                    </Link>
                  </div>
                </div>
              </div>
            );
          })}
          {agents.length === 0 && (
            <div className="card p-10 text-center text-[13px] text-faint flex flex-col items-center gap-2">
              <Icon name="sparkles" size={22} /> No agents yet — create one.
            </div>
          )}
        </div>

        {/* Sessions */}
        <div className="mt-9">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-[14px] font-semibold flex items-center gap-2"><Icon name="terminal" size={15} /> Sessions</h2>
          </div>
          <div className="flex flex-col gap-2">
            {sessions.length === 0 && <div className="card p-6 text-center text-[12.5px] text-faint">No agent sessions yet — launch an agent above.</div>}
            {sessions.slice(0, 12).map((s) => (
              <Link key={s.id} to={`/agents/sessions/${s.id}`} className="card card-hover p-3.5 flex items-center gap-3">
                <Pill status={s.status} />
                <span className="text-[13px] font-medium shrink-0">{s.agentName}</span>
                <span className="text-[11px] text-faint shrink-0">{s.engine}</span>
                <span className="text-[11.5px] text-muted truncate flex-1">{s.prompt}</span>
                <span className="text-[11px] text-faint mono shrink-0">{s.profileName} · {timeAgo(s.createdAt)}</span>
              </Link>
            ))}
          </div>
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
  const [description, setDescription] = useState(a?.description ?? "");
  const [engine, setEngine] = useState<AgentEngine>(a?.engine ?? "codex");
  const [systemPrompt, setSystemPrompt] = useState(a?.systemPrompt ?? "");
  const [scopes, setScopes] = useState<string[]>(a?.scopes ?? ["studio"]);
  const [error, setError] = useState("");
  const toggle = (s: string) => setScopes((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]));
  const save = async () => {
    if (!name.trim()) { setError("Name is required."); return; }
    try {
      const body = { name, description, engine, systemPrompt, scopes: scopes.includes("studio") ? scopes : ["studio", ...scopes] };
      if (isNew) await jpost("/api/agents", body);
      else await jpost(`/api/agents/${a!.id}/update`, body);
      onSaved();
    } catch (e) { setError(String((e as Error).message)); }
  };
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6" style={{ background: "#000000aa" }} onClick={onClose}>
      <div className="card p-5 overflow-auto" style={{ width: 580, maxWidth: "92vw", maxHeight: "90vh" }} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <div className="text-[14px] font-semibold">{isNew ? "New agent" : `Edit ${a!.name}`}</div>
          <button className="text-faint hover:text-fg" onClick={onClose}><Icon name="x" size={16} /></button>
        </div>
        <label className="label">Name</label>
        <input className="input mt-1 mb-3" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Lead Hunter" />
        <label className="label">Description <span className="text-faint">(optional — shown on the card)</span></label>
        <input className="input mt-1 mb-3" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this agent is for" />
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
