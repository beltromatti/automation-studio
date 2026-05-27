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

import base64
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
# the SESSION that owns this MCP server — runs launched here are owned by it and
# its completion notification is delivered to it (falls back to AGENT_ID).
AGENT_SESSION_ID = os.environ.get("AGENT_SESSION_ID", "") or AGENT_ID
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


def _workflow_view(w: dict) -> dict:
    return {"id": w["id"], "name": w["name"], "description": w["description"],
            "builtin": w.get("builtin"), "createdBy": w.get("createdBy"),
            "profile": w.get("profile"), "needsAuth": w.get("needsAuth"),
            "params": [{"name": p["name"], "label": p.get("label"), "type": p["type"],
                        "required": p.get("required", False), "default": p.get("default"),
                        "help": p.get("help", ""), "options": p.get("options")} for p in w.get("params", [])],
            "inputContract": w.get("inputContract", []), "outputContract": w.get("outputContract", [])}


def t_list_workflows(_a):
    d = _api("GET", "/api/workflows")
    return [_workflow_view(w) for w in d.get("workflows", [])]


def t_get_workflow(a):
    wid = a["workflowId"]
    d = _api("GET", "/api/workflows")
    w = next((x for x in d.get("workflows", []) if x["id"] == wid), None)
    if not w:
        return {"error": f"no workflow '{wid}'"}
    out = _workflow_view(w)
    if a.get("includeSource", True):
        out["source"] = _api("GET", f"/api/workflows/{wid}/source").get("source", "")
    return out


def t_list_datasets(_a):
    return _api("GET", "/api/datasets").get("datasets", [])


def t_dataset_schema(_a):
    return _api("GET", "/api/datasets/schema").get("schema", [])


def t_query_data(a):
    return _api("POST", "/api/datasets/query", {"sql": a["sql"], "maxRows": a.get("maxRows", 1000)})


def t_query_to_dataset(a):
    return _api("POST", "/api/datasets/query-to-dataset",
                {"sql": a["sql"], "name": a.get("name", "Query result"),
                 "dedupKeys": a.get("dedupKeys"), "maxRows": a.get("maxRows", 50000)})


def t_exec_sql(a):
    return _api("POST", "/api/datasets/exec", {"sql": a["sql"]})


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
    body = {k: a.get(k) for k in ("id", "name", "description", "code", "params",
                                  "outputContract", "inputContract", "profile", "profileName",
                                  "needsAuth", "icon") if a.get(k) is not None}
    body["createdBy"] = "agent"
    return _api("POST", "/api/workflows", body)


def t_workflow_source(a):
    return _api("GET", f"/api/workflows/{a['workflowId']}/source")


def t_run_workflow(a):
    profile = a.get("profileId") or AGENT_PROFILE_ID
    body = {"workflowId": a["workflowId"], "params": a.get("params", {}),
            "profileId": profile, "watch": a.get("watch", False),
            "datasetId": a.get("datasetId"), "inputDatasetId": a.get("inputDatasetId"),
            "agentId": AGENT_SESSION_ID or None}
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


def t_list_runs(a):
    runs = _api("GET", "/api/runs").get("runs", [])
    runs = runs[: int(a.get("limit", 25))]
    return [{k: r.get(k) for k in ("id", "workflowId", "workflowName", "status", "rows",
                                   "profileId", "error", "createdAt") if r.get(k) is not None}
            | {"progress": r.get("progress")} for r in runs]


def t_schedule_workflow(a):
    body = {"workflowId": a["workflowId"], "params": a.get("params", {}),
            "profileId": a.get("profileId") or AGENT_PROFILE_ID,
            "datasetId": a.get("datasetId"), "inputDatasetId": a.get("inputDatasetId"),
            "agentId": AGENT_SESSION_ID or None,
            "inSeconds": a.get("inSeconds"), "at": a.get("at"), "everySeconds": a.get("everySeconds")}
    d = _api("POST", "/api/runs", body)
    run = d.get("run") or {}
    return {"runId": run.get("id"), "status": run.get("status"), "startAt": run.get("startAt"),
            "everySeconds": run.get("everySeconds"), "error": d.get("error")}


def t_schedule_wake(a):
    if not AGENT_SESSION_ID:
        return {"error": "no agent session bound"}
    return _api("POST", f"/api/agents/sessions/{AGENT_SESSION_ID}/schedule",
                {"inSeconds": a.get("inSeconds"), "at": a.get("at"), "prompt": a.get("prompt", "")})


def t_cancel_schedule(a):
    if a.get("runId"):
        return _api("POST", f"/api/runs/{a['runId']}/cancel")
    if not AGENT_SESSION_ID:
        return {"error": "no agent session bound"}
    return _api("POST", f"/api/agents/sessions/{AGENT_SESSION_ID}/cancel-schedule")


