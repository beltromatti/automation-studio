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
    type: str  # "string" | "number" | "boolean"
    required: bool = False
    default: Any = None
    placeholder: str = ""
    help: str = ""


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
    ),
    WorkflowDef(
        id="linkedin-people",
        name="LinkedIn People",
        description="Scrapes LinkedIn people-search result cards (name, profile URL, headline, "
        "location, degree) without opening profiles. Uses your logged-in profile.",
        icon="users",
        module="automations.linkedin_people",
        profile="shared",
        profile_name="default",
        needs_auth=True,
        params=[
            WorkflowParam("query", "Search query", "string", required=True,
                          placeholder="software engineer milano"),
            WorkflowParam("limit", "Profiles", "number", default=50,
                          help="Target number of profiles (paginates as needed)."),
        ],
        build_argv=lambda p: [str(p.get("query", "")), "-n", str(int(p.get("limit", 50) or 50))],
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
    }
