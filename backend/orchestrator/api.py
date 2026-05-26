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
from .registry import (all_workflows, public_workflow, save_user_workflow,
                       delete_user_workflow, user_workflow_source)
from . import datastore


@asynccontextmanager
async def lifespan(app: FastAPI):
    mgr = get_manager()
    await mgr.start()
    from .agents import get_agents
    get_agents()  # construct (loads defs/sessions, seeds built-ins)
    try:
        yield
    finally:
        try:
            await get_agents().shutdown()
        except Exception:
            pass
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
        return {"ok": True, "version": os.environ.get("AUTOMATION_VERSION", "0.0.0")}

    @app.get("/api/workflows")
    async def workflows():
        return {"workflows": [public_workflow(w) for w in all_workflows()]}

    @app.post("/api/workflows")
    async def create_workflow(body: dict = Body(...)):
        try:
            return {"workflow": save_user_workflow(body)}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/workflows/{wid}/source")
    async def workflow_source(wid: str):
        src = user_workflow_source(wid)
        return {"source": src} if src is not None else JSONResponse({"error": "not found or built-in"}, status_code=404)

    @app.delete("/api/workflows/{wid}")
    async def remove_workflow(wid: str):
        return {"ok": delete_user_workflow(wid)}

    @app.get("/api/runs")
    async def list_runs():
        mgr = get_manager()
        return {"runs": mgr.list(), "settings": mgr.get_settings()}

    @app.post("/api/runs")
    async def create_run(body: dict = Body(...)):
        mgr = get_manager()
        try:
            run = mgr.create(body["workflowId"], body.get("params") or {}, bool(body.get("watch")),
                             body.get("profileId") or "ephemeral", body.get("datasetId"),
                             body.get("attachPort"), body.get("agentId"), body.get("inputDatasetId"))
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

    @app.post("/api/runs/{rid}/to-dataset")
    async def run_to_dataset(rid: str, body: dict = Body(...)):
        """Append a finished run's result into a Dataset (new or existing). The
        canonical way to capture a run's output into the persistent data layer."""
        mgr = get_manager()
        run = mgr.get(rid)
        if not run:
            return JSONResponse({"error": "run not found"}, status_code=404)
        path = mgr.get_result_path(rid)
        if not path.exists():
            return JSONResponse({"error": "this run has no result"}, status_code=404)
        name = body.get("name") or f"{run['workflowName']} — {rid}"
        from .registry import get_workflow
        wf = get_workflow(run["workflowId"])
        res = datastore.ingest_csv(path, target_id=body.get("datasetId"), name=name,
                                   dedup_keys=body.get("dedupKeys"),
                                   source={"kind": "run", "runId": rid, "workflow": run["workflowId"]},
                                   columns=(wf.output_contract if wf else None))
        return res

    @app.get("/api/settings")
    async def get_settings():
        return get_manager().get_settings()

    @app.post("/api/settings")
    async def set_settings(body: dict = Body(...)):
        return get_manager().set_settings(body)

    # ---------------------------------------------------------------- datasets
    # Static collection routes are declared before the dynamic /{did} routes.
    @app.get("/api/datasets")
    async def datasets_list():
        return {"datasets": datastore.list_datasets()}

    @app.post("/api/datasets")
    async def datasets_create(body: dict = Body(...)):
        ds = datastore.create_dataset(body.get("name", "Untitled"), body.get("columns") or [],
                                      body.get("dedupKeys"), body.get("source"), body.get("rows"))
        return {"dataset": ds}

    @app.post("/api/datasets/project")
    async def datasets_project(body: dict = Body(...)):
        ds = datastore.project(body["srcId"], body.get("columns") or [], body.get("name", "Projection"),
                               body.get("dedupKeys"))
        return {"dataset": ds} if ds else JSONResponse({"error": "could not project"}, status_code=400)

    @app.post("/api/datasets/merge")
    async def datasets_merge(body: dict = Body(...)):
        ds = datastore.merge(body.get("ids") or [], body.get("name", "Merged"), body.get("dedupKeys"))
        return {"dataset": ds} if ds else JSONResponse({"error": "could not merge"}, status_code=400)

    @app.post("/api/datasets/import")
    async def datasets_import(body: dict = Body(...)):
        import tempfile, os as _os
        text = body.get("csv") or ""
        fd, tmp = tempfile.mkstemp(suffix=".csv")
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            res = datastore.ingest_csv(tmp, target_id=body.get("datasetId"),
                                       name=body.get("name", "Imported"), dedup_keys=body.get("dedupKeys"))
        finally:
            try: _os.remove(tmp)
            except OSError: pass
        return res

    @app.post("/api/datasets/query")
    async def datasets_query(body: dict = Body(...)):
        return datastore.query(body.get("sql", ""), int(body.get("maxRows", 5000)))

    @app.get("/api/datasets/schema")
    async def datasets_schema():
        return {"schema": datastore.schema_summary()}

    @app.get("/api/datasets/{did}")
    async def dataset_get(did: str):
        ds = datastore.get_dataset(did)
        return {"dataset": ds} if ds else JSONResponse({"error": "not found"}, status_code=404)

    @app.post("/api/datasets/{did}/rename")
    async def dataset_rename(did: str, body: dict = Body(...)):
        ds = datastore.rename_dataset(did, body.get("name", ""))
        return {"dataset": ds} if ds else JSONResponse({"error": "not found"}, status_code=404)

    @app.delete("/api/datasets/{did}")
    async def dataset_delete(did: str):
        return {"ok": datastore.delete_dataset(did)}

    @app.get("/api/datasets/{did}/rows")
    async def dataset_rows(did: str, limit: int = 200, offset: int = 0, search: str = "",
                           sort: str | None = None, direction: str = "asc"):
        return datastore.get_rows(did, limit, offset, search, sort, direction)

    @app.post("/api/datasets/{did}/rows")
    async def dataset_add_rows(did: str, body: dict = Body(...)):
        if "values" in body:
            return datastore.insert_row(did, body["values"])
        return datastore.append_rows(did, body.get("rows") or [], bool(body.get("dedup", True)),
                                     bool(body.get("extend", False)))

    @app.post("/api/datasets/{did}/cell")
    async def dataset_cell(did: str, body: dict = Body(...)):
        ok = datastore.update_cell(did, int(body["rid"]), body["column"], body.get("value"))
        return {"ok": ok}

    @app.post("/api/datasets/{did}/delete-rows")
    async def dataset_delete_rows(did: str, body: dict = Body(...)):
        return {"removed": datastore.delete_rows(did, body.get("rids") or [])}

    @app.post("/api/datasets/{did}/add-column")
    async def dataset_add_column(did: str, body: dict = Body(...)):
        ds = datastore.add_column(did, body["display"], body.get("type", "text"))
        return {"dataset": ds} if ds else JSONResponse({"error": "not found"}, status_code=404)

    @app.post("/api/datasets/{did}/drop-column")
    async def dataset_drop_column(did: str, body: dict = Body(...)):
        ds = datastore.drop_column(did, body["display"])
        return {"dataset": ds} if ds else JSONResponse({"error": "not found"}, status_code=404)

    @app.post("/api/datasets/{did}/rename-column")
    async def dataset_rename_column(did: str, body: dict = Body(...)):
        ds = datastore.rename_column(did, body["from"], body["to"])
        return {"dataset": ds} if ds else JSONResponse({"error": "not found"}, status_code=404)

    @app.post("/api/datasets/{did}/dedup-keys")
    async def dataset_dedup_keys(did: str, body: dict = Body(...)):
        ds = datastore.set_dedup_keys(did, body.get("keys") or [])
        return {"dataset": ds} if ds else JSONResponse({"error": "not found"}, status_code=404)

    @app.post("/api/datasets/{did}/dedup")
    async def dataset_dedup(did: str, body: dict = Body(...)):
        return datastore.dedup(did, body.get("keys"))

    @app.get("/api/datasets/{did}/export")
    async def dataset_export(did: str):
        ds = datastore.get_dataset(did)
        path = datastore.export_csv(did)
        if not path:
            return JSONResponse({"error": "not found"}, status_code=404)
        safe = (ds["name"] if ds else did).replace("/", "-")
        return FileResponse(str(path), media_type="text/csv", filename=f"{safe}.csv")

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
        mgr = get_manager()
        # never delete a profile a run is actively using (it runs on the master dir)
        if any(r["profileId"] == pid and r["status"] in ("starting", "running", "controlled")
               for r in mgr.list()):
            return JSONResponse({"error": "profile is busy with a run"}, status_code=400)
        # close any open login window first so its files aren't locked
        await mgr.close_profile_session(pid)
        ok = profiles.delete(pid)
        return {"ok": ok} if ok else JSONResponse(
            {"error": "cannot delete the last remaining profile"}, status_code=400)

    @app.post("/api/profiles/{pid}/open")
    async def open_profile(pid: str):
        res = await get_manager().open_profile_session(pid)
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    @app.post("/api/profiles/{pid}/close")
    async def close_profile(pid: str):
        return await get_manager().close_profile_session(pid)

    # ---------------------------------------------------------------- agents
    @app.get("/api/agents/engines")
    async def agent_engines():
        from .agents import engine_status
        return {"engines": engine_status()}

    @app.get("/api/agents/sessions")
    async def agent_sessions():
        from .agents import get_agents
        return {"sessions": get_agents().list_sessions()}

    @app.post("/api/agents/sessions")
    async def agent_launch(body: dict = Body(...)):
        from .agents import get_agents
        try:
            s = get_agents().launch(body["agentId"], body.get("profileId") or "ephemeral",
                                    body.get("prompt", ""), bool(body.get("watch")))
            return {"session": get_agents().get_session(s.id)}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/agents/sessions/{sid}")
    async def agent_session(sid: str):
        from .agents import get_agents
        s = get_agents().get_session(sid)
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"session": s, "events": get_agents().get_events(sid)}

    @app.post("/api/agents/sessions/{sid}/steer")
    async def agent_steer(sid: str, body: dict = Body(...)):
        from .agents import get_agents
        return get_agents().steer(sid, body.get("message", ""))

    @app.post("/api/agents/sessions/{sid}/stop")
    async def agent_stop(sid: str):
        from .agents import get_agents
        return await get_agents().stop(sid)

    @app.get("/api/agents")
    async def agents_list():
        from .agents import get_agents
        return {"agents": get_agents().list_defs()}

    @app.post("/api/agents")
    async def agents_create(body: dict = Body(...)):
        from .agents import get_agents
        try:
            return {"agent": get_agents().create_def(body)}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/api/agents/{aid}/update")
    async def agents_update(aid: str, body: dict = Body(...)):
        from .agents import get_agents
        d = get_agents().update_def(aid, body)
        return {"agent": d} if d else JSONResponse({"error": "not found"}, status_code=404)

    @app.delete("/api/agents/{aid}")
    async def agents_delete(aid: str):
        from .agents import get_agents
        return {"ok": get_agents().delete_def(aid)}

    return app
