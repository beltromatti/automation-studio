"""Workflow registry. Adding an automation = add an entry here whose Python
module follows the run-event protocol (automations/_events.py) and accepts
``--server URL -o CSV``. Everything else (UI, orchestration) is workflow-agnostic.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from humanbrowser.config import data_dir

# User/agent-authored workflows live under the (writable) data dir — NEVER in the
# read-only app bundle — so they work identically in dev and packaged builds.
USER_DIR = data_dir() / "workflows"
USER_INDEX = USER_DIR / "workflows.json"


@dataclass
class WorkflowParam:
    name: str
    label: str
    type: str  # "string" | "number" | "boolean" | "select"
    required: bool = False
    default: Any = None
    placeholder: str = ""
    help: str = ""
    options: list[dict] | None = None  # for "select": [{"value","label"}, ...]


@dataclass
class WorkflowDef:
    id: str
    name: str
    description: str
    icon: str
    module: str  # python module, e.g. "automations.google_search"
    profile: str  # "shared" | "ephemeral"
    build_argv: Callable[[dict], list[str]]
    profile_name: str | None = None
    needs_auth: bool = False
    params: list[WorkflowParam] = field(default_factory=list)
    # The columns this workflow's CSV result carries (name + logical type:
    # text|number|boolean). Lets a Dataset adopt/validate the shape when a run is
    # captured into the data layer (Phase 2). Empty = inferred from the CSV header.
    output_contract: list[dict] = field(default_factory=list)
    # When non-empty, this workflow CONSUMES an input dataset (a list of rows with
    # these columns) — the orchestrator dumps the chosen dataset to input.json and
    # passes --input-json. This is what lets workflows chain (A's output → B's input).
    input_contract: list[dict] = field(default_factory=list)
    # Origin: built-ins ship in the bundle (module = dotted import path); user/agent
    # workflows are files under the data dir (path set), differing only by a chip.
    builtin: bool = True
    path: str | None = None          # source .py file for user/agent workflows
    created_by: str = "builtin"      # "builtin" | "user" | "agent"

    @property
    def target(self) -> str:
        """What `run-workflow` should load: a file path (user) or dotted module."""
        return self.path or self.module


def _linkedin_argv(p: dict) -> list[str]:
    """Map the LinkedIn People params to the workflow's CLI arguments."""
    argv: list[str] = []
    q = str(p.get("query", "") or "").strip()
    if q:
        argv.append(q)  # positional fuzzy keywords

    def add(flag: str, key: str) -> None:
        v = str(p.get(key, "") or "").strip()
        if v:
            argv.extend([flag, v])

    add("--current-title", "currentTitle")
    add("--first-name", "firstName")
    add("--last-name", "lastName")
    add("--current-company", "currentCompany")
    add("--school", "school")
    add("--location", "locations")
    add("--industries", "industries")
    add("--connections", "connections")
    add("--profile-languages", "profileLanguages")
    mode = str(p.get("mode", "full") or "full").strip().lower()
    argv.extend(["--mode", "short" if mode == "short" else "full"])
    argv.extend(["-n", str(int(p.get("limit", 25) or 25))])
    return argv


