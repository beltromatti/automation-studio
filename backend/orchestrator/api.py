"""FastAPI app exposing the orchestrator over localhost HTTP. The Electron
frontend (and any client) talks to this; the backend owns all state."""
from __future__ import annotations

import csv as csvmod
import io
from contextlib import asynccontextmanager

import asyncio
import json as jsonmod
from fastapi import FastAPI, Body, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse

from .manager import get_manager
from .registry import (all_workflows, get_workflow, public_workflow, save_user_workflow,
                       delete_user_workflow, workflow_source)
from . import datastore
from . import files as fstore


def _resolve_at(body: dict) -> float | None:
    """A schedule time from a request: absolute `startAt`/`at` (unix seconds) or
    relative `inSeconds` from now. None = run now."""
    import time
    at = body.get("startAt") if body.get("startAt") is not None else body.get("at")
    if at is not None:
        try:
            return float(at)
        except (TypeError, ValueError):
            return None
    secs = body.get("inSeconds")
    if secs is not None:
        try:
            return time.time() + float(secs)
        except (TypeError, ValueError):
            return None
    return None


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


def _absorb_run_files(csv_path, run_id: str, file_cols: list[str], list_cols: list[str]):
    """Read the run's CSV, rewrite path-or-dict values in `file` / `file_list`
    columns into Studio file ids (auto-registering anything that's a real file),
    write to a sibling CSV next to the original and return the new path."""
    import json as _json
    file_cols_lc = {c.strip().lower() for c in file_cols}
    list_cols_lc = {c.strip().lower() for c in list_cols}
    source = f"run:{run_id}"
    in_text = csv_path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csvmod.DictReader(io.StringIO(in_text))
    header = reader.fieldnames or []
    rows_out: list[dict] = []
    for r in reader:
        new = dict(r)
        for c in header:
            cl = c.strip().lower()
            v = new.get(c)
            if not v:
                continue
            if cl in file_cols_lc:
                # If the cell is a JSON dict (registered file record), pull the id.
                s = str(v).strip()
                if s[:1] == "{":
                    try:
                        obj = _json.loads(s)
                        if isinstance(obj, dict) and obj.get("id"):
                            new[c] = obj["id"]
                            continue
                        if isinstance(obj, dict) and obj.get("path"):
                            new[c] = fstore.absorb_path_or_id(obj["path"], source=source) or v
                            continue
                    except ValueError:
                        pass
                new[c] = fstore.absorb_path_or_id(s, source=source)
            elif cl in list_cols_lc:
                s = str(v).strip()
                if s[:1] == "[":
                    new[c] = fstore.absorb_list(s, source=source)
                else:
                    new[c] = fstore.absorb_list([s], source=source)
        rows_out.append(new)
    out_path = csv_path.with_suffix(".captured.csv")
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csvmod.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for r in rows_out:
            w.writerow(r)
    return out_path


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
    async def workflow_source_ep(wid: str):
        w = get_workflow(wid)
        if not w:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"source": workflow_source(wid) or "", "builtin": w.builtin}

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
                             body.get("attachPort"), body.get("agentId"), body.get("inputDatasetId"),
                             start_at=_resolve_at(body), every_seconds=body.get("everySeconds"),
                             timeout_sec=body.get("timeoutSec"))
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

    @app.post("/api/runs/{rid}/claim")
    async def claim_run(rid: str, body: dict = Body(...)):
        """An agent session adopts this run's completion (gets notified/woken when it
        finishes). Refused if another agent already owns it."""
        from .agents import get_agents
        sid = body.get("agentSessionId") or ""
        if not sid:
            return JSONResponse({"error": "agentSessionId required"}, status_code=400)
        return get_agents().claim_run(sid, rid)

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
        canonical way to capture a run's output into the persistent data layer.

        For workflows whose output_contract declares ``file`` / ``file_list``
        columns, any raw path or dict-record value in those cells is
        auto-registered into the file store and replaced with the resulting
        file id BEFORE ingest — so dataset cells always hold a clean opaque id."""
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
        # File-typed output columns: rewrite path-or-dict values in the CSV →
        # registered file ids. Working on a temp copy keeps the original
        # run output.csv untouched (re-captures stay idempotent).
        ingest_path = path
        if wf and wf.output_contract:
            file_cols = [c["name"] for c in wf.output_contract if c.get("type") == "file"]
            list_cols = [c["name"] for c in wf.output_contract if c.get("type") == "file_list"]
            if file_cols or list_cols:
                ingest_path = _absorb_run_files(path, rid, file_cols, list_cols)
        res = datastore.ingest_csv(ingest_path, target_id=body.get("datasetId"), name=name,
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
        try:
            ds = datastore.create_dataset(body.get("name", "Untitled"), body.get("columns") or [],
                                          body.get("dedupKeys"), body.get("source"), body.get("rows"))
            return {"dataset": ds}
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

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

    @app.post("/api/datasets/query-to-dataset")
    async def datasets_query_to_dataset(body: dict = Body(...)):
        """Materialise a read-only SELECT/WITH as a new dataset."""
        return datastore.query_to_dataset(body.get("sql", ""), body.get("name", "Query result"),
                                          body.get("dedupKeys"), int(body.get("maxRows", 50000)))

    @app.post("/api/datasets/exec")
    async def datasets_exec(body: dict = Body(...)):
        """Run a single INSERT/UPDATE/DELETE against the dataset tables (regexp_* available)."""
        return datastore.exec_sql(body.get("sql", ""))

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
        try:
            if "values" in body:
                return datastore.insert_row(did, body["values"])
            return datastore.append_rows(did, body.get("rows") or [], bool(body.get("dedup", True)),
                                         bool(body.get("extend", False)))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

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

    # ---------------------------------------------------------------- files
    # Static collection routes BEFORE the dynamic /{fid} routes (FastAPI matches in order).
    @app.get("/api/files")
    async def files_list(mime: str | None = None, source: str | None = None,
                         tag: str | None = None, search: str | None = None,
                         limit: int = 200, offset: int = 0):
        return fstore.list_files(mime=mime, source=source, tag=tag, search=search,
                                 limit=limit, offset=offset)

    @app.post("/api/files")
    async def files_upload(file: UploadFile = File(...),
                           name: str | None = Form(None),
                           source: str | None = Form(None),
                           tags: str | None = Form(None)):
        """Multipart upload. ``tags`` is an optional JSON array of strings."""
        try:
            data = await file.read()
            tag_list = jsonmod.loads(tags) if tags else None
            rec = fstore.register_from_bytes(data, name=name or file.filename or "upload",
                                             mime=file.content_type, source=source or "upload",
                                             tags=tag_list)
            return {"file": rec}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/api/files/fetch")
    async def files_fetch(body: dict = Body(...)):
        """Plain HTTP fetch (no browser cookies) → register. For session-locked
        downloads agents should use the browser_fetch MCP tool."""
        try:
            rec = fstore.register_from_url(body["url"], name=body.get("name"),
                                           headers=body.get("headers"),
                                           source=body.get("source"),
                                           tags=body.get("tags"))
            return {"file": rec}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/api/files/from-text")
    async def files_from_text(body: dict = Body(...)):
        try:
            rec = fstore.register_from_text(body["content"], name=body.get("name", "text.txt"),
                                            mime=body.get("mime", "text/plain"),
                                            source=body.get("source", "text"),
                                            tags=body.get("tags"))
            return {"file": rec}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/files/{fid}")
    async def file_get(fid: str):
        rec = fstore.get(fid)
        return {"file": rec} if rec else JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/api/files/{fid}/download")
    async def file_download(fid: str):
        rec = fstore.get(fid)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(rec["path"], media_type=rec["mime"], filename=rec["name"])

    @app.get("/api/files/{fid}/preview")
    async def file_preview(fid: str):
        """Inline preview (Content-Disposition: inline) — same bytes, but the
        browser displays it instead of downloading. Used by the FE thumbnail/
        modal renderer for images/video/pdf."""
        rec = fstore.get(fid)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(rec["path"], media_type=rec["mime"],
                            headers={"Content-Disposition": f'inline; filename="{rec["name"]}"'})

    @app.get("/api/files/{fid}/view")
    async def file_view(fid: str, max_bytes: int = 200_000):
        """Get text content (for textual MIME types) — used by the preview modal
        for code/json/csv/text. Non-text files return ``{text: null}``."""
        rec = fstore.get(fid)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"file": rec, "text": fstore.view_text(fid, max_bytes=max_bytes),
                "textual": fstore.is_textual(rec["mime"])}

    @app.post("/api/files/{fid}/rename")
    async def file_rename(fid: str, body: dict = Body(...)):
        rec = fstore.rename(fid, body.get("name", ""))
        return {"file": rec} if rec else JSONResponse({"error": "not found"}, status_code=404)

    @app.post("/api/files/{fid}/tags")
    async def file_tags(fid: str, body: dict = Body(...)):
        rec = fstore.set_tags(fid, body.get("tags") or [])
        return {"file": rec} if rec else JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/api/files/{fid}/references")
    async def file_refs(fid: str):
        return {"references": fstore.references(fid)}

    @app.delete("/api/files/{fid}")
    async def file_delete(fid: str, force: bool = False):
        return fstore.delete(fid, force=force)

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
        # never delete a profile a run or an agent is actively using
        if any(r["profileId"] == pid and r["status"] in ("starting", "running", "controlled")
               for r in mgr.list()):
            return JSONResponse({"error": "profile is busy with a run"}, status_code=400)
        if any(b.get("pid") == pid for b in mgr.agent_browsers.values()):
            return JSONResponse({"error": "profile is busy with an agent"}, status_code=400)
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

    @app.get("/api/system/deps")
    async def system_deps():
        """Status + install guidance for every external dependency (agent engines +
        the browser). Cross-platform, fault-tolerant — used by the UI to gate
        browser/agent actions and guide install when something is missing."""
        from . import deps
        return deps.deps_status()

    @app.get("/api/agents/sessions")
    async def agent_sessions():
        from .agents import get_agents
        return {"sessions": get_agents().list_sessions()}

    @app.post("/api/agents/sessions")
    async def agent_launch(body: dict = Body(...)):
        from .agents import get_agents
        try:
            s = get_agents().launch(body["agentId"], body.get("profileId") or "ephemeral",
                                    body.get("prompt", ""), bool(body.get("watch")),
                                    body.get("engine") or "codex", start_at=_resolve_at(body),
                                    file_ids=body.get("files") or None)
            return {"session": get_agents().get_session(s.id)}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/api/agents/sessions/{sid}/schedule")
    async def agent_schedule(sid: str, body: dict = Body(...)):
        from .agents import get_agents
        at = _resolve_at(body)
        if at is None:
            return JSONResponse({"error": "provide `inSeconds` or `at`/`startAt`"}, status_code=400)
        return get_agents().schedule_wake(sid, at, body.get("prompt", ""))

    @app.post("/api/agents/sessions/{sid}/cancel-schedule")
    async def agent_cancel_schedule(sid: str):
        from .agents import get_agents
        return get_agents().cancel_schedule(sid)

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
        return get_agents().steer(sid, body.get("message", ""), file_ids=body.get("files") or None)

    @app.post("/api/agents/sessions/{sid}/stop")
    async def agent_stop(sid: str):
        from .agents import get_agents
        return await get_agents().stop(sid)

    @app.post("/api/agents/sessions/{sid}/browser")
    async def agent_browser(sid: str, body: dict = Body(...)):
        from .agents import get_agents
        res = await get_agents().control_browser(sid, body.get("action", ""))
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    @app.post("/api/agents/sessions/{sid}/cancel-steer")
    async def agent_cancel_steer(sid: str, body: dict = Body(...)):
        """Remove a queued steer (for the UI list of pending messages above the input)."""
        from .agents import get_agents
        return get_agents().cancel_steer(sid, int(body.get("index", -1)))

    @app.get("/api/agents/sessions/{sid}/events/stream")
    async def agent_events_stream(sid: str):
        """Server-Sent Events stream of this session's events. The frontend uses it
        to render chunks live (no polling). On connect, replays all events so far so
        a reconnect / late subscriber catches up without a separate fetch."""
        from .agents import get_agents
        mgr = get_agents()
        if not mgr.get_session(sid):
            return JSONResponse({"error": "not found"}, status_code=404)
        async def gen():
            q = mgr.subscribe(sid)
            try:
                # replay backlog first so reconnects see everything
                for ev in list(mgr.get_events(sid)):
                    yield f"data: {jsonmod.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                # heartbeat marker so the client knows the replay is done
                yield "event: ready\ndata: {}\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=20)
                        yield f"data: {jsonmod.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        # keep-alive comment so proxies / electron don't close the stream
                        yield ": keep-alive\n\n"
            finally:
                mgr.unsubscribe(sid, q)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-transform",
                                          "X-Accel-Buffering": "no"})

    @app.post("/api/agents/sessions/{sid}/fake-notify")
    async def agent_fake_notify(sid: str, body: dict = Body(default=None)):
        """Dev/test hook: synthesize a notification for this session so we can exercise
        the preemption/wake/coalescing paths without launching a real workflow."""
        from .agents import get_agents
        b = body or {}
        kind = b.get("kind", "workflow_finished")
        payload = b.get("payload") or {"runId": b.get("runId", "fake"),
                                       "status": b.get("status", "succeeded"),
                                       "workflow": b.get("workflow", "fake-workflow"),
                                       "rows": b.get("rows", 1)}
        get_agents().notify(sid, kind, payload)
        return {"ok": True}

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