def t_run_logs(a):
    d = _api("GET", f"/api/runs/{a['runId']}")
    logs = d.get("logs") or []
    tail = int(a.get("tail", 80))
    return {"logs": logs[-tail:], "status": (d.get("run") or {}).get("status")}


def t_cancel_run(a):
    return _api("POST", f"/api/runs/{a['runId']}/cancel")


def t_claim_run(a):
    return _api("POST", f"/api/runs/{a['runId']}/claim", {"agentSessionId": AGENT_SESSION_ID})


def t_run_result(a):
    return _api("GET", f"/api/runs/{a['runId']}/result")


def t_run_to_dataset(a):
    return _api("POST", f"/api/runs/{a['runId']}/to-dataset",
                {k: a.get(k) for k in ("datasetId", "name", "dedupKeys") if a.get(k) is not None})


# ---- datasets: full editing power (parity with the Data screen) -------------
def t_dataset_rename(a):
    return _api("POST", f"/api/datasets/{a['datasetId']}/rename", {"name": a["name"]})


def t_dataset_delete(a):
    return _api("DELETE", f"/api/datasets/{a['datasetId']}")


def t_dataset_merge(a):
    return _api("POST", "/api/datasets/merge", {"ids": a["ids"], "name": a.get("name", "Merged"),
                                                "dedupKeys": a.get("dedupKeys")})


def t_dataset_import(a):
    return _api("POST", "/api/datasets/import", {"csv": a["csv"], "name": a.get("name", "Imported"),
                                                 "datasetId": a.get("datasetId"), "dedupKeys": a.get("dedupKeys")})


def t_dataset_update_cell(a):
    return _api("POST", f"/api/datasets/{a['datasetId']}/cell",
                {"rid": int(a["rowId"]), "column": a["column"], "value": a.get("value")})


def t_dataset_delete_rows(a):
    return _api("POST", f"/api/datasets/{a['datasetId']}/delete-rows", {"rids": a["rowIds"]})


def t_dataset_add_column(a):
    return _api("POST", f"/api/datasets/{a['datasetId']}/add-column",
                {"display": a["name"], "type": a.get("type", "text")})


def t_dataset_drop_column(a):
    return _api("POST", f"/api/datasets/{a['datasetId']}/drop-column", {"display": a["name"]})


def t_dataset_rename_column(a):
    return _api("POST", f"/api/datasets/{a['datasetId']}/rename-column",
                {"from": a["from"], "to": a["to"]})


def t_dataset_set_dedup_keys(a):
    return _api("POST", f"/api/datasets/{a['datasetId']}/dedup-keys", {"keys": a["keys"]})


def t_dataset_dedup(a):
    return _api("POST", f"/api/datasets/{a['datasetId']}/dedup", {"keys": a.get("keys")})


# ---- profiles & workflows ----------------------------------------------------
def t_list_profiles(_a):
    items = _api("GET", "/api/profiles").get("profiles", [])
    return [{k: p.get(k) for k in ("id", "name", "open", "openPort") if p.get(k) is not None} for p in items]


def t_create_profile(a):
    return _api("POST", "/api/profiles", {"name": a.get("name", "Profile")})


def t_delete_workflow(a):
    return _api("DELETE", f"/api/workflows/{a['workflowId']}")


# The browser surface gives agents the SAME power as direct scripting against the
# control server: observe() pierces shadow DOM AND same-origin iframes, and every
# node carries index, tag, accessible name, attrs, inViewport, center [x,y], xpath
# and frame ("" main / "/shadow…" / "/iframe…"). That metadata is what lets an
# agent disambiguate look-alike controls (e.g. an in-card button under <main> vs a
# duplicate sticky-header/nav one) and reach controls plain page JS can't (eval is
# main-frame light-DOM only; the shadow-DOM invite dialog is only reachable via
# observe+click by index). browser_observe shows a readable outline by default and
# the full structured nodes on demand; browser_inspect zooms into matches.
def _observe(max_nodes: int = 1200) -> dict:
    return _ctrl("GET", f"/observe?format=json&max_nodes={int(max_nodes)}")


def _frame_marker(frame: str) -> str:
    f = frame or ""
    tags = []
    if "/iframe" in f:
        tags.append("iframe")
    if "/shadow" in f:
        tags.append("shadow")
    return f" @{'+'.join(tags)}" if tags else ""


def _fmt_node(n: dict) -> str:
    a = n.get("attrs") or {}
    parts = []
    for k in ("type", "role", "href", "value", "placeholder", "expanded", "selected", "checked", "disabled"):
        if k in a:
            v = a[k]
            parts.append(k if v is True else f'{k}="{v}"')
    attr = (" " + " ".join(parts)) if parts else ""
    name = n.get("name") or ""
    body = f" {name}" if name else ""
    off = "" if n.get("inViewport", True) else " (offscreen)"
    return f'[{n["index"]}]<{n.get("tag", "")}{attr}>{body}{_frame_marker(n.get("frame", ""))}{off}'


