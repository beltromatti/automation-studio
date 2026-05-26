"""Automation Studio MCP server — the single tool surface agents act through.

A minimal, dependency-free stdio MCP server (newline-delimited JSON-RPC 2.0) so
both Claude Code and Codex — which are first-class MCP clients — drive the app
and the browser through the *same* tools, with no extra Python deps (bundles
cleanly in the frozen backend). It is spawned per agent run with env that scopes
it to that agent's profile:

    AUTOMATION_BACKEND_URL   the orchestrator API (datasets / workflows / runs)
    MCP_CONTROL_PORT         the browser control-server port the agent owns ("" = no browser)
    AGENT_ID, AGENT_PROFILE_ID

Two namespaces of tools:
  studio_*   — workflows, runs, and the SQLite Data layer (high-level app control)
  browser_*  — drive the single owned browser (goto/observe/click/type/eval/…),
               1:1 with the humanbrowser control-server, registered only when the
               agent owns a browser session.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BACKEND = os.environ.get("AUTOMATION_BACKEND_URL", "http://127.0.0.1:8765").rstrip("/")
CONTROL_PORT = os.environ.get("MCP_CONTROL_PORT", "").strip()
CONTROL = f"http://127.0.0.1:{CONTROL_PORT}" if CONTROL_PORT else ""
AGENT_PROFILE_ID = os.environ.get("AGENT_PROFILE_ID", "ephemeral")
AGENT_ID = os.environ.get("AGENT_ID", "")
CONTROL_PORT_INT = int(CONTROL_PORT) if CONTROL_PORT.isdigit() else None
PROTOCOL_FALLBACK = "2024-11-05"


_DEBUG = os.environ.get("MCP_DEBUG_LOG", "").strip()


def _log(msg: str) -> None:
    sys.stderr.write(f"[mcp] {msg}\n")
    sys.stderr.flush()
    if _DEBUG:
        try:
            with open(_DEBUG, "a") as f:
                f.write(f"{time.time():.3f} {msg}\n")
        except Exception:
            pass


def _http(method: str, url: str, body=None, timeout: float = 120) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode() or "{}")
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


def _api(method: str, path: str, body=None, timeout: float = 120) -> dict:
    return _http(method, BACKEND + path, body, timeout)


def _ctrl(method: str, path: str, body=None, timeout: float = 120) -> dict:
    if not CONTROL:
        return {"error": "this agent does not own a browser session"}
    return _http(method, CONTROL + path, body, timeout)


# ---------------------------------------------------------------------- tools
def _need_browser() -> dict | None:
    return None if CONTROL else {"error": "no browser: this agent has no owned browser session"}


def t_list_workflows(_a):
    d = _api("GET", "/api/workflows")
    return [{"id": w["id"], "name": w["name"], "description": w["description"],
             "params": [{"name": p["name"], "type": p["type"], "required": p.get("required", False),
                         "help": p.get("help", ""), "options": p.get("options")} for p in w["params"]],
            "outputContract": w.get("outputContract", [])} for w in d.get("workflows", [])]


def t_list_datasets(_a):
    return _api("GET", "/api/datasets").get("datasets", [])


def t_dataset_schema(_a):
    return _api("GET", "/api/datasets/schema").get("schema", [])


def t_query_data(a):
    return _api("POST", "/api/datasets/query", {"sql": a["sql"], "maxRows": a.get("maxRows", 1000)})


def t_dataset_rows(a):
    qs = f"limit={a.get('limit', 100)}&offset={a.get('offset', 0)}&search={a.get('search', '')}"
    return _api("GET", f"/api/datasets/{a['datasetId']}/rows?{qs}")


def t_dataset_create(a):
    return _api("POST", "/api/datasets", {"name": a["name"], "columns": a.get("columns", []),
                                          "dedupKeys": a.get("dedupKeys"), "source": {"kind": "manual"}})


def t_dataset_append(a):
    return _api("POST", f"/api/datasets/{a['datasetId']}/rows",
                {"rows": a["rows"], "dedup": a.get("dedup", True), "extend": a.get("extend", True)})


def t_dataset_project(a):
    return _api("POST", "/api/datasets/project", {"srcId": a["srcId"], "columns": a["columns"],
                                                  "name": a["name"], "dedupKeys": a.get("dedupKeys")})


def t_create_workflow(a):
    body = {k: a.get(k) for k in ("id", "name", "description", "code", "params", "outputContract",
                                  "profile", "profileName", "needsAuth", "icon")}
    body["createdBy"] = "agent"
    return _api("POST", "/api/workflows", body)


def t_workflow_source(a):
    return _api("GET", f"/api/workflows/{a['workflowId']}/source")


def t_run_workflow(a):
    profile = a.get("profileId") or AGENT_PROFILE_ID
    body = {"workflowId": a["workflowId"], "params": a.get("params", {}),
            "profileId": profile, "watch": a.get("watch", False),
            "datasetId": a.get("datasetId"), "agentId": AGENT_ID or None}
    # When the agent owns a browser and runs on its own profile, the workflow
    # shares that browser (attach) instead of launching a second one.
    if CONTROL_PORT_INT and profile == AGENT_PROFILE_ID:
        body["attachPort"] = CONTROL_PORT_INT
    d = _api("POST", "/api/runs", body)
    run = d.get("run") or {}
    return {"runId": run.get("id"), "status": run.get("status"), "error": d.get("error")}


def _run_brief(run: dict) -> dict:
    return {k: run.get(k) for k in ("id", "status", "rows", "error", "csvPath", "datasetId")
            if run.get(k) is not None} | {"progress": run.get("progress")}


def t_run_status(a):
    d = _api("GET", f"/api/runs/{a['runId']}")
    return _run_brief(d.get("run") or {})


def t_wait_run(a):
    rid = a["runId"]
    deadline = time.time() + float(a.get("timeoutSec", 600))
    terminal = {"succeeded", "failed", "canceled"}
    while time.time() < deadline:
        run = (_api("GET", f"/api/runs/{rid}").get("run") or {})
        if run.get("status") in terminal:
            return _run_brief(run)
        time.sleep(2)
    return {"status": "timeout", "note": "still running after timeout; check run_status later"}


def t_browser_goto(a):
    return _need_browser() or _ctrl("POST", "/goto", {"url": a["url"]})


def t_browser_observe(_a):
    return _need_browser() or _ctrl("GET", "/observe?format=text")


def t_browser_click(a):
    return _need_browser() or _ctrl("POST", "/act", {"action": "click", "index": int(a["index"])})


def t_browser_type(a):
    return _need_browser() or _ctrl("POST", "/act", {"action": "type", "index": int(a["index"]),
                                                     "text": a["text"], "clear": a.get("clear", False),
                                                     "enter": a.get("enter", False)})


def t_browser_press(a):
    return _need_browser() or _ctrl("POST", "/act", {"action": "press", "key": a["key"]})


def t_browser_scroll(a):
    return _need_browser() or _ctrl("POST", "/act", {"action": "scroll", "dy": int(a.get("dy", 600))})


def t_browser_read_text(_a):
    return _need_browser() or _ctrl("GET", "/text")


def t_browser_eval(a):
    return _need_browser() or _ctrl("POST", "/eval", {"script": a["script"]})


def t_browser_screenshot(_a):
    return _need_browser() or _ctrl("GET", "/screenshot")


def t_browser_current_url(_a):
    return _need_browser() or _ctrl("GET", "/status")


STUDIO_TOOLS = [
    ("studio_list_workflows", "List the available workflows (with their params and output columns).",
     {"type": "object", "properties": {}}, t_list_workflows),
    ("studio_list_datasets", "List all persistent datasets (id, name, columns, dedup keys, row counts).",
     {"type": "object", "properties": {}}, t_list_datasets),
    ("studio_dataset_schema", "Get every dataset's physical SQL table name and columns, so you can write SQL for studio_query_data.",
     {"type": "object", "properties": {}}, t_dataset_schema),
    ("studio_query_data", "Run a read-only SELECT/WITH query across the dataset tables (join/aggregate/filter).",
     {"type": "object", "properties": {"sql": {"type": "string"}, "maxRows": {"type": "integer"}}, "required": ["sql"]}, t_query_data),
    ("studio_dataset_rows", "Read rows from a dataset (paginated, optional text search).",
     {"type": "object", "properties": {"datasetId": {"type": "string"}, "limit": {"type": "integer"},
                                       "offset": {"type": "integer"}, "search": {"type": "string"}}, "required": ["datasetId"]}, t_dataset_rows),
    ("studio_dataset_create", "Create a new dataset. columns: [{name,type:text|number|boolean}]. dedupKeys: column display names.",
     {"type": "object", "properties": {"name": {"type": "string"}, "columns": {"type": "array"},
                                       "dedupKeys": {"type": "array"}}, "required": ["name"]}, t_dataset_create),
    ("studio_dataset_append", "Append rows (list of objects keyed by column name) to a dataset; dedups by the dataset's keys and extends the schema for new columns.",
     {"type": "object", "properties": {"datasetId": {"type": "string"}, "rows": {"type": "array"},
                                       "dedup": {"type": "boolean"}, "extend": {"type": "boolean"}}, "required": ["datasetId", "rows"]}, t_dataset_append),
    ("studio_dataset_project", "Create a new dataset from selected/renamed columns of another (prep a tidy input for the next workflow). columns: [{from,to}] or [name].",
     {"type": "object", "properties": {"srcId": {"type": "string"}, "columns": {"type": "array"},
                                       "name": {"type": "string"}, "dedupKeys": {"type": "array"}}, "required": ["srcId", "columns", "name"]}, t_dataset_project),
    ("studio_create_workflow", "Create (or update) a reusable workflow from Python code. The code must define main(argv) and should use `from automations import userkit` (userkit.parse(argv) -> params,server,output; userkit.run_session(fn,params,server); userkit.write_csv(output,rows,columns); userkit.progress/log/error). params: [{name,label,type:string|number|boolean|select,default,help,options}]. outputContract: [{name,type}]. profile: 'ephemeral'|'shared'. Appears in the app like a built-in (tagged 'agent').",
     {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"},
                                       "description": {"type": "string"}, "code": {"type": "string"},
                                       "params": {"type": "array"}, "outputContract": {"type": "array"},
                                       "profile": {"type": "string"}, "needsAuth": {"type": "boolean"},
                                       "icon": {"type": "string"}}, "required": ["name", "code"]}, t_create_workflow),
    ("studio_workflow_source", "Read the Python source of a user/agent workflow (to inspect or modify it).",
     {"type": "object", "properties": {"workflowId": {"type": "string"}}, "required": ["workflowId"]}, t_workflow_source),
    ("studio_run_workflow", "Start a workflow run. Defaults to this agent's own profile. Optionally bind a datasetId to auto-append the result on success. Returns runId.",
     {"type": "object", "properties": {"workflowId": {"type": "string"}, "params": {"type": "object"},
                                       "profileId": {"type": "string"}, "datasetId": {"type": "string"},
                                       "watch": {"type": "boolean"}}, "required": ["workflowId"]}, t_run_workflow),
    ("studio_run_status", "Get a run's current status, progress, row count and error.",
     {"type": "object", "properties": {"runId": {"type": "string"}}, "required": ["runId"]}, t_run_status),
    ("studio_wait_run", "Block until a run reaches a terminal state (succeeded/failed/canceled) or the timeout, then return its status. Use this instead of polling.",
     {"type": "object", "properties": {"runId": {"type": "string"}, "timeoutSec": {"type": "integer"}}, "required": ["runId"]}, t_wait_run),
]

BROWSER_TOOLS = [
    ("browser_goto", "Navigate the owned browser to a URL.",
     {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, t_browser_goto),
    ("browser_observe", "Observe the current page: returns a compact, indexed snapshot of interactive elements + text. Use the [index] numbers with browser_click / browser_type.",
     {"type": "object", "properties": {}}, t_browser_observe),
    ("browser_click", "Click the element at the given [index] from browser_observe.",
     {"type": "object", "properties": {"index": {"type": "integer"}}, "required": ["index"]}, t_browser_click),
    ("browser_type", "Type into the element at [index]. Set enter=true to submit, clear=true to clear first.",
     {"type": "object", "properties": {"index": {"type": "integer"}, "text": {"type": "string"},
                                       "enter": {"type": "boolean"}, "clear": {"type": "boolean"}}, "required": ["index", "text"]}, t_browser_type),
    ("browser_press", "Press a key (e.g. Enter, Escape, ArrowDown).",
     {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}, t_browser_press),
    ("browser_scroll", "Scroll the page by dy pixels (default 600).",
     {"type": "object", "properties": {"dy": {"type": "integer"}}}, t_browser_scroll),
    ("browser_read_text", "Get the full visible text of the current page.",
     {"type": "object", "properties": {}}, t_browser_read_text),
    ("browser_eval", "Evaluate a JavaScript expression/function in the page and return the result. Full flexibility for custom extraction.",
     {"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]}, t_browser_eval),
    ("browser_screenshot", "Take a screenshot of the current page; returns the file path.",
     {"type": "object", "properties": {}}, t_browser_screenshot),
    ("browser_current_url", "Get the current page URL and title.",
     {"type": "object", "properties": {}}, t_browser_current_url),
]


def _tools() -> list:
    tools = list(STUDIO_TOOLS)
    if CONTROL:
        tools += BROWSER_TOOLS
    return tools


def _tool_defs() -> list[dict]:
    return [{"name": n, "description": d, "inputSchema": s} for (n, d, s, _f) in _tools()]


_HANDLERS = {n: f for (n, _d, _s, f) in (STUDIO_TOOLS + BROWSER_TOOLS)}


# ---------------------------------------------------------------------- JSON-RPC
def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
    if _DEBUG:
        _log(f"=> {json.dumps(obj)[:200]}")


def _result(rid, result) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _handle(msg: dict) -> None:
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_FALLBACK
        _result(rid, {"protocolVersion": proto, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "automation-studio", "version": "0.1.0"}})
    elif method in ("notifications/initialized", "initialized"):
        pass  # notification, no response
    elif method == "ping":
        _result(rid, {})
    elif method == "tools/list":
        _result(rid, {"tools": _tool_defs()})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = _HANDLERS.get(name)
        if not fn or (name in {n for n, *_ in BROWSER_TOOLS} and not CONTROL):
            _result(rid, {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True})
            return
        try:
            out = fn(args)
            text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
            is_err = isinstance(out, dict) and bool(out.get("error"))
            _result(rid, {"content": [{"type": "text", "text": text}], "isError": is_err})
        except Exception as e:
            _log(f"tool {name} failed: {e}")
            _result(rid, {"content": [{"type": "text", "text": f"tool error: {e}"}], "isError": True})
    elif rid is not None:
        _error(rid, -32601, f"method not found: {method}")


def main(argv=None) -> int:
    _log(f"started backend={BACKEND} control={CONTROL or '(none)'} profile={AGENT_PROFILE_ID} "
         f"tools={len(_tools())}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _DEBUG:
            _log(f"<= {line[:200]}")
        try:
            _handle(msg)
        except Exception as e:
            _log(f"handler crash: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