WORKFLOWS: list[WorkflowDef] = [
    WorkflowDef(
        id="google-search",
        name="Google Search",
        description="Autonomous, human-grade Google search. Handles the consent dialog, "
        "types like a person, paginates, and returns ranked organic results.",
        icon="search",
        module="automations.google_search",
        profile="ephemeral",
        needs_auth=False,
        params=[
            WorkflowParam("query", "Search query", "string", required=True,
                          placeholder="best espresso machine under 500"),
            WorkflowParam("numResults", "Results", "number", default=10,
                          help="How many organic results to collect (paginates as needed)."),
        ],
        build_argv=lambda p: [str(p.get("query", "")), "-n", str(int(p.get("numResults", 10) or 10))],
        output_contract=[
            {"name": "rank", "type": "number"}, {"name": "title", "type": "text"},
            {"name": "url", "type": "text"}, {"name": "host", "type": "text"},
            {"name": "snippet", "type": "text"},
        ],
    ),
    WorkflowDef(
        id="linkedin-people",
        name="LinkedIn People",
        description="Human-grade LinkedIn people search with the same targeting as LinkedIn's "
        "own filters. In Full mode it opens each profile to enrich the row (about, current "
        "company, education, connections, followers); Short mode reads result cards only. "
        "Uses your logged-in profile.",
        icon="users",
        module="automations.linkedin_people",
        profile="shared",
        profile_name="default",
        needs_auth=True,
        params=[
            WorkflowParam("mode", "Mode", "select", default="full",
                          options=[{"value": "full", "label": "Full — open each profile & enrich"},
                                   {"value": "short", "label": "Short — result cards only"}],
                          help="Full opens every profile (one page each, human-paced) to add about, "
                               "company, education, connections & followers. Short is faster and lighter."),
            WorkflowParam("query", "Keywords", "string",
                          placeholder="data engineer",
                          help="General fuzzy search. Combine with the filters below for sharper targeting."),
            WorkflowParam("currentTitle", "Current job title", "string", placeholder="Data Engineer"),
            WorkflowParam("locations", "Locations", "string", placeholder="Milan, London",
                          help="Comma-separated. Resolved through LinkedIn's own location autocomplete."),
            WorkflowParam("currentCompany", "Current company", "string", placeholder="Google"),
            WorkflowParam("industries", "Industries", "string", placeholder="Financial Services",
                          help="Comma-separated. Resolved through LinkedIn's industry autocomplete."),
            WorkflowParam("school", "School", "string", placeholder="Politecnico di Milano"),
            WorkflowParam("connections", "Connection degree", "string", placeholder="2nd,3rd",
                          help="Any of 1st, 2nd, 3rd (comma-separated). Blank = all."),
            WorkflowParam("firstName", "First name", "string"),
            WorkflowParam("lastName", "Last name", "string"),
            WorkflowParam("profileLanguages", "Profile language", "string", placeholder="en, it",
                          help="Comma-separated language codes."),
            WorkflowParam("limit", "Profiles", "number", default=25,
                          help="Target number of profiles (paginates as needed)."),
        ],
        build_argv=_linkedin_argv,
        output_contract=[
            {"name": "rank", "type": "number"}, {"name": "name", "type": "text"},
            {"name": "profile_url", "type": "text"}, {"name": "degree", "type": "text"},
            {"name": "headline", "type": "text"}, {"name": "location", "type": "text"},
            {"name": "connections", "type": "text"}, {"name": "followers", "type": "text"},
            {"name": "current_company", "type": "text"}, {"name": "education", "type": "text"},
            {"name": "about", "type": "text"}, {"name": "open_to_work", "type": "text"},
            {"name": "verified", "type": "text"}, {"name": "premium", "type": "text"},
            {"name": "contact_info", "type": "text"}, {"name": "services", "type": "text"},
            {"name": "extra", "type": "text"},
        ],
    ),
    # Demonstrates dataset-as-input: consumes a list of URLs and fetches each page's
    # title. Same shape as a "connect each profile" / "message each lead" workflow,
    # the multi-workflow pipeline the agent layer chains together.
    WorkflowDef(
        id="url-titles",
        name="Page Titles (from a list)",
        description="Takes a dataset of URLs and visits each one to fetch its page title. "
        "A list-consuming workflow — the second half of a pipeline (feed it the output of another).",
        icon="globe",
        module="automations.url_titles",
        profile="ephemeral",
        needs_auth=False,
        params=[],
        build_argv=lambda p: ["--params-json", json.dumps(p)],
        input_contract=[{"name": "url", "type": "text"}],
        output_contract=[{"name": "url", "type": "text"}, {"name": "title", "type": "text"},
                         {"name": "ok", "type": "boolean"}],
    ),
]


# ---------------------------------------------------------------- user/agent workflows
def _user_argv(p: dict) -> list[str]:
    """Generic calling convention for user/agent workflows: params as one JSON arg
    (their main() reads it via automations.userkit.parse)."""
    return ["--params-json", json.dumps(p)]


def _def_from_meta(m: dict) -> WorkflowDef:
    return WorkflowDef(
        id=m["id"], name=m.get("name", m["id"]), description=m.get("description", ""),
        icon=m.get("icon", "wand"), module=m.get("id"), profile=m.get("profile", "ephemeral"),
        profile_name=m.get("profileName"), needs_auth=bool(m.get("needsAuth")),
        params=[WorkflowParam(**{k: pp.get(k) for k in WorkflowParam.__dataclass_fields__ if k in pp})
                for pp in m.get("params", [])],
        output_contract=m.get("outputContract", []), build_argv=_user_argv,
        input_contract=m.get("inputContract", []),
        builtin=False, created_by=m.get("createdBy", "user"),
        path=str(USER_DIR / m["file"]),
    )