def _node_key(n: dict) -> tuple:
    """Identity for dedup: same tag + accessible name + href => 'the same kind of
    control' (e.g. a card's 6 identical photo links)."""
    a = n.get("attrs") or {}
    return (n.get("tag"), (n.get("name") or "").strip(), a.get("href"))


def _outline(d: dict, max_chars: int = 14_000, dedup: bool = False) -> str:
    nodes = d.get("nodes") or d.get("elements") or []
    header = (
        f'URL: {d.get("url", "")}\nTITLE: {d.get("title", "")}\n'
        f'SCROLL: y={d.get("scroll_y", 0)} of {d.get("scroll_height", 0)} '
        f'{"[more below — scroll]" if d.get("has_more_below") else "[bottom]"}\n'
        f'INTERACTIVE ELEMENTS: {d.get("num_elements", 0)}{" (truncated)" if d.get("truncated") else ""} '
        f'— "@shadow"/"@iframe" mark elements reachable only via observe+click (not eval); '
        f'"(offscreen)" = not in viewport. Use browser_inspect for xpath/coords to disambiguate'
        f'{", browser_extract for repeated structured data" if not dedup else ""}.\n--- page ---\n'
    )
    lines: list[str] = []
    # consecutive-run dedup: collapse a run of identical look-alike elements into
    # one line with a "(xN, idx ...)" tag, so repeated cards/photo-links don't drown
    # the snapshot. A text node (or a different element) breaks the run. Off by
    # default — the full snapshot stays available for precise/low-level work.
    pend = None  # {"line": str, "idxs": [int], "key": tuple}

    def flush():
        if not pend:
            return
        if len(pend["idxs"]) > 1:
            shown = ", ".join(str(i) for i in pend["idxs"][:5])
            more = "…" if len(pend["idxs"]) > 5 else ""
            lines.append(f'{pend["line"]}  (×{len(pend["idxs"])} similar — idx {shown}{more})')
        else:
            lines.append(pend["line"])

    for n in nodes:
        is_el = n.get("type") == "element" or "index" in n
        if dedup and is_el:
            k = _node_key(n)
            if pend and pend["key"] == k:
                pend["idxs"].append(n.get("index"))
                continue
            flush()
            pend = {"line": _fmt_node(n), "idxs": [n.get("index")], "key": k}
            continue
        flush(); pend = None
        if is_el:
            lines.append(_fmt_node(n))
        elif n.get("text"):
            lines.append(n["text"])
    flush()
    body = "\n".join(l for l in lines if l)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n… [snapshot truncated — browser_inspect/browser_extract to zoom, dedup=true, or raise maxNodes]"
    return header + body


_EXTRACT_JS = r"""(() => {
  const CONTAINER = __CONTAINER__, FIELDS = __FIELDS__, LIMIT = __LIMIT__;
  const txt = (el) => el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : null;
  const one = (el, spec) => {
    if (!spec || spec === 'text') return txt(el);
    let sel = spec, attr = null; const at = spec.indexOf('@');
    if (at >= 0) { sel = spec.slice(0, at); attr = spec.slice(at + 1); }
    const t = sel ? el.querySelector(sel) : el;
    if (!t) return null;
    if (attr) { const v = t.getAttribute(attr); return v == null ? null : v; }
    return txt(t);
  };
  let items = [], used = CONTAINER;
  if (CONTAINER) {
    items = Array.from(document.querySelectorAll(CONTAINER));
  } else {
    const sig = new Map();
    document.querySelectorAll('*').forEach(el => {
      if (!el.querySelector('a[href]')) return;
      if ((el.innerText || '').replace(/\s+/g, ' ').trim().length < 40) return;
      const cls = (el.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean).slice(0, 3).join('.');
      const k = el.tagName.toLowerCase() + (cls ? '.' + cls : '');
      (sig.get(k) || sig.set(k, []).get(k)).push(el);
    });
    let bestN = 2;
    for (const [k, els] of sig) if (els.length > bestN) { bestN = els.length; items = els; used = k; }
  }
  const fields = (FIELDS && Object.keys(FIELDS).length) ? FIELDS : { text: 'text', href: 'a@href' };
  const rows = items.slice(0, LIMIT).map(el => {
    const r = {}; for (const [n, s] of Object.entries(fields)) r[n] = one(el, s); return r;
  });
  return { container: used || null, count: rows.length, rows };
})"""


def t_browser_goto(a):
    return _need_browser() or _ctrl("POST", "/goto", {"url": a["url"]})


