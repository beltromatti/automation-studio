"""AgentManager — AI agents as a first-class, superior-to-workflows layer.

An **agent definition** is a reusable template: an engine (claude | codex), a
system prompt ("skills"), and capability scopes (studio always; browser when it
owns a profile). **Launching** an agent = pick a definition + a profile + a
prompt; the backend spawns the user's locally-installed CLI in headless-JSON mode
(inheriting their subscription — no API keys), attaches our MCP tool server, and
streams the engine's events normalised into one taxonomy the UI renders live.

Both engines are driven symmetrically as local subprocesses:
  codex exec --json ...      (Codex, ChatGPT subscription via ~/.codex/auth.json)
  claude -p --output-format stream-json ...   (Claude Code, Pro/Max login)

Profile ownership: a browser-scope agent on a persistent profile opens a
control-server (via the RunManager, which serialises that profile) and the agent
drives it through browser_* tools; workflows it launches on that same profile
ATTACH to this control-server (shared browser) instead of spawning their own.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from humanbrowser.config import data_dir
from .manager import get_manager, _self_base, kill_tree, is_ephemeral

DATA = data_dir()
AGENTS_FILE = DATA / "agents.json"
SESSIONS_FILE = DATA / "agent_sessions.json"
SESSIONS_DIR = DATA / "agent_runs"
BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
MAX_EVENTS = 4000
TERMINAL = {"done", "failed", "canceled"}

ENGINES = {"codex", "claude"}


def _find_binary(engine: str) -> str | None:
    """Resolve the user's installed CLI. Finder-launched apps don't inherit the
    shell PATH, so probe common install locations as well as PATH."""
    name = engine
    found = shutil.which(name)
    if found:
        return found
    candidates = [
        f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", f"/usr/bin/{name}",
        os.path.expanduser(f"~/.local/bin/{name}"),
        os.path.expanduser(f"~/.npm-global/bin/{name}"),
    ]
    # nvm installs (codex/claude often live under a node version)
    nvm = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(nvm):
        for v in sorted(os.listdir(nvm), reverse=True):
            candidates.append(os.path.join(nvm, v, "bin", name))
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


@dataclass
class AgentDef:
    id: str
    name: str
    engine: str                       # "codex" | "claude"
    icon: str = "sparkles"
    systemPrompt: str = ""            # the agent's skills / role
    scopes: list[str] = field(default_factory=lambda: ["studio"])  # + "browser"
    createdAt: float = 0.0
    builtin: bool = False


@dataclass
class AgentSession:
    id: str
    agentId: str
    agentName: str
    engine: str
    scopes: list[str]
    profileId: str
    profileName: str
    prompt: str
    status: str                       # starting|running|idle|done|failed|canceled
    createdAt: float
    watch: bool = False
    startedAt: float | None = None
    finishedAt: float | None = None
    error: str | None = None
    controlPort: int | None = None    # owned browser control-server (browser scope)
    threadId: str | None = None       # codex thread / claude session id (for steering)
    usage: dict | None = None
    turns: int = 0
    runIds: list[str] = field(default_factory=list)
    pendingSteers: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ normalisation
def _norm_codex(obj: dict) -> list[dict]:
    """Codex `exec --json` JSONL → normalised events."""
    t = obj.get("type", "")
    out: list[dict] = []
    if t == "thread.started":
        out.append({"kind": "system", "text": "session started", "threadId": obj.get("thread_id")})
    elif t == "turn.started":
        out.append({"kind": "status", "status": "thinking"})
    elif t == "turn.completed":
        out.append({"kind": "usage", "usage": obj.get("usage")})
    elif t == "turn.failed":
        out.append({"kind": "error", "text": (obj.get("error") or {}).get("message", "turn failed")})
    elif t in ("item.started", "item.updated", "item.completed"):
        it = obj.get("item") or {}
        itype = it.get("type")
        done = t == "item.completed"
        if itype == "agent_message":
            if done:
                out.append({"kind": "message", "text": it.get("text", "")})
        elif itype == "reasoning":
            if done:
                out.append({"kind": "reasoning", "text": it.get("text", "")})
        elif itype == "mcp_tool_call":
            if t == "item.started":
                out.append({"kind": "tool_call", "tool": it.get("tool"), "args": it.get("arguments"),
                            "server": it.get("server")})
            elif done:
                out.append({"kind": "tool_result", "tool": it.get("tool"),
                            "ok": it.get("status") != "failed",
                            "result": _short(it.get("result") or (it.get("error") or {}).get("message"))})
        elif itype == "command_execution":
            if t == "item.started":
                out.append({"kind": "tool_call", "tool": "shell", "args": {"command": it.get("command")}})
            elif done:
                out.append({"kind": "tool_result", "tool": "shell", "ok": it.get("exit_code") == 0,
                            "result": _short(it.get("aggregated_output"))})
        elif itype == "web_search":
            if done:
                out.append({"kind": "tool_call", "tool": "web_search", "args": {"query": it.get("query")}})
        elif itype in ("todo_list", "plan_update"):
            if done:
                items = it.get("items") or []
                out.append({"kind": "status", "status": "plan",
                            "text": " · ".join(f"{'✓' if i.get('completed') else '○'} {i.get('text','')}" for i in items)})
    elif t == "error":
        out.append({"kind": "error", "text": obj.get("message", "error")})
    return out


def _norm_claude(obj: dict) -> list[dict]:
    """Claude `-p --output-format stream-json` → normalised events."""
    t = obj.get("type")
    out: list[dict] = []
    if t == "system" and obj.get("subtype") == "init":
        out.append({"kind": "system", "text": f"session started ({obj.get('model','')})",
                    "threadId": obj.get("session_id")})
    elif t == "assistant":
        for b in (obj.get("message") or {}).get("content", []):
            if b.get("type") == "text" and b.get("text", "").strip():
                out.append({"kind": "message", "text": b["text"]})
            elif b.get("type") == "tool_use":
                name = b.get("name") or ""
                if name.startswith("mcp__studio__"):
                    name = name[len("mcp__studio__"):]  # show studio_/browser_ like Codex does
                out.append({"kind": "tool_call", "tool": name, "args": b.get("input")})
            elif b.get("type") == "thinking" and b.get("thinking"):
                out.append({"kind": "reasoning", "text": b.get("thinking")})
    elif t == "user":
        for b in (obj.get("message") or {}).get("content", []):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                txt = c if isinstance(c, str) else json.dumps(c, default=str)
                out.append({"kind": "tool_result", "tool": "", "ok": not b.get("is_error"),
                            "result": _short(txt)})
    elif t == "result":
        out.append({"kind": "usage", "usage": {"total_cost_usd": obj.get("total_cost_usd"),
                                               **(obj.get("usage") or {})}})
        if obj.get("result") and obj.get("subtype") != "success":
            out.append({"kind": "error", "text": str(obj.get("result"))[:500]})
    elif t == "rate_limit_event":
        out.append({"kind": "status", "status": "rate-limited"})
    return out


def _extract_run_id(result: Any) -> str | None:
    """A studio_run_workflow tool result may arrive as plain JSON (Claude) or
    wrapped in MCP {content:[{text}]} (Codex). Dig out runId either way."""
    if not result:
        return None
    try:
        obj = json.loads(result) if isinstance(result, str) else result
    except Exception:
        return None
    if isinstance(obj, dict):
        if obj.get("runId"):
            return obj["runId"]
        content = obj.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            try:
                inner = json.loads(content[0].get("text") or "{}")
                return inner.get("runId")
            except Exception:
                return None
    return None


def _short(v: Any, n: int = 1200) -> str:
    if v is None:
        return ""
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + f"… (+{len(s) - n} chars)"


class AgentManager:
    def __init__(self):
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self.defs: dict[str, AgentDef] = {}
        self.sessions: dict[str, AgentSession] = {}
        self.events: dict[str, list[dict]] = {}
        self.procs: dict[str, Any] = {}        # session id -> current turn subprocess
        self._load()
        self._seed()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            if AGENTS_FILE.exists():
                for d in json.loads(AGENTS_FILE.read_text()):
                    self.defs[d["id"]] = AgentDef(**{k: d[k] for k in AgentDef.__dataclass_fields__ if k in d})
        except Exception:
            pass
        try:
            if SESSIONS_FILE.exists():
                for d in json.loads(SESSIONS_FILE.read_text()):
                    s = AgentSession(**{k: d[k] for k in AgentSession.__dataclass_fields__ if k in d})
                    if s.status not in TERMINAL:
                        s.status = "done" if s.turns else "failed"
                        s.error = s.error or ("interrupted (backend restarted)" if not s.turns else None)
                        s.controlPort = None
                        s.finishedAt = s.finishedAt or time.time()
                    self.sessions[s.id] = s
                    tf = SESSIONS_DIR / s.id / "transcript.jsonl"
                    if tf.exists():
                        evs = [json.loads(l) for l in tf.read_text().splitlines() if l.strip()]
                        self.events[s.id] = evs[-MAX_EVENTS:]
        except Exception:
            pass

    def _seed(self) -> None:
        if self.defs:
            return
        now = time.time()
        seeds = [
            AgentDef(id="studio-ops", name="Studio Operator", engine="codex", icon="sparkles", builtin=True,
                     scopes=["studio"], createdAt=now,
                     systemPrompt="You operate Automation Studio. Use the studio_ tools to inspect workflows, "
                     "run them, and read/clean/combine datasets. Prefer datasets for anything multi-step: capture "
                     "run results, dedup, project columns to prep the next workflow's input. Be concise."),
            AgentDef(id="browser-pilot", name="Browser Pilot", engine="codex", icon="globe", builtin=True,
                     scopes=["studio", "browser"], createdAt=now,
                     systemPrompt="You drive a real browser for the user. Use browser_observe to see the indexed "
                     "page, then browser_click / browser_type / browser_eval to act like a careful human. You can "
                     "also run workflows and use datasets via the studio_ tools. Go step by step and verify."),
        ]
        for s in seeds:
            self.defs[s.id] = s
        self._save_defs()

    def _save_defs(self) -> None:
        try:
            AGENTS_FILE.write_text(json.dumps([asdict(d) for d in self.defs.values()], indent=2))
        except Exception:
            pass

    def _save_sessions(self) -> None:
        try:
            SESSIONS_FILE.write_text(json.dumps([asdict(s) for s in self.sessions.values()], indent=2))
        except Exception:
            pass

    def _emit(self, sid: str, ev: dict) -> None:
        ev = {"t": round(time.time(), 3), **ev}
        arr = self.events.setdefault(sid, [])
        arr.append(ev)
        if len(arr) > MAX_EVENTS:
            del arr[: len(arr) - MAX_EVENTS]
        if ev.get("kind") == "system" and ev.get("threadId"):
            s = self.sessions.get(sid)
            if s and not s.threadId:
                s.threadId = ev["threadId"]
        if ev.get("kind") == "usage":
            s = self.sessions.get(sid)
            if s:
                s.usage = ev.get("usage")
        try:
            d = SESSIONS_DIR / sid
            d.mkdir(parents=True, exist_ok=True)
            with open(d / "transcript.jsonl", "a") as f:
                f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------------ defs API
    def list_defs(self) -> list[dict]:
        return [asdict(d) for d in sorted(self.defs.values(), key=lambda d: d.createdAt)]

    def create_def(self, body: dict) -> dict:
        engine = body.get("engine", "codex")
        if engine not in ENGINES:
            raise ValueError(f"unknown engine: {engine}")
        did = uuid.uuid4().hex[:8]
        d = AgentDef(id=did, name=body.get("name", "Agent"), engine=engine,
                     icon=body.get("icon", "sparkles"), systemPrompt=body.get("systemPrompt", ""),
                     scopes=body.get("scopes") or ["studio"], createdAt=time.time())
        self.defs[did] = d
        self._save_defs()
        return asdict(d)

    def update_def(self, did: str, body: dict) -> dict | None:
        d = self.defs.get(did)
        if not d:
            return None
        for k in ("name", "engine", "icon", "systemPrompt", "scopes"):
            if k in body:
                setattr(d, k, body[k])
        self._save_defs()
        return asdict(d)

    def delete_def(self, did: str) -> bool:
        if did in self.defs:
            del self.defs[did]
            self._save_defs()
            return True
        return False

    # ------------------------------------------------------------------ sessions API
    def list_sessions(self) -> list[dict]:
        return [asdict(s) for s in sorted(self.sessions.values(), key=lambda s: s.createdAt, reverse=True)]

    def get_session(self, sid: str) -> dict | None:
        s = self.sessions.get(sid)
        return asdict(s) if s else None

    def get_events(self, sid: str) -> list[dict]:
        return self.events.get(sid, [])

    def launch(self, agent_id: str, profile_id: str, prompt: str, watch: bool = False) -> AgentSession:
        d = self.defs.get(agent_id)
        if not d:
            raise ValueError(f"unknown agent: {agent_id}")
        if not _find_binary(d.engine):
            raise ValueError(f"{d.engine} CLI not found — install it and sign in to use this agent")
        from . import profiles
        wants_browser = "browser" in d.scopes
        if is_ephemeral(profile_id):
            profile_id, profile_name = "ephemeral", "Ephemeral"
            if wants_browser:
                raise ValueError("browser agents need a persistent profile (so they can own the browser)")
        else:
            prof = profiles.get(profile_id)
            if not prof:
                raise ValueError(f"unknown profile: {profile_id}")
            profile_name = prof["name"]
        sid = uuid.uuid4().hex[:8]
        s = AgentSession(id=sid, agentId=agent_id, agentName=d.name, engine=d.engine, scopes=d.scopes,
                         profileId=profile_id, profileName=profile_name, prompt=prompt, status="starting",
                         createdAt=time.time(), watch=bool(watch))
        self.sessions[sid] = s
        self.events[sid] = []
        self._save_sessions()
        asyncio.create_task(self._run_turn(s, prompt, resume=False))
        return s

    def steer(self, sid: str, message: str) -> dict:
        s = self.sessions.get(sid)
        if not s:
            return {"ok": False, "error": "no such session"}
        if s.status in TERMINAL:
            return {"ok": False, "error": "session has ended"}
        if s.status == "running":
            s.pendingSteers.append(message)   # delivered as the next turn when this one ends
            self._emit(sid, {"kind": "system", "text": "↩ steer queued (will run after the current turn)"})
            self._save_sessions()
            return {"ok": True, "queued": True}
        asyncio.create_task(self._run_turn(s, message, resume=True))
        return {"ok": True, "queued": False}

    async def stop(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if not s:
            return {"ok": True}
        proc = self.procs.get(sid)
        if proc and proc.pid:
            kill_tree(proc.pid)
        await self._release_browser(s)
        if s.status not in TERMINAL:
            s.status = "canceled"
            s.finishedAt = time.time()
        self._emit(sid, {"kind": "system", "text": "■ stopped by user"})
        self._save_sessions()
        return {"ok": True}

    async def shutdown(self) -> None:
        for sid, proc in list(self.procs.items()):
            if proc and proc.pid:
                kill_tree(proc.pid)
        for s in self.sessions.values():
            await self._release_browser(s)

    # ------------------------------------------------------------------ ownership
    async def _ensure_browser(self, s: AgentSession) -> None:
        if "browser" not in s.scopes or s.controlPort:
            return
        mgr = get_manager()
        res = await mgr.open_profile_session(s.profileId)  # serialises the profile + starts control-server
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "could not open the browser for this agent"))
        s.controlPort = res.get("port")

    async def _release_browser(self, s: AgentSession) -> None:
        if s.controlPort:
            try:
                await get_manager().close_profile_session(s.profileId)
            except Exception:
                pass
            s.controlPort = None

    # ------------------------------------------------------------------ engine turn
    async def _run_turn(self, s: AgentSession, prompt: str, resume: bool) -> None:
        try:
            await self._ensure_browser(s)
        except Exception as e:
            s.status = "failed"; s.error = str(e); s.finishedAt = time.time()
            self._emit(s.id, {"kind": "error", "text": str(e)})
            self._save_sessions()
            return
        s.status = "running"
        if not s.startedAt:
            s.startedAt = time.time()
        self._save_sessions()
        self._emit(s.id, {"kind": "system", "text": ("↪ " + prompt) if resume else prompt, "role": "user"})

        backend_url = f"http://127.0.0.1:{os.environ.get('AUTOMATION_PORT', '8765')}"
        d = self.defs.get(s.agentId)
        sysprompt = d.systemPrompt if d else ""
        env_pairs = {
            "AUTOMATION_BACKEND_URL": backend_url,
            "AGENT_ID": s.agentId,
            "AGENT_PROFILE_ID": s.profileId,
        }
        if s.controlPort:
            env_pairs["MCP_CONTROL_PORT"] = str(s.controlPort)

        ws = SESSIONS_DIR / s.id / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        try:
            if s.engine == "codex":
                cmd = self._codex_cmd(s, prompt, sysprompt, env_pairs, str(ws), resume)
            else:
                cmd = self._claude_cmd(s, prompt, sysprompt, env_pairs, resume)
        except Exception as e:
            self._emit(s.id, {"kind": "error", "text": f"failed to build command: {e}"})
            s.status = "failed"; s.error = str(e); s.finishedAt = time.time(); self._save_sessions()
            return

        normalize = _norm_codex if s.engine == "codex" else _norm_claude
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=str(ws), limit=16 * 1024 * 1024,
                start_new_session=(os.name == "posix"),
                env={**os.environ})
        except Exception as e:
            self._emit(s.id, {"kind": "error", "text": f"could not start {s.engine}: {e}"})
            s.status = "failed"; s.error = str(e); s.finishedAt = time.time(); self._save_sessions()
            return
        self.procs[s.id] = proc
        runids_before = set(s.runIds)
        asyncio.create_task(self._drain_stderr(s.id, proc.stderr))
        while True:
            try:
                raw = await proc.stdout.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            for ev in normalize(obj):
                self._emit(s.id, ev)
                # track runs the agent started (tool_result of studio_run_workflow carries runId)
                if ev.get("kind") == "tool_result" and ev.get("tool") in ("studio_run_workflow", ""):
                    rid = _extract_run_id(ev.get("result"))
                    if rid and rid not in s.runIds:
                        s.runIds.append(rid)
        code = await proc.wait()
        self.procs.pop(s.id, None)
        s.turns += 1
        _ = runids_before
        if code != 0 and s.status == "running":
            self._emit(s.id, {"kind": "error", "text": f"{s.engine} exited with code {code}"})
        # next steer, or go idle / done
        if s.pendingSteers:
            nxt = s.pendingSteers.pop(0)
            self._save_sessions()
            await self._run_turn(s, nxt, resume=True)
            return
        s.status = "idle"
        self._emit(s.id, {"kind": "status", "status": "idle"})
        self._save_sessions()

    async def _drain_stderr(self, sid: str, stream) -> None:
        if not stream:
            return
        while True:
            try:
                raw = await stream.readline()
            except Exception:
                break
            if not raw:
                break
            # engine stderr is mostly progress noise; surface only error-ish lines
            line = raw.decode(errors="replace").strip()
            if line and any(w in line.lower() for w in ("error", "failed", "exception", "denied")):
                self._emit(sid, {"kind": "system", "text": f"[{self.sessions[sid].engine}] {line[:200]}"})

    # ------------------------------------------------------------------ command builders
    def _mcp_env_toml(self, env_pairs: dict) -> str:
        inner = ",".join(f'{k}="{v}"' for k, v in env_pairs.items())
        return "mcp_servers.studio.env={" + inner + "}"

    def _codex_cmd(self, s, prompt, sysprompt, env_pairs, ws, resume) -> list[str]:
        binary = _find_binary("codex")
        base = _self_base()
        mcp_cmd, mcp_args = base[0], base[1:] + ["mcp"]
        full_prompt = (f"{sysprompt}\n\n---\nTask: {prompt}" if sysprompt and not resume else prompt)
        cmd = [binary, "exec", "--json", "--skip-git-repo-check",
               "--dangerously-bypass-approvals-and-sandbox", "-C", ws,
               "-c", f'mcp_servers.studio.command="{mcp_cmd}"',
               "-c", "mcp_servers.studio.args=[" + ",".join(json.dumps(a) for a in mcp_args) + "]",
               "-c", f'mcp_servers.studio.cwd="{BACKEND_DIR}"',
               "-c", self._mcp_env_toml(env_pairs)]
        if resume and s.threadId:
            cmd += ["resume", s.threadId, full_prompt]
        else:
            cmd += [full_prompt]
        return cmd

    def _claude_cmd(self, s, prompt, sysprompt, env_pairs, resume) -> list[str]:
        binary = _find_binary("claude")
        base = _self_base()
        cfg = {"mcpServers": {"studio": {"command": base[0], "args": base[1:] + ["mcp"],
                                         "cwd": BACKEND_DIR, "env": env_pairs}}}
        cfg_path = SESSIONS_DIR / s.id / "mcp.json"
        cfg_path.write_text(json.dumps(cfg))
        cmd = [binary, "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--mcp-config", str(cfg_path), "--allowedTools", "mcp__studio",
               "--permission-mode", "bypassPermissions"]
        if sysprompt:
            cmd += ["--append-system-prompt", sysprompt]
        if resume and s.threadId:
            cmd += ["--resume", s.threadId]
        return cmd


def engine_status() -> dict:
    """Which engines are installed/launchable on this machine (for the UI)."""
    return {e: {"available": _find_binary(e) is not None, "path": _find_binary(e)} for e in ENGINES}


_agents: AgentManager | None = None


def get_agents() -> AgentManager:
    global _agents
    if _agents is None:
        _agents = AgentManager()
    return _agents