def _load_index() -> list[dict]:
    try:
        return json.loads(USER_INDEX.read_text()) if USER_INDEX.exists() else []
    except Exception:
        return []


def load_user_workflows() -> list[WorkflowDef]:
    out = []
    for m in _load_index():
        try:
            if (USER_DIR / m["file"]).exists():
                out.append(_def_from_meta(m))
        except Exception:
            continue
    return out


def all_workflows() -> list[WorkflowDef]:
    """Built-ins + user/agent workflows (read fresh so new ones appear without a
    backend restart — the registry is consulted per request)."""
    return WORKFLOWS + load_user_workflows()


def get_workflow(wid: str) -> WorkflowDef | None:
    return next((w for w in all_workflows() if w.id == wid), None)


_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(s: str) -> str:
    return _SLUG.sub("-", (s or "").lower()).strip("-") or "workflow"


def save_user_workflow(body: dict) -> dict:
    """Create or update a user/agent workflow: validate the code compiles and has
    main(), write the .py + index entry. Returns the public workflow."""
    code = body.get("code", "")
    if "def main(" not in code:
        raise ValueError("workflow code must define a main(argv) function")
    try:
        compile(code, "<workflow>", "exec")
    except SyntaxError as e:
        raise ValueError(f"syntax error: {e}")
    USER_DIR.mkdir(parents=True, exist_ok=True)
    index = _load_index()
    wid = body.get("id") or _slug(body.get("name", "workflow"))
    # don't collide with a built-in id
    if any(w.id == wid for w in WORKFLOWS):
        wid = wid + "-user"
    existing = next((m for m in index if m["id"] == wid), None)
    file = (existing or {}).get("file") or f"{wid}.py"
    (USER_DIR / file).write_text(code)
    meta = {
        "id": wid, "name": body.get("name", wid), "description": body.get("description", ""),
        "icon": body.get("icon", "wand"), "profile": body.get("profile", "ephemeral"),
        "profileName": body.get("profileName"), "needsAuth": bool(body.get("needsAuth")),
        "params": body.get("params", []), "outputContract": body.get("outputContract", []),
        "inputContract": body.get("inputContract", []),
        "file": file, "createdBy": body.get("createdBy", "user"),
        "createdAt": (existing or {}).get("createdAt") or time.time(), "updatedAt": time.time(),
    }
    index = [m for m in index if m["id"] != wid] + [meta]
    USER_INDEX.write_text(json.dumps(index, indent=2))
    return public_workflow(_def_from_meta(meta))


def delete_user_workflow(wid: str) -> bool:
    index = _load_index()
    m = next((x for x in index if x["id"] == wid), None)
    if not m:
        return False
    try:
        (USER_DIR / m["file"]).unlink(missing_ok=True)
    except Exception:
        pass
    USER_INDEX.write_text(json.dumps([x for x in index if x["id"] != wid], indent=2))
    return True


def user_workflow_source(wid: str) -> str | None:
    m = next((x for x in _load_index() if x["id"] == wid), None)
    if not m:
        return None
    p = USER_DIR / m["file"]
    return p.read_text() if p.exists() else None


def workflow_source(wid: str) -> str | None:
    """Source code of a workflow: the .py for user/agent workflows; best-effort the
    module source for built-ins (available in dev; not in a frozen build)."""
    src = user_workflow_source(wid)
    if src is not None:
        return src
    w = next((x for x in WORKFLOWS if x.id == wid), None)
    if not w:
        return None
    try:
        import importlib
        import inspect
        return inspect.getsource(importlib.import_module(w.module))
    except Exception:
        return None  # frozen build: built-in source isn't shipped


def public_workflow(w: WorkflowDef) -> dict:
    """Serialisable view for the API (drops the build_argv callable)."""
    return {
        "id": w.id, "name": w.name, "description": w.description, "icon": w.icon,
        "module": w.module, "profile": w.profile, "profileName": w.profile_name,
        "needsAuth": w.needs_auth,
        "params": [vars(p) for p in w.params],
        "outputContract": w.output_contract,
        "inputContract": w.input_contract,
        "builtin": w.builtin, "createdBy": w.created_by,
    }