def t_browser_observe(a):
    err = _need_browser()
    if err:
        return err
    d = _observe(int(a.get("maxNodes", 1200)))
    if d.get("error"):
        return d
    if a.get("format") == "full":
        return {k: d.get(k) for k in ("url", "title", "scroll_y", "scroll_height",
                                      "has_more_below", "num_elements", "truncated")} | {"elements": d.get("elements")}
    return _outline(d, dedup=bool(a.get("dedup")))


def t_browser_extract(a):
    """Structured bulk extraction from repeated page content — light-DOM, deterministic,
    one call. Returns a row per matched container."""
    err = _need_browser()
    if err:
        return err
    container = a.get("container")
    fields = a.get("fields") or {}
    limit = int(a.get("limit", 200))
    js = (_EXTRACT_JS
          .replace("__CONTAINER__", json.dumps(container) if container else "null")
          .replace("__FIELDS__", json.dumps(fields))
          .replace("__LIMIT__", str(limit)))
    r = _ctrl("POST", "/eval", {"script": js})
    if r.get("error"):
        return r
    return r.get("result")


def t_browser_inspect(a):
    """Zoom into elements matching an accessible-name substring / tag / frame and
    return full metadata (index, tag, name, attrs, inViewport, center, xpath,
    frame) — the way to disambiguate look-alike controls and pick the right
    [index] to click."""
    err = _need_browser()
    if err:
        return err
    d = _observe(int(a.get("maxNodes", 1200)))
    if d.get("error"):
        return d
    match = (a.get("match") or "").lower()
    tag = (a.get("tag") or "").lower()
    frame = a.get("frame")  # None | "main" | "shadow" | "iframe"
    out = []
    for n in d.get("elements") or []:
        nm = n.get("name") or ""
        if match and match not in nm.lower():
            continue
        if tag and (n.get("tag") or "").lower() != tag:
            continue
        f = n.get("frame") or ""
        if frame in ("main", "") and frame is not None and f != "":
            continue
        if frame == "shadow" and "/shadow" not in f:
            continue
        if frame == "iframe" and "/iframe" not in f:
            continue
        out.append({"index": n.get("index"), "tag": n.get("tag"), "name": nm,
                    "attrs": n.get("attrs"), "inViewport": n.get("inViewport"),
                    "center": n.get("center"), "xpath": n.get("xpath"), "frame": f})
    return {"count": len(out), "matches": out}


def t_browser_click(a):
    return _need_browser() or _ctrl("POST", "/act", {"action": "click", "index": int(a["index"])})


def t_browser_type(a):
    return _need_browser() or _ctrl("POST", "/act", {"action": "type", "index": int(a["index"]),
                                                     "text": a["text"], "clear": a.get("clear", False),
                                                     "enter": a.get("enter", False)})


def t_browser_press(a):
    return _need_browser() or _ctrl("POST", "/act", {"action": "press", "key": a["key"]})


def t_browser_scroll(a):
    err = _need_browser()
    if err:
        return err
    to = a.get("to")
    if to == "top":
        return _ctrl("POST", "/eval", {"script": "() => window.scrollTo(0, 0)"})
    if to == "bottom":
        return _ctrl("POST", "/eval", {"script": "() => window.scrollTo(0, document.documentElement.scrollHeight)"})
    return _ctrl("POST", "/act", {"action": "scroll", "dy": int(a.get("dy", 600))})


def t_browser_wait(a):
    """Poll until an accessible-name substring (`match`, shadow/iframe-aware via
    observe) or a main-frame CSS `selector` appears, or timeout. Returns the
    matched element (with its current [index]) so you can act on it right away."""
    err = _need_browser()
    if err:
        return err
    match = (a.get("match") or "").lower()
    selector = a.get("selector")
    timeout = float(a.get("timeoutSec", 10))
    deadline = time.time() + timeout
    while True:
        if selector:
            r = _ctrl("POST", "/eval", {"script": f"() => !!document.querySelector({json.dumps(selector)})"})
            if r.get("result") is True:
                return {"found": True, "by": "selector", "selector": selector}
        if match:
            d = _observe()
            for n in d.get("elements") or []:
                if match in (n.get("name") or "").lower():
                    return {"found": True, "by": "match", "index": n.get("index"), "name": n.get("name"),
                            "frame": n.get("frame") or "", "inViewport": n.get("inViewport"), "xpath": n.get("xpath")}
        if time.time() >= deadline:
            return {"found": False, "timeoutSec": timeout}
        time.sleep(0.6)


def t_browser_read_text(_a):
    return _need_browser() or _ctrl("GET", "/text")


def t_browser_eval(a):
    return _need_browser() or _ctrl("POST", "/eval", {"script": a["script"]})


