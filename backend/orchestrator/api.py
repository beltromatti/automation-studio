"""FastAPI app exposing the orchestrator over localhost HTTP. The Electron
frontend (and any client) talks to this; the backend owns all state."""
from __future__ import annotations

import csv as csvmod
import io
from contextlib import asynccontextmanager

from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

from .manager import get_manager
from .registry import WORKFLOWS, public_workflow


@asynccontextmanager
async def lifespan(app: FastAPI):
    mgr = get_manager()
    await mgr.start()
    try:
        yield
    finally:
        await mgr.stop()


def _parse_csv(path) -> dict:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csvmod.DictReader(io.StringIO(text))
    rows = list(reader)
    return {"columns": reader.fieldnames or [], "rows": rows[:5000], "count": len(rows)}


def create_app() -> FastAPI:
    app = FastAPI(title="Automation Studio backend", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/health")
    async def health():
        import os
        return {"ok": True, "version": os.environ.get("AUTOMATION_VERSION", "dev")}

    @app.get("/api/workflows")
    async def workflows():
        return {"workflows": [public_workflow(w) for w in WORKFLOWS]}

    @app.get("/api/runs")
    async def list_runs():
        mgr = get_manager()
        return {"runs": mgr.list(), "settings": mgr.get_settings()}

    @app.post("/api/runs")
    async def create_run(body: dict = Body(...)):
        mgr = get_manager()
        try:
            run = mgr.create(body["workflowId"], body.get("params") or {}, bool(body.get("watch")),
                             body.get("profileId") or "ephemeral")
            return {"run": mgr.get(run.id)}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/runs/{rid}")
    async def run_detail(rid: str):
        mgr = get_manager()
        run = mgr.get(rid)
        if not run:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"run": run, "logs": mgr.get_logs(rid)}

    @app.post("/api/runs/{rid}/cancel")
    async def cancel_run(rid: str):
        await get_manager().cancel(rid)
        return {"ok": True}

    @app.post("/api/runs/{rid}/control")
    async def control_run(rid: str, body: dict = Body(...)):
        res = await get_manager().control(rid, body.get("action", ""))
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    @app.get("/api/runs/{rid}/result")
    async def run_result(rid: str):
        mgr = get_manager()
        if not mgr.get(rid):
            return JSONResponse({"error": "not found"}, status_code=404)
        path = mgr.get_result_path(rid)
        if not path.exists():
            return JSONResponse({"error": "no results yet"}, status_code=404)
        return _parse_csv(path)

    @app.get("/api/runs/{rid}/download")
    async def run_download(rid: str):
        mgr = get_manager()
        run = mgr.get(rid)
        path = mgr.get_result_path(rid)
        if not path.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        name = f"{run['workflowId'] if run else 'run'}-{rid}.csv"
        return FileResponse(str(path), media_type="text/csv", filename=name)

    @app.get("/api/settings")
    async def get_settings():
        return get_manager().get_settings()

    @app.post("/api/settings")
    async def set_settings(body: dict = Body(...)):
        return get_manager().set_settings(body)

    # ---------------------------------------------------------------- profiles
    @app.get("/api/profiles")
    async def list_profiles():
        from . import profiles
        open_ids = set(get_manager().open_session_ids())
        items = [{**p, "open": p["id"] in open_ids,
                  "openPort": get_manager().sessions.get(p["id"], {}).get("port")}
                 for p in profiles.list_profiles()]
        return {"profiles": items}

    @app.post("/api/profiles")
    async def create_profile(body: dict = Body(...)):
        from . import profiles
        return {"profile": profiles.create(body.get("name", "Profile"))}

    @app.post("/api/profiles/{pid}/rename")
    async def rename_profile(pid: str, body: dict = Body(...)):
        from . import profiles
        p = profiles.rename(pid, body.get("name", ""))
        return {"profile": p} if p else JSONResponse({"error": "not found"}, status_code=404)

    @app.delete("/api/profiles/{pid}")
    async def delete_profile(pid: str):
        from . import profiles
        # closing any open session first so its files aren't locked
        await get_manager().close_profile_session(pid)
        ok = profiles.delete(pid)
        return {"ok": ok} if ok else JSONResponse({"error": "cannot delete (default or not found)"}, status_code=400)

    @app.post("/api/profiles/{pid}/open")
    async def open_profile(pid: str):
        res = await get_manager().open_profile_session(pid)
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    @app.post("/api/profiles/{pid}/close")
    async def close_profile(pid: str):
        return await get_manager().close_profile_session(pid)

    return app
