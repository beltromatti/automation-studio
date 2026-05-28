"""RunManager — owns every run and its browser process tree.

Python port of the original TypeScript orchestrator. For each run it spawns a
humanbrowser control-server (one Chrome session) and the workflow attached to it,
parses the workflow's JSON run-events for live status/progress, and enforces
concurrency + per-profile serialisation. Process cleanup is cross-platform via
psutil (kill the whole tree; reap orphaned trees), so browsers can never be
orphaned and overwhelm the machine — the hard-won crash-safety, carried over.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import aiohttp
import psutil

from humanbrowser.config import data_dir

RS = "\x1e"
PORT_BASE = 8810
MAX_LOG_LINES = 600
# Concurrency is governed per-profile (a persistent profile runs one run at a
# time; everything else runs in parallel), not by a user-set number. This is only
# a machine-safety ceiling on simultaneous live browsers so a burst of ephemeral
# runs can't spawn unbounded Chrome processes and panic the OS.
GLOBAL_SAFETY_CAP = 8
REAP_INTERVAL = 10  # seconds
TIMELINE_INTERVAL = 3  # seconds — how often the Timeline checks for due scheduled items
TERMINAL = {"succeeded", "failed", "canceled"}
ACTIVE = {"starting", "running", "controlled"}
# The special "ephemeral" profile: a fresh throwaway dir per run, deleted after.
# Not serialised — many ephemeral runs go in parallel. ("temporary" = legacy id.)
EPHEMERAL_IDS = {"ephemeral", "temporary", ""}


def is_ephemeral(profile_id: str | None) -> bool:
    return (profile_id or "") in EPHEMERAL_IDS


def _port_listening(port: int) -> bool:
    """True if something is already accepting connections on 127.0.0.1:port. Used to
    skip a candidate control-server port that ANY process (a manually-opened profile
    session, an agent browser, another app) already holds — otherwise a run's server
    fails to bind and the workflow silently attaches to whoever is on that port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _free_ephemeral_port() -> int:
    """An OS-assigned free port, as a fallback when the fixed range is exhausted."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

DATA = data_dir()
RUNS_DIR = DATA / "runs"
RUNS_INDEX = DATA / "runs.json"
SETTINGS_FILE = DATA / "settings.json"
PROFILES_DIR = DATA / "profiles"
EPHEMERAL_DIR = PROFILES_DIR / "_ephemeral"


def _self_base() -> list[str]:
    """Command prefix to re-invoke THIS backend in a sub-mode. Frozen → the exe
    itself; dev → ``python -m orchestrator``."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "orchestrator"]


def kill_tree(pid: int | None) -> None:
    """Kill a process and ALL its descendants (python → node driver → Chrome)."""
    if not pid:
        return
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = parent.children(recursive=True)
    procs.append(parent)
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=3)


@dataclass
class Run:
    id: str
    workflowId: str
    workflowName: str
    params: dict
    status: str
    watch: bool
    createdAt: float
    profileKey: str
    profileId: str = "temporary"
    profileName: str = "Temporary"
    profileDir: str = ""
    browserOpen: bool = False
    startedAt: float | None = None
    finishedAt: float | None = None
    progress: dict | None = None
    lastUrl: str | None = None
    csvPath: str | None = None
    rows: int | None = None
    error: str | None = None
    serverPort: int | None = None
    datasetId: str | None = None  # optional: append this run's result here on success
    attachPort: int | None = None  # if set, attach to an agent's existing control-server (shared browser)
    agentId: str | None = None     # the agent session that launched this run, if any
    inputDatasetId: str | None = None  # a dataset fed as the run's input list (list-consuming workflows)
    startAt: float | None = None   # when status == "scheduled": fire (→ queued) at this time
    everySeconds: float | None = None  # recurring: re-arm the next occurrence on fire