def t_browser_screenshot(_a):
    err = _need_browser()
    if err:
        return err
    r = _ctrl("GET", "/screenshot")
    path = r.get("path") if isinstance(r, dict) else None
    if not path:
        return r
    try:  # return the image INLINE (no Read round-trip), with the path as fallback
        with open(path, "rb") as f:
            return {"path": path, "image_b64": base64.b64encode(f.read()).decode(), "mimeType": "image/png"}
    except Exception:
        return {"path": path}


def t_browser_current_url(_a):
    return _need_browser() or _ctrl("GET", "/status")


STUDIO_TOOLS = [
    ("studio_list_workflows", "List all workflows with their full settings: params (name/label/type/required/default/options), input & output contracts, profile, needsAuth, and whether each is a read-only built-in or a user/agent workflow.",
     {"type": "object", "properties": {}}, t_list_workflows),
    ("studio_get_workflow", "Get one workflow's full settings AND its Python source code (set includeSource=false to skip). Reading a built-in's code is a great way to learn how to drive a platform — e.g. read the LinkedIn workflows to see exactly how the browser is navigated, then apply the same patterns yourself.",
     {"type": "object", "properties": {"workflowId": {"type": "string"}, "includeSource": {"type": "boolean"}}, "required": ["workflowId"]}, t_get_workflow),
    ("studio_list_datasets", "List all persistent datasets (id, name, columns, dedup keys, row counts).",
     {"type": "object", "properties": {}}, t_list_datasets),
    ("studio_dataset_schema", "Get every dataset's physical SQL table name and columns, so you can write SQL for studio_query_data.",
     {"type": "object", "properties": {}}, t_dataset_schema),
    ("studio_query_data", "Run a read-only SELECT/WITH query across the dataset tables (join/aggregate/filter/UNION). Use the PHYSICAL table names (ds_<id>) and column names from studio_dataset_schema, not display names. Text power available: REGEXP, regexp_extract(value,pattern[,group]), regexp_replace(value,pattern,repl).",
     {"type": "object", "properties": {"sql": {"type": "string"}, "maxRows": {"type": "integer"}}, "required": ["sql"]}, t_query_data),
    ("studio_query_to_dataset", "Run a read-only SELECT/WITH and SAVE the result as a NEW dataset (columns = the query's output columns, types inferred). The one-shot way to extract/clean/reshape across messy or multiple tables into a fresh tidy dataset — e.g. SELECT regexp_extract(notes,'https?://\\S+') AS url FROM ds_a WHERE notes REGEXP 'http' UNION ... Use studio_dataset_schema for physical table/column names.",
     {"type": "object", "properties": {"sql": {"type": "string"}, "name": {"type": "string"},
                                       "dedupKeys": {"type": "array"}, "maxRows": {"type": "integer"}}, "required": ["sql", "name"]}, t_query_to_dataset),
    ("studio_exec_sql", "Run a single INSERT/UPDATE/DELETE against the dataset tables to clean/transform/move data IN PLACE, with full WHERE/JOIN/expressions and the regexp_* helpers (e.g. UPDATE ds_x SET domain=regexp_extract(url,'https?://([^/]+)',1); DELETE FROM ds_x WHERE url NOT REGEXP 'linkedin'). Use physical names from studio_dataset_schema. The registry tables are off-limits; schema changes go through the add/drop/rename_column tools. Returns rows affected.",
     {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}, t_exec_sql),
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
    ("studio_create_workflow", "Create — or update an existing user/agent workflow (pass its id) — a reusable workflow from Python code. The code must define main(argv) and should use `from automations import userkit` (userkit.parse(argv) -> params,server,output; userkit.input_rows(argv) for list-consuming workflows; userkit.run_session(fn,params,server); userkit.write_csv(output,rows,columns); userkit.progress/log/error). params: [{name,label,type:string|number|boolean|select,default,help,options}]. outputContract/inputContract: [{name,type}] (set inputContract to make it list-consuming/chainable). profile: 'ephemeral'|'shared'. Built-ins are READ-ONLY: passing a built-in's id forks an editable copy instead of overwriting (the result carries a `warning` + `copiedFromBuiltin`), exactly like the human editor.",
     {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"},
                                       "description": {"type": "string"}, "code": {"type": "string"},
                                       "params": {"type": "array"}, "outputContract": {"type": "array"},
                                       "inputContract": {"type": "array"},
                                       "profile": {"type": "string"}, "needsAuth": {"type": "boolean"},
                                       "icon": {"type": "string"}}, "required": ["name", "code"]}, t_create_workflow),
    ("studio_workflow_source", "Read a workflow's Python source — works for user/agent AND built-in workflows. Use studio_get_workflow for source + settings together.",
     {"type": "object", "properties": {"workflowId": {"type": "string"}}, "required": ["workflowId"]}, t_workflow_source),
    ("studio_run_workflow", "Start a workflow run. Defaults to this agent's own profile. Bind datasetId to auto-append the result on success. For list-consuming workflows (those with an input contract, e.g. url-titles), set inputDatasetId to feed a dataset of rows as input. Returns runId.",
     {"type": "object", "properties": {"workflowId": {"type": "string"}, "params": {"type": "object"},
                                       "profileId": {"type": "string"}, "datasetId": {"type": "string"},
                                       "inputDatasetId": {"type": "string"}, "watch": {"type": "boolean"}},
      "required": ["workflowId"]}, t_run_workflow),
    ("studio_run_status", "Get a run's current status, progress, row count and error.",
     {"type": "object", "properties": {"runId": {"type": "string"}}, "required": ["runId"]}, t_run_status),
    ("studio_wait_run", "Block until a run reaches a terminal state (succeeded/failed/canceled) or the timeout, then return its status. Use this instead of polling.",
     {"type": "object", "properties": {"runId": {"type": "string"}, "timeoutSec": {"type": "integer"}}, "required": ["runId"]}, t_wait_run),
    ("studio_list_runs", "List recent runs (most recent first) with status, row count and error — to see history or find a runId.",
     {"type": "object", "properties": {"limit": {"type": "integer"}}}, t_list_runs),
    ("studio_run_logs", "Read a run's log lines (the workflow's progress/log/error output) — use to diagnose a failed or stuck run.",
     {"type": "object", "properties": {"runId": {"type": "string"}, "tail": {"type": "integer"}}, "required": ["runId"]}, t_run_logs),
    ("studio_cancel_run", "Cancel a running/queued/scheduled run.",
     {"type": "object", "properties": {"runId": {"type": "string"}}, "required": ["runId"]}, t_cancel_run),
    ("studio_claim_run", "Adopt a run's completion so YOU are notified/woken when it finishes (treats it like one of your own runs — you'll rest as `waiting` until it's done). Use this when a workflow you didn't launch is holding your profile/browser: claim it, then end your turn and you'll be re-activated when it completes. Refused if another agent already owns it.",
     {"type": "object", "properties": {"runId": {"type": "string"}}, "required": ["runId"]}, t_claim_run),
    ("studio_run_result", "Read a finished run's result rows directly (the output CSV parsed to rows) without going through a dataset.",
     {"type": "object", "properties": {"runId": {"type": "string"}}, "required": ["runId"]}, t_run_result),
    ("studio_run_to_dataset", "Append a finished run's result into a dataset (new if no datasetId, else existing). The canonical way to capture a run's output into the persistent data layer.",
     {"type": "object", "properties": {"runId": {"type": "string"}, "datasetId": {"type": "string"},
                                       "name": {"type": "string"}, "dedupKeys": {"type": "array"}}, "required": ["runId"]}, t_run_to_dataset),
    ("studio_schedule_workflow", "Schedule a workflow to run later (not now): set `inSeconds` (relative) or `at` (unix epoch seconds), and optionally `everySeconds` to repeat. It appears as a `scheduled` run, fires into the queue when due, then takes the profile lock normally. Same params as studio_run_workflow (params/profileId/inputDatasetId/datasetId). You'll be notified when each occurrence finishes, like any run you launch.",
     {"type": "object", "properties": {"workflowId": {"type": "string"}, "params": {"type": "object"},
                                       "profileId": {"type": "string"}, "inputDatasetId": {"type": "string"},
                                       "datasetId": {"type": "string"}, "inSeconds": {"type": "number"},
                                       "at": {"type": "number"}, "everySeconds": {"type": "number"}}, "required": ["workflowId"]}, t_schedule_workflow),
    ("studio_schedule_wake", "Schedule YOURSELF to be woken later with a prompt (set `inSeconds` or `at`). End your turn after calling this; you'll rest as `scheduled` (releasing the profile so others can use it) and be re-activated at that time with the prompt. Use this to wait on something external, pace work, or come back to a task later.",
     {"type": "object", "properties": {"inSeconds": {"type": "number"}, "at": {"type": "number"}, "prompt": {"type": "string"}}, "required": ["prompt"]}, t_schedule_wake),
    ("studio_cancel_schedule", "Cancel a scheduled item: pass `runId` to cancel a scheduled workflow run, or no args to cancel your own pending scheduled wake.",
     {"type": "object", "properties": {"runId": {"type": "string"}}}, t_cancel_schedule),
    ("studio_dataset_rename", "Rename a dataset.",
     {"type": "object", "properties": {"datasetId": {"type": "string"}, "name": {"type": "string"}}, "required": ["datasetId", "name"]}, t_dataset_rename),
    ("studio_dataset_delete", "Delete a dataset permanently.",
     {"type": "object", "properties": {"datasetId": {"type": "string"}}, "required": ["datasetId"]}, t_dataset_delete),
    ("studio_dataset_merge", "Merge several datasets (by id) into one new dataset, union of columns, optional dedup keys.",
     {"type": "object", "properties": {"ids": {"type": "array"}, "name": {"type": "string"}, "dedupKeys": {"type": "array"}}, "required": ["ids"]}, t_dataset_merge),
    ("studio_dataset_import", "Create or append a dataset from raw CSV text (header row + rows). Use to bring external data into the data layer.",
     {"type": "object", "properties": {"csv": {"type": "string"}, "name": {"type": "string"},
                                       "datasetId": {"type": "string"}, "dedupKeys": {"type": "array"}}, "required": ["csv"]}, t_dataset_import),
    ("studio_dataset_update_cell", "Set one cell's value. rowId is the row's `_rid` field from studio_dataset_rows; column is the display name.",
     {"type": "object", "properties": {"datasetId": {"type": "string"}, "rowId": {"type": "integer"},
                                       "column": {"type": "string"}, "value": {}}, "required": ["datasetId", "rowId", "column"]}, t_dataset_update_cell),
    ("studio_dataset_delete_rows", "Delete rows by their `_rid` values (from studio_dataset_rows).",
     {"type": "object", "properties": {"datasetId": {"type": "string"}, "rowIds": {"type": "array"}}, "required": ["datasetId", "rowIds"]}, t_dataset_delete_rows),
    ("studio_dataset_add_column", "Add a column (type text|number|boolean).",
     {"type": "object", "properties": {"datasetId": {"type": "string"}, "name": {"type": "string"}, "type": {"type": "string"}}, "required": ["datasetId", "name"]}, t_dataset_add_column),
    ("studio_dataset_drop_column", "Remove a column by display name.",
     {"type": "object", "properties": {"datasetId": {"type": "string"}, "name": {"type": "string"}}, "required": ["datasetId", "name"]}, t_dataset_drop_column),
    ("studio_dataset_rename_column", "Rename a column (from display name -> to display name).",
     {"type": "object", "properties": {"datasetId": {"type": "string"}, "from": {"type": "string"}, "to": {"type": "string"}}, "required": ["datasetId", "from", "to"]}, t_dataset_rename_column),
    ("studio_dataset_set_dedup_keys", "Set the dataset's dedup key columns (display names).",
     {"type": "object", "properties": {"datasetId": {"type": "string"}, "keys": {"type": "array"}}, "required": ["datasetId", "keys"]}, t_dataset_set_dedup_keys),
    ("studio_dataset_dedup", "Deduplicate a dataset now, by its dedup keys (or the keys you pass).",
     {"type": "object", "properties": {"datasetId": {"type": "string"}, "keys": {"type": "array"}}, "required": ["datasetId"]}, t_dataset_dedup),
    ("studio_list_profiles", "List browser profiles (id, name, whether a login window is open). Use to pick a profileId for runs or to check which accounts are set up.",
     {"type": "object", "properties": {}}, t_list_profiles),
    ("studio_create_profile", "Create a new browser profile (a fresh, isolated logged-out browser identity).",
     {"type": "object", "properties": {"name": {"type": "string"}}}, t_create_profile),
    ("studio_delete_workflow", "Delete a user/agent-created workflow (built-ins can't be deleted).",
     {"type": "object", "properties": {"workflowId": {"type": "string"}}, "required": ["workflowId"]}, t_delete_workflow),
]

