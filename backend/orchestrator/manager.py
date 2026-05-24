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
MAX_CONCURRENCY = 4
REAP_INTERVAL = 10  # seconds
TERMINAL = {"succeeded", "failed", "canceled"}
ACTIVE = {"starting", "running", "controlled"}

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


class RunManager:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self.runs: dict[str, Run] = {}
        self.procs: dict[str, dict[str, Any]] = {}  # id -> {server, work} asyncio procs
        self.logs: dict[str, list[str]] = {}
        self.flags: dict[str, dict] = {}  # id -> {canceled, takingControl}
        self.sessions: dict[str, dict] = {}  # profileId -> {proc, port} (manual open sessions)
        self.settings = {"maxConcurrency": 1}
        self._http: aiohttp.ClientSession | None = None
        self._reaper_task: asyncio.Task | None = None
        self.reap_strays(startup=True)
        self._load()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._http = aiohttp.ClientSession()
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def stop(self) -> None:
        if self._reaper_task:
            self._reaper_task.cancel()
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

    def kill_all(self) -> None:
        for p in self.procs.values():
            for key in ("work", "server"):
                proc = p.get(key)
                if proc and proc.pid:
                    kill_tree(proc.pid)
        for s in self.sessions.values():
            if s.get("proc"):
                kill_tree(s["proc"].pid)
        self.reap_strays(startup=True)

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
        port = self._alloc_port()
        master = str(profiles.master_dir(pid))
        Path(master).mkdir(parents=True, exist_ok=True)
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
        s = self.sessions.get(pid)
        if not s:
            return {"ok": True}
        try:
            await self._server_post(s["port"], "/shutdown")
        except Exception:
            pass
        kill_tree(s["proc"].pid)
        self.sessions.pop(pid, None)
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
        try:
            if RUNS_INDEX.exists():
                for d in json.loads(RUNS_INDEX.read_text()):
                    fields = {k: d[k] for k in Run.__dataclass_fields__ if k in d}
                    r = Run(**fields)
                    if r.status not in TERMINAL:
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
    def list(self) -> list[dict]:
        return [asdict(r) for r in sorted(self.runs.values(), key=lambda r: r.createdAt, reverse=True)]

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
               profile_id: str = "temporary") -> Run:
        from .registry import get_workflow
        from . import profiles
        wf = get_workflow(workflow_id)
        if not wf:
            raise ValueError(f"unknown workflow: {workflow_id}")
        for p in wf.params:
            if p.required and not params.get(p.name):
                raise ValueError(f"missing required parameter: {p.label}")
        if profile_id and profile_id != "temporary":
            prof = profiles.get(profile_id)
            if not prof:
                raise ValueError(f"unknown profile: {profile_id}")
            profile_name = prof["name"]
        else:
            profile_id, profile_name = "temporary", "Temporary"
        rid = uuid.uuid4().hex[:8]
        run = Run(id=rid, workflowId=workflow_id, workflowName=wf.name, params=params,
                  status="queued", watch=bool(watch), createdAt=time.time(),
                  profileKey=profile_id, profileId=profile_id, profileName=profile_name)
        self.runs[rid] = run
        (RUNS_DIR / rid).mkdir(parents=True, exist_ok=True)
        self._save()
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
        # Each run uses its own cloned profile dir, so runs never contend for a
        # profile lock — they can all run in parallel up to maxConcurrency,
        # whatever profile they use (same or different).
        active = [r for r in self.runs.values() if r.status in ACTIVE]
        running = sum(1 for r in active if r.status in ("running", "starting"))
        slots = self.settings["maxConcurrency"] - running
        if slots <= 0:
            return
        queued = sorted((r for r in self.runs.values() if r.status == "queued"), key=lambda r: r.createdAt)
        for run in queued[:slots]:
            asyncio.create_task(self._start_run_guarded(run))

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

    def _alloc_port(self) -> int:
        used = {r.serverPort for r in self.runs.values() if r.serverPort}
        for p in range(PORT_BASE, PORT_BASE + 80):
            if p not in used:
                return p
        return PORT_BASE + 79

    # ------------------------------------------------------------------ run lifecycle
    async def start_run(self, run: Run) -> None:
        from .registry import get_workflow
        from . import profiles
        wf = get_workflow(run.workflowId)
        run.status = "starting"
        run.startedAt = time.time()
        port = self._alloc_port()
        run.serverPort = port
        # Give the run an isolated, parallel-safe profile dir: a fast CLONE of the
        # chosen profile (login preserved) or a fresh empty one for "Temporary".
        if run.profileId and run.profileId != "temporary":
            run.profileDir = profiles.clone_for_run(run.profileId, run.id)
            profiles.touch(run.profileId)
        else:
            run.profileDir = profiles.fresh_for_run(run.id)
        (RUNS_DIR / run.id).mkdir(parents=True, exist_ok=True)
        self._save()

        # 1) control server (the browser session)
        server_cmd = _self_base() + ["control-server", "--port", str(port), "--profile", run.profileDir]
        if not run.watch:
            server_cmd.append("--headless")
        server = await self._spawn(server_cmd)
        self.procs.setdefault(run.id, {})["server"] = server
        asyncio.create_task(self._pump(run.id, server.stdout, "[browser] "))

        if not await self._wait_ready(port, 25):
            self._log(run.id, "[backend] control server failed to start")
            run.status = "failed"; run.error = "control server failed to start"; run.finishedAt = time.time()
            kill_tree(server.pid)
            self._save(); self.schedule(); return
        run.browserOpen = True
        run.status = "running"
        self._log(run.id, f"[backend] browser ready on :{port} ({'headed' if run.watch else 'headless'}) — launching workflow")
        self._save()

        # 2) workflow, attached to the server
        csv = str(RUNS_DIR / run.id / "output.csv")
        work_cmd = _self_base() + ["run-workflow", wf.module] + wf.build_argv(run.params) + ["--server", f"http://127.0.0.1:{port}", "-o", csv]
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
            await self.shutdown_server(run)
        else:
            run.status = "failed"
            if not run.error:
                run.error = f"workflow exited with code {code}"
            self._log(run.id, f"[backend] failed: {run.error} — browser left open for inspection")
            try:
                await self._server_post(run.serverPort, "/switch_mode", {"headless": False})
                await self._server_post(run.serverPort, "/pause")
                run.watch = True
            except Exception:
                pass
        self._save()
        self.schedule()

    # ------------------------------------------------------------------ server helpers
    async def shutdown_server(self, run: Run) -> None:
        run.browserOpen = False
        pid = (self.procs.get(run.id, {}).get("server") or None)
        pid = pid.pid if pid else None
        if run.serverPort:
            try:
                await self._server_post(run.serverPort, "/shutdown")
            except Exception:
                pass
        kill_tree(pid)
        if run.profileDir and run.profileDir.startswith(str(EPHEMERAL_DIR)):
            try:
                shutil.rmtree(run.profileDir, ignore_errors=True)
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
