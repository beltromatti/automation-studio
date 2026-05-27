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

# Model + reasoning effort each engine runs at (the most capable settings).
CODEX_MODEL = "gpt-5.5"
CODEX_EFFORT = "xhigh"        # codex: model_reasoning_effort (minimal|low|medium|high|xhigh)
CLAUDE_MODEL = "claude-opus-4-7"
CLAUDE_EFFORT = "max"        # claude: --effort (low|medium|high|max)


def _find_binary(engine: str) -> str | None:
    """Resolve the user's installed engine CLI, cross-platform (delegates to the
    central, fault-tolerant dependency gateway). Finder-launched apps don't inherit
    the shell PATH, so it probes the standard install locations too."""
    from . import deps
    return deps.find_engine(engine)


@dataclass
class AgentDef:
    # An agent is engine-agnostic: the engine (codex|claude) is chosen per session
    # at launch, not baked into the definition.
    id: str
    name: str
    icon: str = "sparkles"
    description: str = ""             # optional, human-facing (shown on the card)
    systemPrompt: str = ""            # the agent's skills / role (injected)
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
    status: str                       # queued|starting|running|waiting|scheduled|done|failed|canceled
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
    # canonical inbox: notifications wake the agent (e.g. a detached workflow it
    # launched finished). Each: {id, kind, payload, createdAt, delivered}.
    notifications: list[dict] = field(default_factory=list)
    scheduledAt: float | None = None  # when status == "scheduled": the future wake time


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


_RUNNING = {"starting", "queued", "running"}  # a turn is in flight (can't reactivate, only queue)
_AT_REST = {"done", "failed", "canceled", "waiting", "scheduled"}  # reactivatable; waiting/scheduled also auto-resume

# Canonical, TOOL-AGNOSTIC preamble prepended to every agent's system prompt. It
# defines the agent's shape/role inside Automation Studio without naming specific
# tools (those are per-agent — the MCP tool list/descriptions tell the agent what
# it actually has). Composed with runtime facts (the session workspace path).
STUDIO_PREAMBLE = (
    "You are an autonomous agent operating INSIDE Automation Studio — a desktop app for building and "
    "running real browser and data automations. You act through the Studio tools exposed to you; their "
    "descriptions tell you what each does. Those tools are your primary way to work: they're integrated "
    "with the app, visible to the user, persistent and safe. You also have your own native abilities (a "
    "shell, file read/write, web). Prefer the Studio tools for anything about data, workflows, runs or the "
    "browser; fall back to native abilities only when nothing in Studio fits.\n"
    "Your session has a private working folder at {ws} (it's your working directory). Keep temporary "
    "scripts you write+run, downloaded or prepared data, and scratch artifacts THERE by default. You may "
    "read or write elsewhere on the machine if a task genuinely needs it, but default to your session "
    "folder.\n"
    "Running a workflow is DETACHED by default: you get a runId and can keep working, poll its status/logs, "
    "or simply end your turn. If you end your turn while a workflow YOU launched is still running, you are "
    "automatically paused and then WOKEN with a notification when it finishes — so you never need to wait "
    "explicitly or busy-loop. When you have nothing left to do and nothing of yours is running, just end "
    "your turn."
)