BROWSER_TOOLS = [
    ("browser_goto", "Navigate the owned browser to a URL.",
     {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, t_browser_goto),
    ("browser_observe", "Observe the current page: an indexed snapshot of interactive elements + text, descending into shadow DOM and same-origin iframes. Each element shows its [index] (use with browser_click/browser_type), with '@shadow'/'@iframe' marking elements only reachable via observe+click (NOT browser_eval) and '(offscreen)' marking out-of-viewport ones. Pass format='full' to also get every element's xpath, center [x,y], frame and inViewport — use that (or browser_inspect) to tell look-alike controls apart (e.g. an in-card button under <main> vs a duplicate sticky-header one). Pass dedup=true to collapse runs of identical look-alike elements (e.g. a card's repeated photo links) into one line with a count — great for cutting noise on listing/grid pages (then use browser_extract for the structured data). maxNodes caps the snapshot (default 1200).",
     {"type": "object", "properties": {"format": {"type": "string", "enum": ["outline", "full"]}, "dedup": {"type": "boolean"}, "maxNodes": {"type": "integer"}}}, t_browser_observe),
    ("browser_extract", "Bulk-extract structured rows from REPEATED page content in one call (light DOM, deterministic — no parsing round-trips). `container` is a CSS selector for the repeating item (e.g. a listing card); omit it to auto-detect the dominant repeated block (explicit is more reliable). `fields` maps output keys to per-item specs: 'text' or '' = the element's text; 'css-selector' = that child's text; 'css-selector@attr' or '@attr' = an attribute (e.g. {title:'h3', price:'.price', url:'a@href', img:'img@src'}). Returns {container, count, rows}. Use this instead of observe+manual-parse whenever you're scraping many similar items; for precise single controls / shadow-DOM, use browser_observe + browser_inspect.",
     {"type": "object", "properties": {"container": {"type": "string"}, "fields": {"type": "object"}, "limit": {"type": "integer"}}}, t_browser_extract),
    ("browser_inspect", "Zoom into elements whose accessible name contains `match` (and/or a `tag`, and/or `frame`=main|shadow|iframe) and return full metadata for each: index, tag, name, attrs, inViewport, center [x,y], xpath, frame. The way to disambiguate duplicate/look-alike controls and pick the right [index] before clicking (e.g. choose the Connect whose xpath contains '/main', not the sticky-header twin).",
     {"type": "object", "properties": {"match": {"type": "string"}, "tag": {"type": "string"},
                                       "frame": {"type": "string", "enum": ["main", "shadow", "iframe"]},
                                       "maxNodes": {"type": "integer"}}}, t_browser_inspect),
    ("browser_click", "Click the element at the given [index] from the most recent browser_observe/browser_inspect/browser_wait.",
     {"type": "object", "properties": {"index": {"type": "integer"}}, "required": ["index"]}, t_browser_click),
    ("browser_type", "Type into the element at [index]. Set enter=true to submit, clear=true to clear first.",
     {"type": "object", "properties": {"index": {"type": "integer"}, "text": {"type": "string"},
                                       "enter": {"type": "boolean"}, "clear": {"type": "boolean"}}, "required": ["index", "text"]}, t_browser_type),
    ("browser_press", "Press a key (e.g. Enter, Escape, ArrowDown).",
     {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}, t_browser_press),
    ("browser_scroll", "Scroll the page. Either by dy pixels (default 600) or to='top'|'bottom'. Scrolling to top is useful to dissolve sticky headers before acting.",
     {"type": "object", "properties": {"dy": {"type": "integer"}, "to": {"type": "string", "enum": ["top", "bottom"]}}}, t_browser_scroll),
    ("browser_wait", "Poll until an element appears (or timeout): `match` = accessible-name substring (shadow/iframe-aware, via observe), or `selector` = a main-frame CSS selector. Returns the matched element with its current [index]. Use to wait for late/dynamic content like a dialog that renders in shadow DOM.",
     {"type": "object", "properties": {"match": {"type": "string"}, "selector": {"type": "string"}, "timeoutSec": {"type": "number"}}}, t_browser_wait),
    ("browser_read_text", "Get the full visible text of the current page.",
     {"type": "object", "properties": {}}, t_browser_read_text),
    ("browser_eval", "Evaluate a JavaScript function in the page (main frame, light DOM) and return the result — full flexibility for custom extraction or clicks. NOTE: main-frame JS cannot see shadow-DOM or cross-origin iframe content; for those use browser_observe/browser_inspect + browser_click by [index].",
     {"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]}, t_browser_eval),
    ("browser_screenshot", "Take a screenshot of the current page — returned INLINE as an image you can see directly (plus the file path). Use it to visually verify a page or read something the DOM snapshot doesn't expose.",
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
_SCHEMAS = {n: s for (n, _d, s, _f) in (STUDIO_TOOLS + BROWSER_TOOLS)}


def _coerce_args(name: str, args: dict) -> dict:
    """Be forgiving about how the engine serialized arguments. Claude/Codex
    sometimes send an array/object parameter as a JSON *string* (e.g. columns as
    '[{"name":"title","type":"text"}]'). For any property the tool's schema
    declares as array/object, parse a JSON-looking string into the real value —
    so a tool never receives a string where it expects a list (which silently
    corrupted datasets char-by-char before)."""
    props = (_SCHEMAS.get(name) or {}).get("properties") or {}
    for k, v in list(args.items()):
        if isinstance(v, str) and (props.get(k) or {}).get("type") in ("array", "object"):
            s = v.strip()
            if s[:1] in ("[", "{"):
                try:
                    args[k] = json.loads(s)
                except (ValueError, TypeError):
                    pass
    return args


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
            out = fn(_coerce_args(name, args))
            # inline image result (e.g. browser_screenshot): an image block the model
            # can see directly + a text path fallback. Works for Claude and Codex.
            if isinstance(out, dict) and out.get("image_b64"):
                _result(rid, {"content": [
                    {"type": "image", "data": out["image_b64"], "mimeType": out.get("mimeType", "image/png")},
                    {"type": "text", "text": json.dumps({"path": out.get("path")})}], "isError": False})
                return
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
