import { Link, useNavigate, useParams } from "react-router-dom";
import { useEffect, useMemo, useState } from "react";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { jget, jpost, timeAgo } from "@/lib/client";
import type { AgentDef, AgentEngine, AgentSession, EngineInfo, Profile } from "@/lib/types";

const STATUS_COLOR: Record<string, string> = {
  starting: "#3b9eff", running: "#3b9eff", idle: "#f5a623",
  done: "#2bd576", failed: "#ff5c5c", canceled: "#6e6e6e",
};

export default function AgentLaunchPage() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [agent, setAgent] = useState<AgentDef | null>(null);
  const [engines, setEngines] = useState<Record<AgentEngine, EngineInfo> | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [profileId, setProfileId] = useState("ephemeral");
  const [prompt, setPrompt] = useState("");
  const [watch, setWatch] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    jget<{ agents: AgentDef[] }>("/api/agents").then((d) => setAgent(d.agents.find((a) => a.id === id) ?? null)).catch(() => {});
    jget<{ engines: Record<AgentEngine, EngineInfo> }>("/api/agents/engines").then((d) => setEngines(d.engines)).catch(() => {});
    jget<{ profiles: Profile[] }>("/api/profiles").then((d) => setProfiles(d.profiles)).catch(() => {});
    const load = () => jget<{ sessions: AgentSession[] }>("/api/agents/sessions").then((d) => setSessions(d.sessions.filter((s) => s.agentId === id))).catch(() => {});
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [id]);

  const wantsBrowser = !!agent?.scopes.includes("browser");
  const engineMissing = agent && engines && !engines[agent.engine]?.available;

  // browser agents need a persistent profile; default off ephemeral
  useEffect(() => {
    if (wantsBrowser) setProfileId((cur) => (cur === "ephemeral" ? (profiles[0]?.id ?? "ephemeral") : cur));
  }, [wantsBrowser, profiles]);

  const launch = async () => {
    if (!prompt.trim()) return;
    setBusy(true); setError("");
    try {
      const { session } = await jpost<{ session: AgentSession }>("/api/agents/sessions", { agentId: id, profileId, prompt, watch });
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
            <span className="text-[10px] px-1.5 py-0.5 rounded text-faint" style={{ background: "#161616" }}>{agent.engine}</span>
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

            {engineMissing && <div className="text-[12px] text-danger">{agent.engine} is not installed/available on this machine.</div>}
            {error && <div className="text-[12px] text-danger">{error}</div>}

            <button onClick={launch} disabled={busy || !prompt.trim() || !!engineMissing} className="btn btn-primary mt-1 self-start">
              <Icon name="play" size={14} /> {busy ? "Launching…" : "Launch agent"}
            </button>
          </div>
        </div>

        {recent.length > 0 && (
          <div className="mt-9">
            <h2 className="text-[14px] font-semibold mb-3">Sessions</h2>
            <div className="flex flex-col gap-2 max-w-[760px]">
              {recent.map((s) => {
                const c = STATUS_COLOR[s.status] ?? "#6e6e6e";
                return (
                  <Link key={s.id} to={`/agents/sessions/${s.id}`} className="card card-hover p-3.5 flex items-center gap-3">
                    <span className="inline-flex items-center gap-1.5 text-[11px] px-2 py-0.5 rounded-md" style={{ background: `${c}1e`, color: c }}>
                      <span className="w-1.5 h-1.5 rounded-full" style={{ background: c }} /> {s.status}
                    </span>
                    <span className="text-[11.5px] text-muted truncate flex-1">{s.prompt}</span>
                    <span className="text-[11px] text-faint mono shrink-0">{s.profileName} · {timeAgo(s.createdAt)}</span>
                  </Link>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </>
  );
}
