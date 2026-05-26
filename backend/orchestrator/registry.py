"""Workflow registry. Adding an automation = add an entry here whose Python
module follows the run-event protocol (automations/_events.py) and accepts
``--server URL -o CSV``. Everything else (UI, orchestration) is workflow-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


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
]


def get_workflow(wid: str) -> WorkflowDef | None:
    return next((w for w in WORKFLOWS if w.id == wid), None)


def public_workflow(w: WorkflowDef) -> dict:
    """Serialisable view for the API (drops the build_argv callable)."""
    return {
        "id": w.id, "name": w.name, "description": w.description, "icon": w.icon,
        "module": w.module, "profile": w.profile, "profileName": w.profile_name,
        "needsAuth": w.needs_auth,
        "params": [vars(p) for p in w.params],
        "outputContract": w.output_contract,
    }