class RunManager:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self.runs: dict[str, Run] = {}
        self.procs: dict[str, dict[str, Any]] = {}  # id -> {server, work} asyncio procs
        self.logs: dict[str, list[str]] = {}
        self.flags: dict[str, dict] = {}  # id -> {canceled, takingControl}
        self.sessions: dict[str, dict] = {}  # profileId -> {proc, port} (manual open sessions)
        self.agent_browsers: dict[str, dict] = {}  # agent session id -> {proc, port, pid} (agent-owned)
        self._acquiring: set[str] = set()  # persistent profile ids being claimed right now (race guard)
        self.settings = {"maxConcurrency": 1}
        self._http: aiohttp.ClientSession | None = None
        self._reaper_task: asyncio.Task | None = None
        self._timeline_task: asyncio.Task | None = None
        self.reap_strays(startup=True)
        self._load()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._http = aiohttp.ClientSession()
        self._reaper_task = asyncio.create_task(self._reaper_loop())
        self._timeline_task = asyncio.create_task(self._timeline_loop())

    async def stop(self) -> None:
        for t in (self._reaper_task, getattr(self, "_timeline_task", None)):
            if t:
                t.cancel()
        self.kill_all()
        if self._http:
            await self._http.close()

    async def _reaper_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(REAP_INTERVAL)
                self.reap_strays(startup=False)
        except asyncio.CancelledError:
            pass

    async def _timeline_loop(self) -> None:
        """The Timeline: the time-based trigger engine. On a tight cadence it
        releases due scheduled runs into the queue (where the Studio Scheduler then
        grants the profile lock) and wakes due scheduled agents. Distinct from the
        Studio Scheduler (schedule(), which allocates locks); this only deals with
        WHEN, not WHO-gets-the-profile."""
        try:
            while True:
                await asyncio.sleep(TIMELINE_INTERVAL)
                try:
                    self.fire_due_runs()
                except Exception:
                    pass
                try:
                    from .agents import get_agents
                    get_agents().fire_due_wakes()
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    def fire_due_runs(self) -> None:
        """Release scheduled runs whose time has come into the queue; re-arm
        recurring ones for their next occurrence."""
        now = time.time()
        fired = False
        for run in list(self.runs.values()):
            if run.status != "scheduled" or not run.startAt or run.startAt > now:
                continue
            if run.everySeconds and run.everySeconds > 0:
                # re-arm the next occurrence at a steady cadence from the planned time
                nxt = run.startAt + run.everySeconds
                while nxt <= now:
                    nxt += run.everySeconds
                try:
                    self.create(run.workflowId, run.params, watch=run.watch, profile_id=run.profileId,
                                dataset_id=run.datasetId, agent_id=run.agentId,
                                input_dataset_id=run.inputDatasetId, start_at=nxt,
                                every_seconds=run.everySeconds)
                except Exception:
                    pass
                run.everySeconds = None  # this occurrence is now a one-shot
            run.status = "queued"
            fired = True
        if fired:
            self._save()
            self.schedule()

    def kill_all(self) -> None:
        for p in self.procs.values():
            for key in ("work", "server"):
                proc = p.get(key)
                if proc and proc.pid:
                    kill_tree(proc.pid)
        for s in self.sessions.values():
            if s.get("proc"):
                kill_tree(s["proc"].pid)
        for b in self.agent_browsers.values():
            if b.get("proc"):
                kill_tree(b["proc"].pid)
        self.reap_strays(startup=True)

    # ------------------------------------------------------------------ profile gate
    # A persistent profile is used by AT MOST ONE owner at a time, across runs,
    # manual login sessions AND agent-owned browsers. Everything consults this one
    # predicate so they queue for each other instead of colliding.
    def _persistent_profile_busy(self, pid: str) -> bool:
        if is_ephemeral(pid):
            return False
        if pid in self.sessions:                       # a manual login window
            return True
        if pid in self._acquiring:                     # an owner mid-acquire (race guard)
            return True
        if any(b.get("pid") == pid for b in self.agent_browsers.values()):  # an agent owns it
            return True
        return any(r.status in ACTIVE and r.profileId == pid and not r.attachPort
                   for r in self.runs.values())        # an active (non-attached) run

    def claim_profile(self, pid: str) -> bool:
        """Atomically (single event-loop step, no await) claim a free persistent
        profile. Returns False if it's busy. Pair with unclaim_profile/open."""
        if self._persistent_profile_busy(pid):
            return False
        self._acquiring.add(pid)
        return True

    def unclaim_profile(self, pid: str) -> None:
        self._acquiring.discard(pid)

    async def open_agent_browser(self, sid: str, pid: str, headed: bool) -> dict:
        """Open a control-server on a persistent profile's master dir, owned by an
        agent session. Caller must have claim_profile()'d ``pid`` first."""
        from . import profiles
        port = self._alloc_port()
        profiles.clear_locks(pid)
        master = str(profiles.master_dir(pid))
        cmd = _self_base() + ["control-server", "--port", str(port), "--profile", master]
        if not headed:
            cmd.append("--headless")
        proc = await self._spawn(cmd)
        self.agent_browsers[sid] = {"proc": proc, "port": port, "pid": pid}
        if not await self._wait_ready(port, 25):
            kill_tree(proc.pid)
            self.agent_browsers.pop(sid, None)
            return {"ok": False, "error": "browser failed to start"}
        profiles.touch(pid)
        return {"ok": True, "port": port}

    async def release_agent_browser(self, sid: str) -> None:
        """Close an agent-owned control-server gracefully (flush the profile DBs +
        free the lock), then let queued runs/agents proceed."""
        from . import profiles
        b = self.agent_browsers.pop(sid, None)
        if not b:
            return
        try:
            await self._server_post(b["port"], "/shutdown")
        except Exception:
            pass
        try:
            await asyncio.wait_for(b["proc"].wait(), timeout=10)
        except Exception:
            pass
        kill_tree(b["proc"].pid)
        if not is_ephemeral(b["pid"]):
            profiles.clear_locks(b["pid"])
        self.schedule()  # a queued run/agent on this profile can now start

    # ------------------------------------------------------------------ profile sessions
    async def open_profile_session(self, pid: str) -> dict:
        """Open a headed browser on a profile's MASTER dir (no workflow) so the
        user can log in / set things up; changes persist to the profile."""
        from . import profiles
        prof = profiles.get(pid)
        if not prof:
            return {"ok": False, "error": "unknown profile"}
        if pid in self.sessions:
            return {"ok": True, "port": self.sessions[pid]["port"]}
        # one owner at a time per profile: refuse if a run or an agent is using it
        if self._persistent_profile_busy(pid):
            return {"ok": False, "error": "profile is busy (a run or an agent is using it) — wait for it to finish"}
        port = self._alloc_port()
        profiles.clear_locks(pid)  # also mkdir's the master + drops any stale lock
        master = str(profiles.master_dir(pid))
        cmd = _self_base() + ["control-server", "--port", str(port), "--profile", master]
        proc = await self._spawn(cmd)
        self.sessions[pid] = {"proc": proc, "port": port}
        asyncio.create_task(self._session_watch(pid, proc))
        if not await self._wait_ready(port, 25):
            kill_tree(proc.pid)
            self.sessions.pop(pid, None)
            return {"ok": False, "error": "browser failed to start"}
        try:
            await self._server_post(port, "/goto", {"url": "about:blank"})
        except Exception:
            pass
        profiles.touch(pid)
        return {"ok": True, "port": port}

    async def _session_watch(self, pid: str, proc) -> None:
        try:
            await proc.wait()
        finally:
            self.sessions.pop(pid, None)

    async def close_profile_session(self, pid: str) -> dict:
        from . import profiles
        s = self.sessions.get(pid)
        if not s:
            return {"ok": True}
        try:
            await self._server_post(s["port"], "/shutdown")
        except Exception:
            pass
        # let the clean close flush the profile's DBs + free the lock before killing
        try:
            await asyncio.wait_for(s["proc"].wait(), timeout=10)
        except Exception:
            pass
        kill_tree(s["proc"].pid)
        self.sessions.pop(pid, None)
        profiles.clear_locks(pid)
        self.schedule()  # runs queued behind this profile can now start
        return {"ok": True}

    def open_session_ids(self) -> list[str]:
        return list(self.sessions.keys())

    # ------------------------------------------------------------------ reaping
    def reap_strays(self, startup: bool) -> None:
        """Kill our browser process trees that have been orphaned (reparented to
        init, ppid<=1) — Chrome under our profiles dir and patchright node drivers.
        Matched strictly by path, so the user's personal Chrome is never touched.
        On startup, clear all of ours (clean slate); otherwise only orphans, so
        active runs are never disturbed."""
        me = os.getpid()
        prof = str(PROFILES_DIR)
        for p in psutil.process_iter(["pid", "ppid", "cmdline"]):
            try:
                if p.info["pid"] == me:
                    continue
                cl = " ".join(p.info.get("cmdline") or [])
                if not cl:
                    continue
                ours = (prof in cl) or ("patchright/driver" in cl) or ("patchright\\driver" in cl)
                if not ours:
                    continue
                if startup or (p.info.get("ppid") or 0) <= 1:
                    psutil.Process(p.info["pid"]).kill()
            except psutil.Error:
                continue

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            if SETTINGS_FILE.exists():
                self.settings.update(json.loads(SETTINGS_FILE.read_text()))
        except Exception:
            pass
        migrated = False
        try:
            if RUNS_INDEX.exists():
                for d in json.loads(RUNS_INDEX.read_text()):
                    fields = {k: d[k] for k in Run.__dataclass_fields__ if k in d}
                    r = Run(**fields)
                    # Legacy runs stored timestamps in MILLISECONDS (~1.78e12); the
                    # current code uses seconds (~1.78e9). Migrate so "X ago" and the
                    # duration display correctly instead of "-1777…s".
                    for attr in ("createdAt", "startedAt", "finishedAt"):
                        v = getattr(r, attr, None)
                        if isinstance(v, (int, float)) and v > 1e11:
                            setattr(r, attr, v / 1000.0)
                            migrated = True
                    if r.status == "scheduled":
                        pass  # future scheduled run survives a restart; the Timeline re-fires it
                    elif r.status not in TERMINAL:
                        r.status = "failed"
                        r.error = r.error or "interrupted (backend restarted)"
                        r.browserOpen = False
                        r.serverPort = None
                        r.finishedAt = r.finishedAt or time.time()
                    self.runs[r.id] = r
                    lf = RUNS_DIR / r.id / "events.log"
                    if lf.exists():
                        self.logs[r.id] = lf.read_text(errors="replace").splitlines()[-MAX_LOG_LINES:]
        except Exception:
            pass
        if migrated:
            self._save()  # persist the seconds migration so it's a one-time fix

    def _save(self) -> None:
        try:
            RUNS_INDEX.write_text(json.dumps([asdict(r) for r in self.runs.values()], indent=2))
            SETTINGS_FILE.write_text(json.dumps(self.settings))
        except Exception:
            pass

    # ------------------------------------------------------------------ logging
    def _log(self, rid: str, line: str) -> None:
        if not line:
            return
        arr = self.logs.setdefault(rid, [])
        arr.append(line)
        if len(arr) > MAX_LOG_LINES:
            del arr[: len(arr) - MAX_LOG_LINES]
        try:
            (RUNS_DIR / rid).mkdir(parents=True, exist_ok=True)
            with open(RUNS_DIR / rid / "events.log", "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def get_logs(self, rid: str) -> list[str]:
        return self.logs.get(rid, [])

    # ------------------------------------------------------------------ public API
    @staticmethod
    def _activity_key(r: Run) -> tuple:
        """Order by most-recent activity: live runs (queued/starting/running/
        controlled) first, then finished ones — each newest-activity first. Coherent
        with how agent sessions are ordered."""
        live = r.status not in TERMINAL
        return (1 if live else 0, r.finishedAt or r.startedAt or r.createdAt or 0)

    def list(self) -> list[dict]:
        return [asdict(r) for r in sorted(self.runs.values(), key=self._activity_key, reverse=True)]

    def get(self, rid: str) -> dict | None:
        r = self.runs.get(rid)
        return asdict(r) if r else None

    def get_settings(self) -> dict:
        return self.settings

    def set_settings(self, s: dict) -> dict:
        if isinstance(s.get("maxConcurrency"), (int, float)):
            self.settings["maxConcurrency"] = max(1, min(MAX_CONCURRENCY, int(s["maxConcurrency"])))
        self._save()
        self.schedule()
        return self.settings

    def create(self, workflow_id: str, params: dict, watch: bool = False,
               profile_id: str = "ephemeral", dataset_id: str | None = None,
               attach_port: int | None = None, agent_id: str | None = None,
               input_dataset_id: str | None = None, start_at: float | None = None,
               every_seconds: float | None = None) -> Run:
        from .registry import get_workflow
        from . import profiles
        wf = get_workflow(workflow_id)
        if not wf:
            raise ValueError(f"unknown workflow: {workflow_id}")
        for p in wf.params:
            if p.required and not params.get(p.name):
                raise ValueError(f"missing required parameter: {p.label}")
        if is_ephemeral(profile_id):
            profile_id, profile_name = "ephemeral", "Ephemeral"
        else:
            prof = profiles.get(profile_id)
            if not prof:
                raise ValueError(f"unknown profile: {profile_id}")
            profile_name = prof["name"]
        rid = uuid.uuid4().hex[:8]
        # A future start time parks the run as "scheduled" — the Timeline flips it to
        # "queued" when due, then the Studio Scheduler grants the profile lock as
        # normal. A scheduled run holds no lock (it isn't ACTIVE) until it fires.
        scheduled = bool(start_at) and start_at > time.time() + 1
        run = Run(id=rid, workflowId=workflow_id, workflowName=wf.name, params=params,
                  status="scheduled" if scheduled else "queued", watch=bool(watch), createdAt=time.time(),
                  profileKey=profile_id, profileId=profile_id, profileName=profile_name,
                  datasetId=dataset_id or None, attachPort=attach_port or None, agentId=agent_id or None,
                  inputDatasetId=input_dataset_id or None,
                  startAt=(start_at if scheduled else None), everySeconds=every_seconds or None)
        self.runs[rid] = run
        (RUNS_DIR / rid).mkdir(parents=True, exist_ok=True)
        self._save()
        if not scheduled:
            self.schedule()
        return run

    async def cancel(self, rid: str) -> None:
        run = self.runs.get(rid)
        if not run:
            return
        self.flags.setdefault(rid, {})["canceled"] = True
        work = self.procs.get(rid, {}).get("work")
        if work:
            kill_tree(work.pid)
        await self.shutdown_server(run)
        if run.status not in TERMINAL:
            run.status = "canceled"
            run.finishedAt = time.time()
            run.browserOpen = False
        self._save()
        self.schedule()

    async def control(self, rid: str, action: str) -> dict:
        run = self.runs.get(rid)
        if not run:
            return {"ok": False, "error": "run not found"}
        if not run.serverPort or not run.browserOpen:
            return {"ok": False, "error": "browser is not open for this run"}
        port = run.serverPort
        try:
            if action == "takeover":
                self.flags.setdefault(rid, {})["takingControl"] = True
                await self._server_post(port, "/pause")
                if not run.watch:
                    await self._server_post(port, "/switch_mode", {"headless": False})
                    run.watch = True
                run.status = "controlled"
            elif action == "pause":
                await self._server_post(port, "/pause")
                run.status = "controlled"
            elif action == "resume":
                await self._server_post(port, "/resume")
                if self.procs.get(rid, {}).get("work") and run.status == "controlled":
                    run.status = "running"
            elif action == "show":
                await self._server_post(port, "/switch_mode", {"headless": False}); run.watch = True
            elif action == "hide":
                await self._server_post(port, "/switch_mode", {"headless": True}); run.watch = False
            elif action == "close":
                await self.shutdown_server(run)
                if run.status not in TERMINAL:
                    run.status = "canceled"; run.finishedAt = time.time()
            else:
                return {"ok": False, "error": f"unknown action: {action}"}
            self._save(); self.schedule()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_result_path(self, rid: str) -> Path:
        run = self.runs.get(rid)
        if run and run.csvPath and Path(run.csvPath).exists():
            return Path(run.csvPath)
        return RUNS_DIR / rid / "output.csv"

    # ------------------------------------------------------------------ scheduling
    def schedule(self) -> None:
        """Start queued runs under the per-profile concurrency rule:
        - a **persistent** profile runs ONE run at a time (others queue behind it),
          and an open manual login session also occupies it — so the profile ages
          serially and genuinely, like a real returning user;
        - **ephemeral** runs (and runs on *different* profiles) run in parallel,
          bounded only by a machine-safety ceiling on simultaneous browsers.
        """
        # profiles occupied right now (by an active run, a manual login session, an
        # agent-owned browser, or an owner mid-acquire)
        busy = set(self.sessions.keys()) | set(self._acquiring) | {b["pid"] for b in self.agent_browsers.values()}
        for r in self.runs.values():
            if r.status in ACTIVE and not is_ephemeral(r.profileId) and not r.attachPort:
                busy.add(r.profileId)
        live = sum(1 for r in self.runs.values() if r.status in ACTIVE) + len(self.sessions) + len(self.agent_browsers)
        queued = sorted((r for r in self.runs.values() if r.status == "queued"), key=lambda r: r.createdAt)
        started = False
        for run in queued:
            if live >= GLOBAL_SAFETY_CAP:
                break
            if run.attachPort:
                pass  # attaches to an agent's owned browser — no profile lock needed
            elif not is_ephemeral(run.profileId):
                if run.profileId in busy:
                    continue  # its persistent profile is in use → keep it queued
                busy.add(run.profileId)  # claim the profile for this scheduling pass
            run.status = "starting"  # claim synchronously so a re-entrant schedule() can't double-start
            live += 1
            started = True
            asyncio.create_task(self._start_run_guarded(run))
        if started:
            self._save()

    async def _start_run_guarded(self, run: Run) -> None:
        try:
            await self.start_run(run)
        except Exception as e:
            run.status = "failed"
            run.error = f"failed to start: {e}"
            run.finishedAt = time.time()
            run.browserOpen = False
            self._log(run.id, f"[backend] start error: {e}")
            try:
                await self.shutdown_server(run)
            except Exception:
                pass
            self._save()
            self.schedule()

    def _input_args(self, run: Run, wf) -> list[str]:
        """For a list-consuming workflow bound to an input dataset, dump the dataset
        rows to input.json and pass --input-json (read by automations.userkit).

        File-typed cells (`file` / `file_list` columns) are expanded from bare
        ids to ``{id, path, name, mime, ...}`` dicts here, so workflow code
        just reads ``row["image"]["path"]`` without an extra lookup."""
        if not run.inputDatasetId or not getattr(wf, "input_contract", None):
            return []
        try:
            from . import datastore, files as fstore
            ds = datastore.get_dataset(run.inputDatasetId)
            file_cols = {c["display"] for c in (ds.get("columns") or []) if c["type"] == "file"}
            list_cols = {c["display"] for c in (ds.get("columns") or []) if c["type"] == "file_list"}
            rows, offset = [], 0
            while True:
                page = datastore.get_rows(run.inputDatasetId, limit=5000, offset=offset)["rows"]
                if not page:
                    break
                for r in page:
                    row = {k: v for k, v in r.items() if k != "_rid"}
                    for col in file_cols:
                        if col in row and row[col] is not None and str(row[col]).strip():
                            row[col] = fstore.expand_value(row[col])
                    for col in list_cols:
                        if col in row and row[col] is not None and str(row[col]).strip():
                            row[col] = fstore.expand_value(row[col])
                    rows.append(row)
                offset += len(page)
                if len(page) < 5000:
                    break
            p = RUNS_DIR / run.id / "input.json"
            p.write_text(json.dumps(rows))
            extras = []
            if file_cols:
                extras.append(f"{len(file_cols)} file col{'s' if len(file_cols) != 1 else ''}")
            if list_cols:
                extras.append(f"{len(list_cols)} file-list col{'s' if len(list_cols) != 1 else ''}")
            note = f" ({', '.join(extras)} expanded)" if extras else ""
            self._log(run.id, f"[backend] input dataset {run.inputDatasetId}: {len(rows)} rows{note}")
            return ["--input-json", str(p)]
        except Exception as e:
            self._log(run.id, f"[backend] input dataset load failed: {e}")
            return []

    def _alloc_port(self) -> int:
        """Pick a control-server port free at BOTH the bookkeeping level (no run, manual
        profile session or agent browser is assigned it) AND the OS level (nothing is
        currently listening on it). The OS probe is essential: manual sessions and agent
        browsers occupy ports in this same range, and handing out one already in use made
        a run's control-server fail to bind while the workflow silently drove whatever
        browser was already on that port (e.g. the user's manually-opened profile)."""
        used = {r.serverPort for r in self.runs.values() if r.serverPort}
        used |= {s["port"] for s in self.sessions.values() if s.get("port")}
        used |= {b["port"] for b in self.agent_browsers.values() if b.get("port")}
        for p in range(PORT_BASE, PORT_BASE + 80):
            if p not in used and not _port_listening(p):
                return p
        return _free_ephemeral_port()

    # ------------------------------------------------------------------ run lifecycle
    async def start_run(self, run: Run) -> None:
        from .registry import get_workflow
        from . import profiles
        wf = get_workflow(run.workflowId)
        run.status = "starting"
        run.startedAt = time.time()

        # Attached run: an agent already owns a control-server on this profile; the
        # workflow shares that browser instead of launching its own.
        if run.attachPort:
            run.serverPort = run.attachPort
            run.browserOpen = True
            run.status = "running"
            self._log(run.id, f"[backend] attached to agent browser on :{run.attachPort} — launching workflow")
            self._save()
            csv = str(RUNS_DIR / run.id / "output.csv")
            work_cmd = _self_base() + ["run-workflow", wf.target] + wf.build_argv(run.params) + \
                self._input_args(run, wf) + ["--server", f"http://127.0.0.1:{run.attachPort}", "-o", csv]
            work = await self._spawn(work_cmd)
            self.procs.setdefault(run.id, {})["work"] = work
            asyncio.create_task(self._pump(run.id, work.stdout))
            asyncio.create_task(self._await_workflow(run, work))
            return

        port = self._alloc_port()
        run.serverPort = port
        # Profile dir for this run:
        #  - ephemeral → a fresh empty throwaway dir (deleted on teardown);
        #  - persistent → the profile's MASTER dir directly, so cookies/login/
        #    history accumulate and the profile ages genuinely. The scheduler
        #    serialises persistent profiles, so no two processes share the dir;
        #    we clear any stale lock left by a previous hard-kill before launch.
        if is_ephemeral(run.profileId):
            run.profileDir = profiles.fresh_for_run(run.id)
        else:
            profiles.clear_locks(run.profileId)
            run.profileDir = str(profiles.master_dir(run.profileId))
            profiles.touch(run.profileId)
        (RUNS_DIR / run.id).mkdir(parents=True, exist_ok=True)
        self._save()

        # 1) control server (the browser session)
        server_cmd = _self_base() + ["control-server", "--port", str(port), "--profile", run.profileDir]
        if not run.watch:
            server_cmd.append("--headless")
        server = await self._spawn(server_cmd)
        self.procs.setdefault(run.id, {})["server"] = server
        asyncio.create_task(self._pump(run.id, server.stdout, "[browser] "))

        # Our control-server must come up AND still be alive: if it died (e.g. lost a
        # port race and failed to bind), _wait_ready could otherwise see a DIFFERENT
        # server already on this port and we'd silently attach to the wrong browser.
        ready = await self._wait_ready(port, 25)
        if server.returncode is not None or not ready:
            self._log(run.id, "[backend] control server failed to start (port busy or crashed)")
            run.status = "failed"; run.error = "control server failed to start"; run.finishedAt = time.time()
            kill_tree(server.pid)
            self._save(); self.schedule(); return
        run.browserOpen = True
        run.status = "running"
        self._log(run.id, f"[backend] browser ready on :{port} ({'headed' if run.watch else 'headless'}) — launching workflow")
        self._save()

        # 2) workflow, attached to the server
        csv = str(RUNS_DIR / run.id / "output.csv")
        work_cmd = _self_base() + ["run-workflow", wf.target] + wf.build_argv(run.params) + \
            self._input_args(run, wf) + ["--server", f"http://127.0.0.1:{port}", "-o", csv]
        work = await self._spawn(work_cmd)
        self.procs.setdefault(run.id, {})["work"] = work
        asyncio.create_task(self._pump(run.id, work.stdout))
        asyncio.create_task(self._await_workflow(run, work))

    async def _spawn(self, cmd: list[str]):
        kw: dict = {"stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.STDOUT, "cwd": str(Path(__file__).resolve().parent.parent)}
        if os.name == "posix":
            kw["start_new_session"] = True
        return await asyncio.create_subprocess_exec(*cmd, **kw)

    async def _pump(self, rid: str, stream, prefix: str = "") -> None:
        if stream is None:
            return
        while True:
            try:
                raw = await stream.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip("\r\n")
            if line.startswith(RS):
                try:
                    self._handle_event(self.runs.get(rid), json.loads(line[1:]))
                except Exception:
                    pass
            elif line.strip():
                self._log(rid, prefix + line)

    def _handle_event(self, run: Run | None, ev: dict) -> None:
        if not run:
            return
        kind = ev.get("hb_event")
        if kind == "status" and ev.get("state") == "running" and run.status == "starting":
            run.status = "running"
        elif kind == "progress":
            run.progress = {"collected": int(ev.get("collected", 0)), "total": int(ev.get("total", 0)),
                            "message": ev.get("message", ""), "page": ev.get("page")}
            if ev.get("url"):
                run.lastUrl = ev["url"]
            self._log(run.id, f"[progress] {ev.get('collected')}/{ev.get('total')} {ev.get('message','')}")
        elif kind == "result":
            run.csvPath = ev.get("csv"); run.rows = int(ev.get("rows", 0))
        elif kind == "error":
            run.error = ev.get("message")
            if ev.get("url"):
                run.lastUrl = ev["url"]
        elif kind == "log":
            self._log(run.id, str(ev.get("message", "")))
        self._save()

    async def _await_workflow(self, run: Run, work) -> None:
        code = await work.wait()
        await self._on_workflow_close(run, code)

    async def _on_workflow_close(self, run: Run, code: int) -> None:
        flags = self.flags.get(run.id, {})
        run.finishedAt = time.time()
        self.procs.setdefault(run.id, {})["work"] = None
        csv = RUNS_DIR / run.id / "output.csv"
        if flags.get("canceled"):
            run.status = "canceled"
            await self.shutdown_server(run)
        elif flags.get("takingControl"):
            run.status = "controlled"
            self._log(run.id, "[backend] handed control to you — browser is paused")
        elif code == 0 and csv.exists():
            run.status = "succeeded"; run.csvPath = str(csv)
            self._log(run.id, f"[backend] done — {run.rows if run.rows is not None else '?'} rows")
            if run.datasetId:
                try:
                    from . import datastore
                    from .registry import get_workflow
                    _wf = get_workflow(run.workflowId)
                    res = datastore.ingest_csv(csv, target_id=run.datasetId,
                                               source={"kind": "run", "runId": run.id, "workflow": run.workflowId},
                                               columns=(_wf.output_contract if _wf else None))
                    self._log(run.id, f"[backend] → dataset {run.datasetId}: +{res.get('inserted',0)} rows "
                                      f"({res.get('skipped',0)} dup skipped)")
                except Exception as e:
                    self._log(run.id, f"[backend] dataset append failed: {e}")
            await self.shutdown_server(run)
        else:
            run.status = "failed"
            if not run.error:
                run.error = f"workflow exited with code {code}"
            self._log(run.id, f"[backend] failed: {run.error} — browser left open for inspection")
            if not run.attachPort:  # don't reconfigure an agent's shared browser
                try:
                    await self._server_post(run.serverPort, "/switch_mode", {"headless": False})
                    await self._server_post(run.serverPort, "/pause")
                    run.watch = True
                except Exception:
                    pass
            else:
                await self.shutdown_server(run)  # detach bookkeeping; keep agent's browser
        self._save()
        self.schedule()
        # Canonical notification: a workflow an agent launched (detached) finished →
        # tell that agent, which wakes it (a `waiting` agent resumes; a `done` one is
        # re-activated). The agent then inspects the result and decides what's next.
        if run.agentId and run.status in TERMINAL:
            try:
                from .agents import get_agents
                get_agents().notify(run.agentId, "workflow_finished", {
                    "runId": run.id, "workflow": run.workflowId, "status": run.status,
                    "rows": run.rows, "error": run.error, "datasetId": run.datasetId})
            except Exception as e:
                self._log(run.id, f"[backend] agent notify failed: {e}")

    # ------------------------------------------------------------------ server helpers
    async def shutdown_server(self, run: Run) -> None:
        from . import profiles
        run.browserOpen = False
        # Attached run: the agent owns the control-server/browser — never touch it,
        # just drop our bookkeeping for this workflow.
        if run.attachPort:
            self.procs.pop(run.id, None)
            self.flags.pop(run.id, None)
            return
        server = self.procs.get(run.id, {}).get("server")
        persistent = not is_ephemeral(run.profileId)
        # Ask the control server to close cleanly: browser.stop() runs
        # context.close(), which flushes the cookie/history SQLite DBs and frees
        # the SingletonLock. For a persistent profile we must let that finish
        # before killing the tree, or we'd hard-kill the master mid-flush.
        if run.serverPort:
            try:
                await self._server_post(run.serverPort, "/shutdown")
            except Exception:
                pass
        if server and server.pid:
            if persistent:
                try:
                    await asyncio.wait_for(server.wait(), timeout=10)
                except Exception:
                    pass
            kill_tree(server.pid)  # ensure the whole tree is gone (no-op if already exited)
        if persistent:
            profiles.clear_locks(run.profileId)  # drop any lock the close left behind
        elif run.profileDir and run.profileDir.startswith(str(EPHEMERAL_DIR)):
            try:
                shutil.rmtree(run.profileDir, ignore_errors=True)  # discard the throwaway dir
            except Exception:
                pass
        self.procs.pop(run.id, None)
        self.flags.pop(run.id, None)

    async def _wait_ready(self, port: int, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                async with self._http.get(f"http://127.0.0.1:{port}/status", timeout=aiohttp.ClientTimeout(total=1)) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return False

    async def _server_post(self, port: int, path: str, body: dict | None = None) -> dict:
        async with self._http.post(f"http://127.0.0.1:{port}{path}", json=body or {}) as r:
            return await r.json()

    async def _server_get(self, port: int, path: str) -> dict:
        async with self._http.get(f"http://127.0.0.1:{port}{path}") as r:
            return await r.json()


# single shared instance
_manager: RunManager | None = None


def get_manager() -> RunManager:
    global _manager
    if _manager is None:
        _manager = RunManager()
    return _manager
