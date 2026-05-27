import { Link, useNavigate, useParams } from "react-router-dom";
import { useEffect, useMemo, useState } from "react";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { jget, jpost } from "@/lib/client";
import { SessionRow } from "@/components/SessionRow";
import { DependencyModal, useChromeGate } from "@/components/DependencyModal";
import type { AgentDef, AgentEngine, AgentSession, EngineInfo, Profile } from "@/lib/types";

export default function AgentLaunchPage() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [agent, setAgent] = useState<AgentDef | null>(null);
  const [engines, setEngines] = useState<Record<AgentEngine, EngineInfo> | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [profileId, setProfileId] = useState("ephemeral");
  const [engine, setEngine] = useState<AgentEngine>("codex");
  const [prompt, setPrompt] = useState("");
  const [watch, setWatch] = useState(false);
  const [scheduleMin, setScheduleMin] = useState(0); // 0 = launch now; else minutes from now
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showEngineHelp, setShowEngineHelp] = useState(false);
  const { guard, modal: chromeModal } = useChromeGate();

  useEffect(() => {
    jget<{ agents: AgentDef[] }>("/api/agents").then((d) => setAgent(d.agents.find((a) => a.id === id) ?? null)).catch(() => {});
    jget<{ engines: Record<AgentEngine, EngineInfo> }>("/api/agents/engines").then((d) => {
      setEngines(d.engines);
      setEngine(d.engines.codex?.available ? "codex" : d.engines.claude?.available ? "claude" : "codex");
    }).catch(() => {});
    jget<{ profiles: Profile[] }>("/api/profiles").then((d) => setProfiles(d.profiles)).catch(() => {});
    const load = () => jget<{ sessions: AgentSession[] }>("/api/agents/sessions").then((d) => setSessions(d.sessions.filter((s) => s.agentId === id))).catch(() => {});
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [id]);

  const wantsBrowser = !!agent?.scopes.includes("browser");
  const engineMissing = !!(engines && !engines[engine]?.available);

  // browser agents need a persistent profile; default off ephemeral
  useEffect(() => {
    if (wantsBrowser) setProfileId((cur) => (cur === "ephemeral" ? (profiles[0]?.id ?? "ephemeral") : cur));
  }, [wantsBrowser, profiles]);

  const launch = async () => {
    if (!prompt.trim()) return;
    setBusy(true); setError("");
    try {
      const { session } = await jpost<{ session: AgentSession }>("/api/agents/sessions",
        { agentId: id, profileId, prompt, watch, engine, inSeconds: scheduleMin > 0 ? scheduleMin * 60 : undefined });
      navigate(`/agents/sessions/${session.id}`);
    } catch (e) { setError(String((e as Error).message)); setBusy(false); }
  };

  const recent = useMemo(() => sessions.slice(0, 12), [sessions]);

  if (!agent) return (<><Header title="…" /><div className="px-7 py-6 text-faint text-[13px]">Loading agent…</div></>);

  return (
    <>
      <Header
        title={
          <span className="flex items-center gap-2">
            <Link to="/agents" className="text-muted hover:text-fg">Agents</Link>
            <Icon name="chevronRight" size={14} />
            {agent.name}
            {agent.scopes.includes("browser") && <span className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: "#f5a62318", color: "#f5a623" }}>browser</span>}
          </span>
        }
        actions={<button className="btn btn-secondary btn-sm" onClick={() => navigate("/agents")}><Icon name="pencil" size={13} /> Manage</button>}
      />
      <div className="px-7 py-6 max-w-[1000px]">
        <p className="text-[13px] text-muted max-w-[640px] leading-relaxed mb-5">{agent.description || agent.systemPrompt || "No description."}</p>

        {/* Launch form — the agent's "run" equivalent */}
        <div className="card p-5 max-w-[620px]">
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <label className="label">Task<span className="text-danger"> *</span></label>
              <textarea className="input" style={{ height: 96, padding: 11, lineHeight: 1.5 }} autoFocus
                        placeholder={"Tell the agent what to do — e.g. “Run LinkedIn People for data engineers in Milan, capture into a dataset, dedup by profile URL, then project the links into a connect-input dataset.”"}
                        value={prompt} onChange={(e) => setPrompt(e.target.value)} />
            </div>

            <div className="flex flex-col gap-1.5">
              <label className="label">Engine</label>
              <div className="relative flex items-center">
                <span className="absolute left-3 pointer-events-none text-faint"><Icon name="sparkles" size={14} /></span>
                <select className="input appearance-none" style={{ paddingLeft: 32 }} value={engine} onChange={(e) => setEngine(e.target.value as AgentEngine)}>
                  <option value="codex" disabled={!!(engines && !engines.codex.available)}>Codex{engines && !engines.codex.available ? " — not installed" : ""}</option>
                  <option value="claude" disabled={!!(engines && !engines.claude.available)}>Claude Code{engines && !engines.claude.available ? " — not installed" : ""}</option>
                </select>
                <Icon name="chevronRight" size={14} className="absolute right-3 rotate-90 pointer-events-none text-faint" />
              </div>
              <span className="text-[11px] text-faint">Chosen per session — the same agent can run on either engine.</span>
            </div>

            <div className="flex flex-col gap-1.5">
              <label className="label">Profile {wantsBrowser && <span className="text-faint">— persistent (the agent owns the browser)</span>}</label>
              <div className="relative flex items-center">
                <span className="absolute left-3 pointer-events-none text-faint"><Icon name="user" size={14} /></span>
                <select className="input appearance-none" style={{ paddingLeft: 32 }} value={profileId} onChange={(e) => setProfileId(e.target.value)}>
                  {!wantsBrowser && <option value="ephemeral">Ephemeral — throwaway, no saved login</option>}
                  {profiles.map((p) => <option key={p.id} value={p.id}>{p.name}{p.open ? " — window open" : ""}</option>)}
                </select>
                <Icon name="chevronRight" size={14} className="absolute right-3 rotate-90 pointer-events-none text-faint" />
              </div>
              <span className="text-[11px] text-faint">
                {wantsBrowser
                  ? "This agent drives a real browser on the chosen profile and can run workflows on that same session."
                  : "Studio-only agent: it orchestrates workflows & data. Workflows it launches pick their own profile."}
              </span>
            </div>

            <label className="flex items-center gap-2.5 cursor-pointer select-none" onClick={() => setWatch((w) => !w)}>
              <span className="w-9 h-5 rounded-full relative transition-colors" style={{ background: watch ? "#0072f5" : "#2a2a2a" }}>
                <span className="absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all" style={{ left: watch ? 18 : 2 }} />
              </span>
              <span className="text-[13px]">Watch the browser <span className="text-faint">— headed window during the session</span></span>
            </label>

            {engineMissing && (
              <div className="card p-3 flex items-center gap-2 text-[12px]" style={{ background: "#ff5c5c12", color: "#ff8d8d", borderColor: "#4a2424" }}>
                <Icon name="alert" size={15} />
                <span><b>{engine === "codex" ? "Codex" : "Claude Code"}</b> isn't installed on this machine.</span>
                <button className="btn btn-secondary btn-sm ml-auto" onClick={() => setShowEngineHelp(true)}>
                  <Icon name="download2" size={13} /> How to install
                </button>
              </div>
            )}
            {error && <div className="text-[12px] text-danger">{error}</div>}

            <label className="flex items-center gap-2 text-[13px] text-muted">
              <Icon name="clock" size={14} /><span>Schedule:</span>
              <input type="number" min={0} step={1} value={scheduleMin}
                     onChange={(e) => setScheduleMin(Math.max(0, Number(e.target.value) || 0))}
                     className="input" style={{ width: 72, height: 30 }} />
              <span className="text-faint">{scheduleMin > 0 ? "minutes from now" : "minutes from now (0 = launch now)"}</span>
            </label>
            <button onClick={() => (wantsBrowser ? guard(launch) : launch())} disabled={busy || !prompt.trim() || !!engineMissing} className="btn btn-primary mt-1 self-start">
              <Icon name={scheduleMin > 0 ? "clock" : "play"} size={14} /> {busy ? "Launching…" : scheduleMin > 0 ? `Schedule in ${scheduleMin}m` : "Launch agent"}
            </button>
          </div>
        </div>

        {recent.length > 0 && (
          <div className="mt-9">
            <h2 className="text-[14px] font-semibold mb-3">Sessions</h2>
            <div className="flex flex-col gap-2 max-w-[760px]">
              {recent.map((s) => <SessionRow key={s.id} session={s} />)}
            </div>
          </div>
        )}
      </div>
      {chromeModal}
      {showEngineHelp && engines?.[engine]?.install && (
        <DependencyModal kind="engine" install={engines[engine].install!} onClose={() => setShowEngineHelp(false)} />
      )}
    </>
  );
}