class _Stopped(Exception):
    """Raised when a session is stopped while it was queued waiting for a profile."""


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
                    s.controlPort = None      # no browser survives a restart
                    s.pendingSteers = []
                    if s.status in ("idle", "waiting"):
                        # legacy rest state, or was waiting on a run that didn't survive
                        # the restart → done (reactivatable; its lock is gone anyway).
                        s.status = "done"
                    elif s.status == "scheduled":
                        pass                  # the Timeline re-fires it at scheduledAt
                    elif s.status not in TERMINAL:
                        # interrupted mid-flight by a restart — resources are gone, but
                        # the native thread persists, so it's still reactivatable.
                        s.status = "failed"
                        s.error = s.error or "interrupted (backend restarted)"
                        s.finishedAt = s.finishedAt or time.time()
                    self.sessions[s.id] = s
                    tf = SESSIONS_DIR / s.id / "transcript.jsonl"
                    if tf.exists():
                        evs = [json.loads(l) for l in tf.read_text().splitlines() if l.strip()]
                        self.events[s.id] = evs[-MAX_EVENTS:]
        except Exception:
            pass

    def _seed(self) -> None:
        now = time.time()
        seeds = [
            AgentDef(id="studio-ops", name="Studio Operator", icon="sparkles", builtin=True,
                     scopes=["studio"], createdAt=now,
                     description="Runs and chains your workflows and curates the data layer — capture run "
                     "results into datasets, dedup, and project tidy inputs for the next step.",
                     systemPrompt="You operate Automation Studio. Use the studio_ tools to inspect workflows, "
                     "run them, and read/clean/combine datasets. Prefer datasets for anything multi-step: capture "
                     "run results, dedup, project columns to prep the next workflow's input. Be concise."),
            AgentDef(id="browser-pilot", name="Browser Pilot", icon="globe", builtin=True,
                     scopes=["studio", "browser"], createdAt=now,
                     description="Drives a real browser for you and can launch workflows on the same session — "
                     "observe the page, click, type and extract like a careful human, step by step.",
                     systemPrompt="You drive a real browser for the user. browser_observe gives an indexed snapshot "
                     "(it sees into shadow DOM and iframes — '@shadow'/'@iframe' mark those); act with browser_click / "
                     "browser_type by [index]. When two controls look alike (e.g. a real in-card button vs a duplicate "
                     "sticky-header/nav one), use browser_inspect to read their xpath/coords/frame and pick the right "
                     "one (e.g. the one whose xpath is under '/main'). Use browser_wait for late/dynamic elements "
                     "(including shadow-DOM dialogs), browser_scroll to='top' to clear sticky headers, and browser_eval "
                     "for custom extraction (main-frame light DOM only — shadow/iframe needs observe+click). You can "
                     "also run workflows and use datasets via the studio_ tools. Go step by step and verify."),
        ]
        changed = False
        for s in seeds:
            existing = self.defs.get(s.id)
            if not existing:
                self.defs[s.id] = s            # first run: add the built-in
                changed = True
            elif existing.builtin and not existing.description:
                existing.description = s.description  # backfill on update, respecting user edits
                changed = True
        if changed:
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
        did = uuid.uuid4().hex[:8]
        d = AgentDef(id=did, name=body.get("name", "Agent"),
                     icon=body.get("icon", "sparkles"), description=body.get("description", ""),
                     systemPrompt=body.get("systemPrompt", ""),
                     scopes=body.get("scopes") or ["studio"], createdAt=time.time())
        self.defs[did] = d
        self._save_defs()
        return asdict(d)

    def update_def(self, did: str, body: dict) -> dict | None:
        d = self.defs.get(did)
        if not d:
            return None
        for k in ("name", "icon", "description", "systemPrompt", "scopes"):
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
        # Most-recent activity first: live sessions (starting/queued/running) on top,
        # then rested ones by when they last did something. Coherent with runs.
        def key(s: AgentSession) -> tuple:
            live = s.status not in TERMINAL
            return (1 if live else 0, s.finishedAt or s.startedAt or s.createdAt or 0)
        return [asdict(s) for s in sorted(self.sessions.values(), key=key, reverse=True)]

    def get_session(self, sid: str) -> dict | None:
        s = self.sessions.get(sid)
        return asdict(s) if s else None

    def get_events(self, sid: str) -> list[dict]:
        return self.events.get(sid, [])

    def launch(self, agent_id: str, profile_id: str, prompt: str, watch: bool = False,
               engine: str = "codex") -> AgentSession:
        d = self.defs.get(agent_id)
        if not d:
            raise ValueError(f"unknown agent: {agent_id}")
        engine = (engine or "codex").strip().lower()
        if engine not in ENGINES:
            raise ValueError(f"unknown engine: {engine}")
        if not _find_binary(engine):
            raise ValueError(f"{engine} CLI not found — install it and sign in to use this agent")
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
        s = AgentSession(id=sid, agentId=agent_id, agentName=d.name, engine=engine, scopes=d.scopes,
                         profileId=profile_id, profileName=profile_name, prompt=prompt, status="starting",
                         createdAt=time.time(), watch=bool(watch))
        self.sessions[sid] = s
        self.events[sid] = []
        self._save_sessions()
        asyncio.create_task(self._run_turn(s, prompt, resume=False))
        return s

    def steer(self, sid: str, message: str) -> dict:
        """Send a message to a session. If a turn is in flight, it's queued for the
        next turn; otherwise it REACTIVATES the session (resumes the native thread).
        Works from done/failed/canceled — sessions are continuable."""
        s = self.sessions.get(sid)
        if not s:
            return {"ok": False, "error": "no such session"}
        if s.status in _RUNNING:
            s.pendingSteers.append(message)   # delivered as the next turn when this one ends
            self._emit(sid, {"kind": "system", "text": "↩ message queued — runs after the current turn"})
            self._save_sessions()
            return {"ok": True, "queued": True}
        asyncio.create_task(self._run_turn(s, message, resume=True))
        return {"ok": True, "queued": False}

    async def stop(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if not s:
            return {"ok": True}
        s.status = "canceled"             # signals a queued-acquire loop to abort
        s.pendingSteers.clear()
        proc = self.procs.get(sid)
        if proc and proc.pid:
            kill_tree(proc.pid)
        await self._release_browser(s)
        s.finishedAt = time.time()
        self._emit(sid, {"kind": "system", "text": "■ stopped"})
        self._save_sessions()
        return {"ok": True}

    async def shutdown(self) -> None:
        for sid, proc in list(self.procs.items()):
            if proc and proc.pid:
                kill_tree(proc.pid)
        for s in self.sessions.values():
            await self._release_browser(s)

    # ------------------------------------------------------------------ context / notifications
    def _compose_sysprompt(self, s: AgentSession, d: "AgentDef | None") -> str:
        """The canonical Studio preamble (+ this session's workspace path) prepended
        to the agent's own role/skills prompt."""
        ws = str(SESSIONS_DIR / s.id / "workspace")
        head = STUDIO_PREAMBLE.format(ws=ws)
        role = (d.systemPrompt if d else "") or ""
        return f"{head}\n\n{role}".strip() if role else head

    def _run_active(self, rid: str) -> bool:
        try:
            from .manager import get_manager, ACTIVE
            r = get_manager().get(rid)
            return bool(r and r.get("status") in (ACTIVE | {"queued"}))
        except Exception:
            return False

    def _owned_active_runs(self, s: AgentSession) -> list[str]:
        return [rid for rid in s.runIds if self._run_active(rid)]

    def _pending_notes(self, s: AgentSession) -> list[dict]:
        return [n for n in s.notifications if not n.get("delivered")]

    def notify(self, sid: str, kind: str, payload: dict) -> None:
        """Canonical notification entry point: append to the agent's inbox and wake
        it. Used by the RunManager when a detached workflow the agent launched
        finishes; extensible to other event kinds later (mid-turn or at-rest)."""
        s = self.sessions.get(sid)
        if not s:
            return
        note = {"id": uuid.uuid4().hex[:8], "kind": kind, "payload": payload,
                "createdAt": time.time(), "delivered": False}
        s.notifications.append(note)
        summary = self._note_summary(note)
        self._emit(sid, {"kind": "system", "text": f"🔔 {summary}"})
        self._save_sessions()
        self._maybe_wake(s)

    @staticmethod
    def _note_summary(n: dict) -> str:
        p = n.get("payload") or {}
        if n.get("kind") == "workflow_finished":
            rid, st = p.get("runId"), p.get("status")
            extra = f" — {p.get('rows')} rows" if p.get("rows") is not None else (f" — {p.get('error')}" if p.get("error") else "")
            return f"workflow {p.get('workflow') or rid} {st}{extra} (run {rid})"
        return n.get("kind", "notification")

    def _wake_prompt(self, notes: list[dict]) -> str:
        lines = ["You were woken because:"]
        for n in notes:
            lines.append(f"- {self._note_summary(n)}")
        lines.append("Check the result (e.g. studio_run_result / studio_run_status / studio_run_logs), then "
                     "continue what you were doing, start the next step, or end your turn if there's nothing left.")
        return "\n".join(lines)

    def _maybe_wake(self, s: AgentSession) -> None:
        """If the agent is at rest (not mid-turn, not user-canceled), start a turn to
        consume pending notifications."""
        if s.status in _RUNNING or s.status == "canceled":
            return  # mid-turn: consumed at turn end; canceled: user stopped it
        if not self._pending_notes(s):
            return
        asyncio.create_task(self._wake(s))

    async def _wake(self, s: AgentSession) -> None:
        if s.status in _RUNNING or s.status == "canceled":
            return
        notes = self._pending_notes(s)
        if not notes:
            return
        for n in notes:
            n["delivered"] = True
        self._save_sessions()
        await self._run_turn(s, self._wake_prompt(notes), resume=True)

    # ------------------------------------------------------------------ ownership
    async def _ensure_browser(self, s: AgentSession) -> None:
        """Acquire the agent's browser for a turn. A persistent profile is taken
        through the RunManager's single per-profile gate, QUEUEING (not failing)
        until it's free of any run / manual session / other agent. Held only for
        the duration of the turn; released at rest."""
        if "browser" not in s.scopes or s.controlPort:
            return
        mgr = get_manager()
        waited = False
        while not mgr.claim_profile(s.profileId):
            if s.status == "canceled":         # stopped while queued
                raise _Stopped()
            if not waited:
                waited = True
                s.status = "queued"
                self._emit(s.id, {"kind": "status", "status": "queued"})
                self._emit(s.id, {"kind": "system", "text": f"⏳ waiting for profile “{s.profileName}” to be free…"})
                self._save_sessions()
            await asyncio.sleep(0.4)
        try:
            res = await mgr.open_agent_browser(s.id, s.profileId, headed=s.watch)
        finally:
            mgr.unclaim_profile(s.profileId)
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "could not open the browser for this agent"))
        s.controlPort = res.get("port")
        if s.status == "canceled":             # stopped during acquire → undo
            await self._release_browser(s)
            raise _Stopped()

    async def _release_browser(self, s: AgentSession) -> None:
        if s.controlPort or s.id in get_manager().agent_browsers:
            try:
                await get_manager().release_agent_browser(s.id)
            except Exception:
                pass
            s.controlPort = None

    # ------------------------------------------------------------------ engine turn
    async def _run_turn(self, s: AgentSession, prompt: str, resume: bool, _retry: bool = False) -> None:
        s.status = "starting"
        s.error = None
        s.finishedAt = None
        self._emit(s.id, {"kind": "system", "text": ("↪ " + prompt) if resume else prompt, "role": "user"})
        self._save_sessions()
        try:
            await self._ensure_browser(s)   # may queue for a busy profile
        except _Stopped:
            return  # stopped while queued; status is already "canceled"
        except Exception as e:
            s.status = "failed"; s.error = str(e); s.finishedAt = time.time()
            self._emit(s.id, {"kind": "error", "text": str(e)})
            self._save_sessions()
            return
        s.status = "running"
        if not s.startedAt:
            s.startedAt = time.time()
        self._save_sessions()

        backend_url = f"http://127.0.0.1:{os.environ.get('AUTOMATION_PORT', '8765')}"
        d = self.defs.get(s.agentId)
        sysprompt = self._compose_sysprompt(s, d)
        env_pairs = {
            "AUTOMATION_BACKEND_URL": backend_url,
            "AGENT_ID": s.agentId,            # agent DEFINITION id
            "AGENT_SESSION_ID": s.id,         # this SESSION (runs are owned by + notify the session)
            "AGENT_PROFILE_ID": s.profileId,
        }
        if s.controlPort:
            env_pairs["MCP_CONTROL_PORT"] = str(s.controlPort)

        ws = SESSIONS_DIR / s.id / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        resume_attempted = resume and bool(s.threadId)  # did we use the engine's resume path?
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
        turn_flags: dict = {}
        drain = asyncio.create_task(self._drain_stderr(s.id, proc.stderr, turn_flags))
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
                # a top-level error event (codex turn.failed / claude result error)
                # means the agentic loop itself broke — distinct from a tool error.
                if ev.get("kind") == "error":
                    turn_flags["turn_error"] = True
                    turn_flags["turn_error_msg"] = ev.get("text")
                # track runs the agent started (tool_result of studio_run_workflow carries runId)
                if ev.get("kind") == "tool_result" and ev.get("tool") in ("studio_run_workflow", ""):
                    rid = _extract_run_id(ev.get("result"))
                    if rid and rid not in s.runIds:
                        s.runIds.append(rid)
        code = await proc.wait()
        try:
            await asyncio.wait_for(drain, timeout=2)
        except Exception:
            pass
        self.procs.pop(s.id, None)
        s.turns += 1
        # Resume failed (e.g. the prior turn was interrupted before the engine
        # saved its rollout) → fall back to a fresh turn so the message still runs.
        if code != 0 and resume_attempted and turn_flags.get("resume_failed") and not _retry and s.status != "canceled":
            self._emit(s.id, {"kind": "system", "text": "↻ couldn't resume the previous thread — starting a fresh session for this message"})
            s.threadId = None
            await self._run_turn(s, prompt, resume=False, _retry=True)
            return
        if s.status == "canceled":
            await self._release_browser(s)
            self._save_sessions()
            return
        # A message queued during this turn → continue immediately, KEEPING the
        # browser (no restart between back-to-back turns).
        if s.pendingSteers:
            nxt = s.pendingSteers.pop(0)
            self._save_sessions()
            await self._run_turn(s, nxt, resume=True)
            return
        # A notification arrived during this turn → consume it as the next turn,
        # KEEPING the browser (same as a steer).
        if self._pending_notes(s) and not (code != 0 or turn_flags.get("turn_error")):
            await self._wake(s)
            return
        # Decide the resting status. Engine crash (non-zero exit / top-level turn
        # error) → FAILURE. Otherwise: if a workflow this agent launched is still
        # running, it OWNS the profile lock and must keep its browser (the run is
        # attached to it) → WAITING, to be woken by the run's completion
        # notification. Only when nothing of its own is running do we release the
        # lock and go DONE. All three are reactivatable.
        if code != 0 or turn_flags.get("turn_error"):
            await self._release_browser(s)
            s.status = "failed"
            tail = "\n".join(turn_flags.get("stderr_tail") or [])
            s.error = (turn_flags.get("turn_error_msg") or _short(tail, 500)
                       or f"the {s.engine} agent ended unexpectedly (exit code {code})")
            if not turn_flags.get("turn_error"):  # surface a crash that produced no error event
                self._emit(s.id, {"kind": "error",
                                  "text": f"the {s.engine} agent crashed mid-turn (exit code {code}). {s.error}"[:600]})
        elif self._owned_active_runs(s):
            s.status = "waiting"   # keep the browser/profile lock; woken on completion
            self._emit(s.id, {"kind": "system",
                              "text": "⏸ turn ended with a workflow still running — paused; you'll be woken when it finishes"})
        else:
            await self._release_browser(s)
            s.status = "done"
        s.finishedAt = time.time()
        self._emit(s.id, {"kind": "status", "status": s.status})
        self._save_sessions()

    async def _drain_stderr(self, sid: str, stream, flags: dict | None = None) -> None:
        if not stream:
            return
        while True:
            try:
                raw = await stream.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            low = line.lower()
            if flags is not None:
                # keep a rolling tail of stderr to use as the error message on a crash
                tail = flags.setdefault("stderr_tail", [])
                tail.append(line[:300])
                if len(tail) > 15:
                    del tail[0]
                # detect a failed resume so the caller can retry the message fresh
                if "no rollout" in low or ("resume" in low and ("fail" in low or "not found" in low)):
                    flags["resume_failed"] = True
            # engine stderr is mostly progress noise; surface only error-ish lines
            if any(w in low for w in ("error", "failed", "exception", "denied", "traceback", "panic")):
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
               "-m", CODEX_MODEL,
               "-c", f'model_reasoning_effort="{CODEX_EFFORT}"',
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
               "--model", CLAUDE_MODEL, "--effort", CLAUDE_EFFORT,
               "--mcp-config", str(cfg_path), "--allowedTools", "mcp__studio",
               "--permission-mode", "bypassPermissions"]
        if sysprompt:
            cmd += ["--append-system-prompt", sysprompt]
        if resume and s.threadId:
            cmd += ["--resume", s.threadId]
        return cmd


def engine_status() -> dict:
    """Which engines are installed/launchable on this machine, with install help."""
    from . import deps
    return deps.engine_status()


_agents: AgentManager | None = None


def get_agents() -> AgentManager:
    global _agents
    if _agents is None:
        _agents = AgentManager()
    return _agents
