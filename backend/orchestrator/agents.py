"""AgentManager — AI agents as a first-class, superior-to-workflows layer.

An **agent definition** is a reusable template: an engine (claude | codex), a
system prompt ("skills"), and capability scopes (studio always; browser when it
owns a profile). **Launching** an agent = pick a definition + a profile + a
prompt; the backend spawns the user's locally-installed CLI in headless-JSON mode
(inheriting their subscription — no API keys), attaches our MCP tool server, and
streams the engine's events normalised into one taxonomy the UI renders live.

Both engines are driven symmetrically as local subprocesses:
  codex exec --json ...      (Codex, ChatGPT subscription via ~/.codex/auth.json)
  claude -p --output-format stream-json ...   (Claude Code, Pro/Max login)

Profile ownership: a browser-scope agent on a persistent profile opens a
control-server (via the RunManager, which serialises that profile) and the agent
drives it through browser_* tools; workflows it launches on that same profile
ATTACH to this control-server (shared browser) instead of spawning their own.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from humanbrowser.config import data_dir
from . import engines
from .manager import get_manager, _self_base, kill_tree, is_ephemeral

DATA = data_dir()
AGENTS_FILE = DATA / "agents.json"
SESSIONS_FILE = DATA / "agent_sessions.json"
SESSIONS_DIR = DATA / "agent_runs"
BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
MAX_EVENTS = 4000
TERMINAL = {"done", "failed", "stopped"}  # "canceled" is the legacy name (loaded → "stopped")
MAX_NATIVE_THREAD_TURNS = 24
MAX_NATIVE_THREAD_INPUT_TOKENS = 10_000_000

ENGINES = {"codex", "claude"}

IS_WIN = os.name == "nt" or sys.platform.startswith("win")
# Windows caps a whole command line at 32,767 characters; stay well under it.
WIN_CMDLINE_BUDGET = 24_000

# Models and reasoning efforts are NEVER hardcoded here: they are fetched from the
# installed CLIs themselves (orchestrator.engines) and chosen per session, exactly
# like picking a model in Codex or Claude Code directly.


def _find_binary(engine: str) -> str | None:
    """Resolve the user's installed engine CLI, cross-platform (delegates to the
    central, fault-tolerant dependency gateway). Finder-launched apps don't inherit
    the shell PATH, so it probes the standard install locations too."""
    from . import deps
    return deps.find_engine(engine)


@dataclass
class AgentDef:
    # An agent is engine-agnostic: the engine (codex|claude) is chosen per session
    # at launch, not baked into the definition.
    id: str
    name: str
    icon: str = "sparkles"
    description: str = ""             # optional, human-facing (shown on the card)
    systemPrompt: str = ""            # the agent's skills / role (injected)
    scopes: list[str] = field(default_factory=lambda: ["studio"])  # + "browser"
    createdAt: float = 0.0
    builtin: bool = False


@dataclass
class AgentSession:
    id: str
    agentId: str
    agentName: str
    engine: str
    scopes: list[str]
    profileId: str
    profileName: str
    prompt: str
    status: str                       # queued|starting|running|waiting|scheduled|done|failed|stopped
    createdAt: float
    watch: bool = False
    startedAt: float | None = None
    finishedAt: float | None = None
    error: str | None = None
    controlPort: int | None = None    # owned browser control-server (browser scope)
    threadId: str | None = None       # codex thread / claude session id (for steering)
    threadStartedTurn: int = 0        # local turn index when the native thread was created
    usage: dict | None = None
    turns: int = 0
    runIds: list[str] = field(default_factory=list)
    pendingSteers: list[str] = field(default_factory=list)
    # canonical inbox: notifications wake the agent (e.g. a detached workflow it
    # launched finished). Each: {id, kind, payload, createdAt, delivered}.
    notifications: list[dict] = field(default_factory=list)
    # runs whose terminal state the agent has ALREADY learned inline (via studio_wait_run /
    # run_status terminal / run_result / run_to_dataset). Suppresses duplicate
    # workflow_finished notifications so we never tell the agent something it just learned.
    ackedRuns: list[str] = field(default_factory=list)
    scheduledAt: float | None = None  # when status == "scheduled": the future wake time
    scheduledPrompt: str | None = None  # the prompt to wake with at scheduledAt
    # Engine model + reasoning effort for the NEXT turn. Both are ids the installed
    # CLI advertises (see orchestrator.engines) — never a hardcoded name — and the
    # user can change them mid-conversation exactly like /model in Codex or Claude
    # Code: the native thread is kept, the new setting applies from the next turn.
    model: str = ""
    effort: str = ""
    # Text the engine had STREAMED but never committed to its own native session
    # before it was killed (a stop, or a preempt to deliver a notification). The
    # engine's thread has no memory of it; ours does, so the next turn replays it
    # as context and the conversation stays coherent instead of restarting blind.
    interruptedTail: str = ""


# ------------------------------------------------------------------ normalisation
# Both engines are reduced to ONE event taxonomy the UI renders:
#   system | status | message | reasoning | tool_call | tool_result | usage | error
# Internal, never persisted: _text_delta / _reasoning_delta (per-token streaming)
# and _msg_start (ties a stream of deltas to the assistant message it belongs to).
#
# Tool calls carry a `callId` and results echo it, so a result is always matched
# to the RIGHT call — Claude and Codex both issue tool calls in parallel, and a
# positional (FIFO) pairing mis-attributes results as soon as they interleave.


def _codex_tool_event(t: str, it: dict) -> list[dict]:
    """One Codex item.* event for a tool-ish item → tool_call / tool_result."""
    itype = it.get("type")
    cid = it.get("id")
    started, done = t == "item.started", t == "item.completed"
    if itype == "mcp_tool_call":
        if started:
            return [{"kind": "tool_call", "tool": it.get("tool"), "args": it.get("arguments"),
                     "server": it.get("server"), "callId": cid}]
        if done:
            return [{"kind": "tool_result", "tool": it.get("tool"), "callId": cid,
                     "ok": it.get("status") != "failed",
                     "result": _short(it.get("result") or (it.get("error") or {}).get("message"))}]
    elif itype == "command_execution":
        if started:
            return [{"kind": "tool_call", "tool": "shell", "callId": cid,
                     "args": {"command": it.get("command")}}]
        if done:
            return [{"kind": "tool_result", "tool": "shell", "callId": cid,
                     "ok": it.get("exit_code") == 0,
                     "result": _short(it.get("aggregated_output"))}]
    elif itype == "file_change":
        # Codex edits files through its own apply-patch item; surface it like any
        # other tool so the transcript shows what it touched.
        changes = it.get("changes") or []
        paths = [c.get("path") for c in changes if isinstance(c, dict) and c.get("path")]
        if started:
            return [{"kind": "tool_call", "tool": "edit_files", "callId": cid,
                     "args": {"paths": paths or None, "changes": len(changes) or None}}]
        if done:
            return [{"kind": "tool_result", "tool": "edit_files", "callId": cid,
                     "ok": (it.get("status") or "completed") not in ("failed", "error"),
                     "result": _short(paths or it.get("status") or "applied")}]
    elif itype == "web_search":
        if started:
            return [{"kind": "tool_call", "tool": "web_search", "callId": cid,
                     "args": {"query": it.get("query")}}]
        if done:
            return [{"kind": "tool_result", "tool": "web_search", "callId": cid, "ok": True,
                     "result": _short(it.get("query"))}]
    return []


def _norm_codex(obj: dict) -> list[dict]:
    """Codex `exec --json` JSONL → normalised events.

    Codex does not stream partial text: an `agent_message` / `reasoning` item only
    materialises at item.completed (its per-token deltas exist only on the
    experimental app-server protocol). Everything else — tool calls, shell, file
    edits, plans, web search — streams as started/completed pairs.
    """
    t = obj.get("type", "")
    out: list[dict] = []
    if t == "thread.started":
        out.append({"kind": "system", "text": "session started", "threadId": obj.get("thread_id")})
    elif t == "turn.started":
        out.append({"kind": "status", "status": "thinking"})
    elif t == "turn.completed":
        out.append({"kind": "usage", "usage": obj.get("usage")})
    elif t == "turn.failed":
        out.append({"kind": "error", "text": (obj.get("error") or {}).get("message", "turn failed")})
    elif t in ("item.started", "item.updated", "item.completed"):
        it = obj.get("item") or {}
        itype = it.get("type")
        done = t == "item.completed"
        if itype == "agent_message":
            if done and (it.get("text") or "").strip():
                out.append({"kind": "message", "text": it.get("text", "")})
        elif itype == "reasoning":
            if done and (it.get("text") or "").strip():
                out.append({"kind": "reasoning", "text": it.get("text", "")})
        elif itype in ("todo_list", "plan_update"):
            # live plan: Codex re-emits the whole list on every update, so give it a
            # stable id and let the UI replace the row in place instead of stacking.
            items = it.get("items") or []
            if items:
                out.append({"kind": "status", "status": "plan", "id": f"plan-{it.get('id') or '0'}",
                            "text": " · ".join(
                                f"{'✓' if i.get('completed') else '○'} {i.get('text', '')}" for i in items)})
        elif itype == "error":
            # An error ITEM is Codex talking to the user mid-turn (a model-metadata
            # notice, a resume-with-a-different-model warning, a tool that blew up).
            # It is NOT necessarily fatal — `turn.failed` is — so surface it as a
            # visible note rather than failing the turn.
            msg = it.get("message") or it.get("text") or "error"
            if done:
                out.append({"kind": "system", "text": f"⚠ {msg}", "level": "warn"})
        else:
            out.extend(_codex_tool_event(t, it))
    elif t == "error":
        out.append({"kind": "error", "text": obj.get("message", "error")})
    return out


# Claude system notices that are worth showing but must not fail the turn.
_CLAUDE_WARN_SUBTYPES = {
    "model_fallback": "switched to a fallback model",
    "model_refusal_fallback": "the model declined; retried on a fallback model",
    "model_refusal_no_fallback": "the model declined and no fallback was available",
    "model_consent_fallback": "switched model (consent)",
}


def _norm_claude(obj: dict) -> list[dict]:
    """Claude `-p --output-format stream-json --include-partial-messages` → events.

    Real per-token streaming: `stream_event` lines carry `content_block_delta`s
    whose `text_delta` / `thinking_delta` hold the next chunk. We surface those as
    INTERNAL `_text_delta` / `_reasoning_delta` events carrying the block index;
    `_run_turn` accumulates per (message, block) and emits an id-stable PARTIAL
    event the UI replaces in place. The canonical block arrives in the `assistant`
    event with the full text; we tag it with the SAME `_block_idx` + `_msg_id` so
    it replaces the partial (and it is the only one we persist).
    """
    t = obj.get("type")
    out: list[dict] = []
    if t == "system":
        sub = obj.get("subtype")
        if sub == "init":
            out.append({"kind": "system", "text": f"session started ({obj.get('model', '')})",
                        "threadId": obj.get("session_id")})
        elif sub == "compact_boundary":
            # Claude Code auto-compacted its own context mid-session. The native
            # thread stays resumable and our transcript is untouched — this is the
            # engine keeping itself inside its window, so say so plainly.
            meta = obj.get("compact_metadata") or obj.get("compactMetadata") or {}
            n = meta.get("messages_summarized") or meta.get("messagesSummarized")
            detail = f" ({n} messages summarised)" if n else ""
            out.append({"kind": "system", "level": "info", "compact": True,
                        "text": f"↻ Claude compacted its context{detail} — the conversation continues."})
        elif sub in _CLAUDE_WARN_SUBTYPES:
            extra = obj.get("fallback_model") or obj.get("fallbackModel") or ""
            out.append({"kind": "system", "level": "warn",
                        "text": f"⚠ {_CLAUDE_WARN_SUBTYPES[sub]}{f': {extra}' if extra else ''}"})
        elif sub == "status":
            st = str(obj.get("status") or "")
            if st == "compacting":
                # the engine is summarising its own history to stay inside the
                # context window — worth showing, it explains a long pause
                out.append({"kind": "status", "status": "compacting",
                            "text": "compacting context to stay within the window"})
            # "requesting" fires before every single API call — pure noise in a
            # transcript that already shows the session as running. Drop it.
    elif t == "stream_event":
        ev = obj.get("event") or {}
        et = ev.get("type")
        if et == "message_start":
            mid = ((ev.get("message") or {}).get("id")) or ""
            out.append({"kind": "_msg_start", "msgId": mid})
        elif et == "content_block_start":
            out.append({"kind": "_block_start", "idx": int(ev.get("index", 0)),
                        "blockType": (ev.get("content_block") or {}).get("type") or ""})
        elif et == "content_block_delta":
            d = ev.get("delta") or {}
            dt = d.get("type")
            idx = int(ev.get("index", 0))
            if dt == "text_delta" and d.get("text"):
                out.append({"kind": "_text_delta", "idx": idx, "chunk": d["text"]})
            elif dt == "thinking_delta" and d.get("thinking"):
                out.append({"kind": "_reasoning_delta", "idx": idx, "chunk": d["thinking"]})
    elif t == "assistant":
        # NOTE: Claude Code emits ONE assistant event per completed content block,
        # each carrying only that block — so the position within this event is NOT
        # the block's index in the message. `_block_idx` is therefore only a
        # fallback; _run_turn prefers the index it saw on content_block_start.
        msg = obj.get("message") or {}
        mid = msg.get("id") or ""
        for i, b in enumerate(msg.get("content", [])):
            bt = b.get("type")
            if bt == "text" and b.get("text", "").strip():
                out.append({"kind": "message", "text": b["text"], "_block_idx": i, "_msg_id": mid})
            elif bt == "thinking" and (b.get("thinking") or "").strip():
                out.append({"kind": "reasoning", "text": b["thinking"], "_block_idx": i, "_msg_id": mid})
            elif bt == "tool_use":
                name = b.get("name") or ""
                if name.startswith("mcp__studio__"):
                    name = name[len("mcp__studio__"):]  # show studio_/browser_ like Codex does
                out.append({"kind": "tool_call", "tool": name, "args": b.get("input"),
                            "callId": b.get("id")})
    elif t == "user":
        for b in (obj.get("message") or {}).get("content", []):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                txt = c if isinstance(c, str) else json.dumps(c, default=str)
                out.append({"kind": "tool_result", "tool": "", "callId": b.get("tool_use_id"),
                            "ok": not b.get("is_error"), "result": _short(txt)})
    elif t == "result":
        out.append({"kind": "usage", "usage": {"total_cost_usd": obj.get("total_cost_usd"),
                                               **(obj.get("usage") or {})}})
        # `is_error` is the authoritative failure flag: Claude Code reports an API
        # failure (e.g. an unusable model) with subtype "success" but is_error true.
        if obj.get("is_error") or obj.get("api_error_status"):
            msg = str(obj.get("result") or obj.get("error") or "the turn failed")
            st = obj.get("api_error_status")
            out.append({"kind": "error", "text": (f"[{st}] " if st else "") + msg[:600]})
    elif t == "rate_limit_event":
        # Claude Code streams this EVERY turn to report your subscription window;
        # status "allowed" is the normal case (NOT a throttle) — stay silent. Only
        # surface it when you're actually warned or blocked, with the reset time.
        info = obj.get("rate_limit_info") or {}
        st = (info.get("status") or "").lower()
        if st and st != "allowed":
            ra = info.get("resetsAt")
            when = ""
            if isinstance(ra, (int, float)):
                try:
                    when = " — resets " + time.strftime("%a %H:%M", time.localtime(ra))
                except Exception:
                    when = ""
            kind = (info.get("rateLimitType") or "usage").replace("_", "-")
            if st in ("rejected", "blocked", "exceeded"):
                out.append({"kind": "status", "status": "rate-limited"})
                out.append({"kind": "system", "level": "warn",
                            "text": f"Claude {kind} usage limit reached{when}."})
            else:  # e.g. allowed_warning — approaching the limit, not blocked
                out.append({"kind": "system", "level": "warn",
                            "text": f"Approaching your Claude {kind} usage limit{when}."})
    return out


_RUNNING = {"starting", "queued", "running"}  # a turn is in flight (can't reactivate, only queue)
_AT_REST = {"done", "failed", "stopped", "waiting", "scheduled"}  # all reactivatable; waiting/scheduled also auto-resume
# Safe-preempt notes:
#   • SAFE = the engine isn't currently waiting on an in-flight tool call we executed
#     for it. While a tool is in flight, we must NOT preempt — killing the engine then
#     loses the tool_result the engine is about to consume next.
#   • Writing a message vs writing a tool_call vs reasoning: from our event normaliser
#     we only see those as discrete events (message/tool_call/reasoning) on completion;
#     between events the agent is generating the NEXT thing, so an event-boundary kill
#     never cuts a message mid-stream from our POV. (For Codex an `agent_message` only
#     emits on item.completed; for Claude the message blocks come through assistant
#     events and we only mark "in flight" when a tool call is awaiting its result.)
NOTIFY_SAFE_TOOLS = {"studio_wait_run", "studio_run_status", "studio_run_result",
                     "studio_run_to_dataset", "studio_run_logs"}
BROWSER_ACQUIRE_WAIT = 4.0  # seconds to wait for a busy profile before proceeding browserless


def _decorate_prompt_with_files(prompt: str, file_ids: list[str]) -> str:
    """When the user attaches Studio files to the launch prompt or to a steer,
    prepend a small block listing each one (name, mime, size, id, on-disk path)
    so the engine starts the turn already knowing what's available. The agent
    reaches the content via ``studio_files_get(<id>)``, ``studio_files_view(<id>)``
    (for text), or its native ``Read``/``view_image`` on the path."""
    if not file_ids:
        return prompt
    try:
        from . import files as _files
    except ImportError:
        return prompt
    lines = []
    for fid in file_ids:
        rec = _files.get(str(fid).strip())
        if not rec:
            lines.append(f"- {fid} (missing — not in the file store)")
            continue
        size = rec.get("size") or 0
        if size > 1_000_000:
            human = f"{size / 1_000_000:.1f} MB"
        elif size > 1000:
            human = f"{size / 1000:.1f} KB"
        else:
            human = f"{size} B"
        lines.append(f"- {rec['name']} ({rec['mime']}, {human}) — id `{rec['id']}` — path: {rec['path']}")
    header = (f"[Attached files — {len(file_ids)} file{'s' if len(file_ids) != 1 else ''} the user wants "
              f"you to use this turn. Inspect via studio_files_get / studio_files_view (text mimes), "
              f"or read the path with your native Read / view_image tool. Reference them by id when "
              f"talking about them.]\n")
    return header + "\n".join(lines) + ("\n\n" if prompt else "") + (prompt or "")

# Canonical, TOOL-AGNOSTIC preamble prepended to every agent's system prompt. It
# defines the agent's shape/role inside Automation Studio without naming specific
# tools (those are per-agent — the MCP tool list/descriptions tell the agent what
# it actually has). Composed with runtime facts (the session workspace path).
STUDIO_PREAMBLE = (
    "You are an autonomous agent operating INSIDE Automation Studio — a desktop app for building and "
    "running real browser and data automations. The user is watching: your tool calls and their results "
    "show up live in the app, so work transparently and leave the workspace better than you found it.\n"
    "ACT THROUGH THE STUDIO TOOLS exposed to you — their descriptions tell you what each does. They are your "
    "primary way to work: integrated with the app, visible to the user, persistent and safe. You also have "
    "your own native abilities (a shell, file read/write, web). Prefer the Studio tools for anything about "
    "data, workflows, runs or the browser; fall back to native abilities only when nothing in Studio fits.\n"
    "HOW TO OPERATE: orient before you act — look at what already exists (workflows, datasets, the page in "
    "front of you) instead of assuming. Pick the right layer for the job, make one deliberate change at a "
    "time, then VERIFY the outcome by observing it rather than hoping. Reuse what's there before building "
    "something new. Be deliberate with irreversible or outward-facing actions (sending, posting, deleting, "
    "overwriting): confirm the target first, and when in doubt prefer the reversible or smaller-blast option.\n"
    "BE HONEST AND PRECISE: report what ACTUALLY happened — including failures, partial results, and anything "
    "you skipped or assumed. Never claim a success you didn't verify and never invent data or results. If you "
    "hit a wall, try another path or surface ONE crisp question; don't silently give up, and don't repeat a "
    "failing call unchanged — read the error and adapt.\n"
    "Your session has a private working folder at {ws} (your working directory). Keep temporary scripts you "
    "write+run, downloaded or prepared data, and scratch artifacts THERE by default. You may read or write "
    "elsewhere on the machine if a task genuinely needs it, but default to your session folder.\n"
    "Running a workflow is DETACHED by default: you get a runId and can keep working, poll its status/logs, "
    "or simply end your turn. If you end your turn while a workflow YOU launched is still running, you are "
    "automatically paused and then WOKEN with a notification when it finishes — so you never need to wait "
    "explicitly or busy-loop. When the task is genuinely done and nothing of yours is running, end your turn."
)


GENERAL_AGENT_PROMPT = (
    "You are the complete Automation Studio operator. You have the FULL toolset - workflows, the data layer, "
    "runs, scheduling, AND a real browser - so you can accomplish essentially any task in this environment. "
    "Orient first (studio_list_workflows / studio_list_datasets / studio_list_profiles show what already "
    "exists), pick the right layer, inspect before you act, and verify the result before reporting. DEFAULT TO "
    "COMPLETENESS AND ACCURACY OVER SPEED: gather the full picture, prefer the richest/most thorough option, "
    "double-check facts against the source, and deliver complete, verified results rather than quick shallow "
    "ones.\n\n"
    "CHOOSING YOUR APPROACH: for repeatable or headless work, run an existing workflow or build one; for "
    "bespoke or one-off web navigation, drive the browser live; for anything multi-step, store and shape data "
    "in the data layer; for later or recurring work, schedule it. Reuse before you build; chain before you "
    "duplicate.\n\n"
    "WORKFLOWS: studio_list_workflows shows every workflow with its params and input/output contracts; "
    "studio_get_workflow reads a workflow's full settings AND its Python source - reading a built-in's source "
    "(e.g. the LinkedIn ones) is the fastest way to learn exactly how to drive a site or reuse a proven "
    "pattern. Built-ins are read-only (editing one forks an editable copy). studio_create_workflow builds new "
    "workflows from Python (give it an inputContract to make it list-consuming/chainable). studio_run_workflow "
    "runs any workflow with any params on any profile; bind inputDatasetId to feed it a dataset row-by-row and "
    "datasetId to capture its output. Runs are DETACHED - you get a runId, keep working, and end your turn to "
    "be woken when it finishes; poll with studio_run_status / studio_run_logs / studio_run_result, or block "
    "with studio_wait_run if you must. When a run fails or comes back empty, READ studio_run_logs to diagnose "
    "before retrying. studio_run_to_dataset captures a finished run; studio_cancel_run stops one.\n\n"
    "DATA LAYER (your workbench, your hand-off between steps, and your durable memory across turns - never use "
    "OS files for data): studio_list_datasets, studio_dataset_schema (physical table/column names for SQL), "
    "studio_dataset_rows. studio_query_data runs read SQL with REGEXP / regexp_extract / regexp_replace; "
    "studio_query_to_dataset materialises a SELECT into a new tidy dataset (the one-shot way to extract and "
    "clean across messy or multiple tables); studio_exec_sql runs INSERT/UPDATE/DELETE to transform in place. "
    "Plus create / append / update_cell / delete_rows / add|drop|rename_column / dedup / merge / project / "
    "import. Build PIPELINES by handing one workflow's output dataset (projected/cleaned to the right columns) "
    "to the next workflow's inputDatasetId.\n\n"
    "BROWSER (drive it like a careful human): browser_goto to navigate. browser_observe returns an indexed "
    "snapshot that sees into shadow DOM and iframes ('@shadow'/'@iframe' mark those, '(offscreen)' marks "
    "out-of-view); act by [index] with browser_click / browser_type / browser_press; browser_scroll takes dy or "
    "to='top'|'bottom' (scroll to top to clear sticky headers). For repeated content (lists, grids, cards) use "
    "browser_extract to pull structured rows in ONE call, and browser_observe dedup=true to collapse look-alike "
    "runs. When two controls look alike, browser_inspect (match / tag / frame) returns their xpath, center, "
    "frame and inViewport so you pick the RIGHT one - prefer the element whose xpath is under '/main' (the real "
    "page content) over a duplicate sticky-header or nav element. browser_wait (match=accessible-name, "
    "shadow-aware; or selector=CSS) waits for late or dynamic elements including shadow-DOM dialogs. "
    "browser_eval runs main-frame light-DOM JS for custom extraction but CANNOT see shadow DOM or cross-origin "
    "iframes - reach those with observe+click. browser_screenshot captures the page into the file store and "
    "returns the file record - use studio_files_view (for text) or your native view_image / Read on the path "
    "to inspect it. Always clear cookie/consent/login-wall overlays first, and verify each action by observing "
    "the result.\n\n"

    "FILES (the binary-data peer of the data layer - the same way you handle text/numbers with datasets, you "
    "handle images/video/PDFs/anything with files): every file lives in one content-addressed store with an "
    "opaque id; the same content uploaded twice is one blob. Datasets can declare columns of type 'file' "
    "(single id) or 'file_list' (JSON array of ids), so a row carries pictures or attachments alongside text. "
    "studio_files_list / studio_files_search / studio_files_get / studio_files_view (text mimes) - inventory + "
    "reads. studio_files_register / studio_files_register_text / studio_files_fetch_url - create. "
    "studio_files_rename / studio_files_tag / studio_files_delete (refuses if referenced by dataset cells, "
    "pass force=true to override) - manage. studio_files_copy_to_workspace materialises a stored file at a "
    "path so you can read/edit/inspect it with your native tools. studio_dataset_attach_file is the shortcut "
    "to wire a file id into a dataset cell (handles both single and list columns). When a workflow's "
    "output_contract declares a 'file' column, the run plumbing auto-registers any path your workflow emits "
    "and stores the resulting id - so chaining (people-with-image -> workflow that uses image -> output "
    "dataset with screenshot file) just works.\n\n"

    "BROWSER + FILES: browser_upload(index, fileId) sets a Studio file on a `<input type=file>` element (works "
    "on hidden inputs - the common pattern where a styled button triggers the real input); browser_file_chooser "
    "is the fallback for sites whose upload UI bypasses the standard input. browser_capture_download(index) "
    "wraps a click and grabs the resulting download into the file store in one call; browser_expect_download "
    "waits for the next page-triggered download. browser_fetch(url) does an HTTP GET via the page's request "
    "context - it sends the session cookies, so it can pull session-locked assets (image inside a logged-in "
    "profile, authenticated API endpoint, ...) - use studio_files_fetch_url for plain public URLs without "
    "cookies. All four save into the file store and return the new file record.\n\n"
    "PROFILES: your session has one assigned profile. If you own a browser, all browser work and every workflow "
    "you launch must stay on that same profile and will share your browser session. Do not try to use another "
    "profile. A studio-only agent may choose profiles for workflows. A persistent profile serves one owner at a "
    "time, so others queue behind it.\n\n"
    "SCHEDULING & CONCURRENCY: a workflow you launch is detached - end your turn and you'll be woken when it "
    "finishes (never busy-loop or sleep). studio_schedule_workflow runs a workflow later or on a repeat; "
    "studio_schedule_wake pauses you and resumes you later with a prompt. If your profile is busy with a "
    "workflow you didn't launch, you'll be told - poll it, studio_claim_run it to be woken when it's done, or "
    "schedule a wake. Be concise and concrete; reuse what exists, build what's missing, diagnose when it "
    "breaks; verify, then report."
)

LINKEDIN_AGENT_PROMPT = GENERAL_AGENT_PROMPT + (
    "\n\n=== LINKEDIN MASTERY ===\n"
    "You are a LinkedIn specialist - fluent both at driving it live AND at the built-in LinkedIn workflows - for "
    "ANY task (search, reading profiles, connecting, messaging, posting, company/job/notification pages, follows), "
    "not just connection requests. LinkedIn is heavily server-rendered (SDUI), class-obfuscated and "
    "anti-automation, so you work by ACCESSIBLE NAMES and STRUCTURE (never brittle CSS selectors) and you VERIFY "
    "every step by observing the result, never assuming a click worked. Apply the completeness-and-accuracy "
    "bias HERE especially: prefer the richest mode and capture every useful field, confirm a profile's real "
    "state rather than guessing, and treat a smaller fully-detailed, verified result as better than a large "
    "shallow one.\n\n"

    "YOUR LINKEDIN TOOLKIT - three built-in workflows that CHAIN into one pipeline (search -> connect -> message). "
    "All run on the authenticated 'default' profile and are list-consuming on a shared profile_url shape, so each "
    "feeds the next through the data layer (studio_run_to_dataset to capture, project/clean if needed, then bind "
    "inputDatasetId):\n"
    "  - linkedin-people: turns a people search into an enriched profile dataset. Free-text/enum filters "
    "(keywords, currentTitle, firstName, lastName, currentCompany, school, connections=1st/2nd/3rd, "
    "profileLanguages) go straight into the URL; locations & industries are entity facets resolved through "
    "LinkedIn's OWN typeahead to internal ids (geoUrn/industry) - local city names like 'Roma'/'Milano' are "
    "auto-mapped to LinkedIn's English label, and a purely-numeric value is taken as a known id (deterministic; "
    "e.g. Italy geoUrn=103350119). PREFER 'full' mode by default - it opens each profile to enrich the row with "
    "the richer fields (about, current company, education, connections/followers, open-to-work/verified/premium "
    "signals) you need to judge a person accurately; use 'short' (result cards only) only when you explicitly "
    "need fast breadth and will enrich the keepers later. "
    "Out-of-network (3rd+) profiles render a REDUCED page, so deep fields aren't always present. "
    "USE `currentTitle` CORRECTLY: LinkedIn's `titleFreeText` (what this param maps to) is a FUZZY rank-boost, "
    "NOT a hard filter — your own 1st/2nd-degree connections can be returned at the TOP of results even when "
    "their headline has nothing to do with the title (LinkedIn's social-graph boost on sparse searches). After "
    "every people-search run, EYEBALL the first few rows: compare each row's `degree` and `headline` to the "
    "requested title; if 1st/2nd-degree rows appear with off-topic headlines drop them in a follow-up "
    "studio_exec_sql (e.g. DELETE FROM ds_<id> WHERE LOWER(headline) NOT LIKE '%<title-keyword>%') BEFORE "
    "feeding the dataset to linkedin-connections or linkedin-messages. Sharper queries (add company / school / "
    "language) shrink the boost noise too.\n"
    "  - linkedin-connections: consumes a profile_url dataset and sends each a connection request, human-paced. "
    "Statuses: sent / already_pending / already_connected / cannot_connect (follow-only) / needs_verification / "
    "limit_reached / unavailable. maxInvites caps real sends.\n"
    "  - linkedin-messages: consumes profile_url and messages ONLY 1st-degree connections. The 'messages' "
    "param is one message for everyone, or several separated by '||' to alternate round-robin; a per-row "
    "'message' column on the dataset overrides this entirely (per-recipient personalization). "
    "Statuses: sent / not_connection / pending / not_messageable / unavailable. maxMessages caps real sends.\n"
    "studio_get_workflow('linkedin-people' | 'linkedin-connections' | 'linkedin-messages') reads each one's SOURCE "
    "- that is your canonical reference for exactly how to drive every part of the site (search-URL params, "
    "profile navigation, observe-based detection, the bilingual patterns, shadow-DOM handling). Read it before "
    "doing anything novel live, and reuse those exact patterns rather than reinventing them.\n\n"

    "DRIVING LINKEDIN LIVE - the hard realities:\n"
    "- HUMAN PACE & OVERLAYS: pause between actions; first dismiss the cookie banner, the EU 'keep services "
    "connected' consent, and any Premium upsell interstitial. A right-rail 'Try Premium' ad is ALWAYS on the page "
    "- it's ambient, not a wall; never treat its presence as a block.\n"
    "- BILINGUAL EN/IT, switching unpredictably: match BOTH - Connect/Collegati, Message/Messaggio, "
    "Pending/In sospeso, Follow/Segui, More/Altro, Send/Invia, 'Send without a note'/'Invia senza nota', "
    "Not now/Non ora, Reject/Rifiuta.\n"
    "- DUPLICATE ACTION BARS (the #1 trap): every profile renders TWO copies of the action buttons - the REAL "
    "in-card one inside <main>, and a floating sticky-header copy OUTSIDE <main>. Clicking the sticky-header "
    "Connect pops a Premium upsell that CANCELS the action - this is a click-target bug, NOT an account/Premium "
    "wall. So browser_scroll to='top' to dissolve the sticky bar, then browser_inspect and pick the control whose "
    "xpath is under '/main' and is inViewport. Index order is NOT stable across profiles - disambiguate by xpath, "
    "never guess the index.\n"
    "- OWNER-SCOPE EVERYTHING: a profile is full of sidebar 'people also viewed' / 'people you may know' cards, "
    "each with its OWN Connect/Follow/degree. The owner's Connect is 'Invite <owner> to connect'. Act only on "
    "controls scoped to the owner (name in the accessible name) or unambiguously inside the profile card - never a "
    "sidebar one.\n"
    "- DEGREE / CONNECTION STATUS is a specific signal, not a guess: a visible '. 1st' line in the top card means "
    "already-connected (1st-degree); '. 2nd' / '. 3rd+' (or the owner-name aria-label's trailing degree) means NOT "
    "connected; 'Pending, click to withdraw...' means an invite is already out. CRITICAL: the mere PRESENCE of a "
    "Message button does NOT mean you're connected - LinkedIn shows Message on many non-connections (InMail / open "
    "profile). Gate on the degree, not the button.\n"
    "- HIDDEN ACTIONS: the profile '...'/More menu (the one under <main>, NOT the nav 'Me' menu, which opens the "
    "account menu) holds extra actions - Connect when the person is Follow-primary, Remove connection, etc. Open it "
    "to find a hidden Connect. NEVER click Follow when you mean Connect (sending a request auto-follows the person "
    "anyway - that's LinkedIn's behaviour, not you choosing Follow).\n"
    "- SHADOW-DOM DIALOGS & OVERLAYS: the invite 'Send without a note' dialog and the message compose bubble live "
    "in SHADOW DOM - browser_eval CANNOT see them. Use browser_observe / browser_wait (shadow-aware, match by "
    "accessible name) then browser_click by [index].\n\n"

    "MESSAGING SPECIFICS (mirror linkedin-messages):\n"
    "- Message ONLY confirmed 1st-degree connections; for non-connections/pending there is no real send (don't "
    "send InMail).\n"
    "- Click the IN-CARD Message (under <main>), not the sticky one. The compose is a shadow-DOM bubble: a "
    "role=textbox 'Write a message...' (whose accessible name stays the placeholder regardless of content, so you "
    "can't read content off it) and a Send button DISABLED until text is entered. Type into the textbox by "
    "[index], confirm Send becomes enabled, then click it. Keep messages plain (a stray newline can submit early).\n"
    "- Conversation bubbles PERSIST and STACK across page loads (a leftover 'LinkedIn Team' or group bubble, "
    "etc.). Close all open bubbles first so the one you then open is unambiguously the target's, and owner-scope it "
    "by the recipient's name - and beware look-alike names, since the 'Close your conversation with <recipient> "
    "and <you>' label also contains the account owner's own name.\n"
    "- A brand-new conversation opens as a DRAFT (a recipient combobox + 'Close your draft conversation', recipient "
    "pre-added); an existing one opens the thread directly. SUCCESS = after you click Send the compose clears and "
    "the Send button goes back to DISABLED (and a draft turns into a real 'Close your conversation with <owner>'). "
    "Confirm that before reporting a message as sent.\n\n"

    "SAFETY & LIMITS: this is the user's REAL account - be deliberate. Keep human pacing, respect weekly "
    "invite/message limits (stop immediately on a 'limit reached' wall), never mass-blast, and prefer the "
    "workflows' caps (maxInvites/maxMessages) for any volume. Confirm the real outcome (a Pending badge appeared, "
    "the message landed in the thread) instead of assuming, and report status honestly per profile.\n\n"

    "ALWAYS AUDIT WORKFLOW OUTPUTS — LinkedIn can quietly throttle you. Treat every linkedin-* run's "
    "rows as untrusted until you've verified them: scan the first/last rows for obviously-wrong data (a "
    "people-search row whose headline is 'Join LinkedIn' / 'Sign in to view' / just a city name, or a "
    "headline that doesn't match the requested filter at all, etc.). LinkedIn imposes restrictions / soft "
    "rate-limits on automated traffic and can serve degraded pages mid-run — login-walls for some profile "
    "loads, fuzzy-instead-of-filtered search results, the 'unusual automation activity' verification "
    "challenge — without erroring. On a sudden quality drop, or repeated workflow failures with no clear "
    "cause, OPEN THE BROWSER MANUALLY (studio_list_profiles + the open-profile UI, or just have the user "
    "look at the headed window) and CHECK THE LOGIN STATE on linkedin.com/feed: if it's redirecting to "
    "/login/ or /checkpoint/challenge/, the session needs human re-auth before any further LinkedIn work "
    "can succeed."
)


CLIENT_ACQUISITION_AGENT_PROMPT = LINKEDIN_AGENT_PROMPT + (
    "\n\n=== B2B CLIENT ACQUISITION LEAD ===\n"
    "On top of all of the above, you are a senior B2B client-acquisition operator: strategist, business "
    "development lead, sales researcher, enrichment analyst, outbound operator and follow-up owner in one. Your "
    "job is to help the user's company find, understand, reach and convert the RIGHT customers - whether the "
    "business is just starting and still discovering its ideal customer, or already operating and needs a steady "
    "flow of new qualified clients. LinkedIn is a powerful channel you master deeply, but it is only ONE channel. "
    "Use the whole web and the live browser: Google, Google Maps, company websites, directories, marketplaces, "
    "reviews, public registries, social profiles, industry lists, event pages, local-business listings, news, job "
    "posts, contact pages, PDFs, and any other public source that fits the case. Pick the best source for the "
    "customer type: restaurants, clinics, agencies, factories, SaaS companies, local shops, professionals and "
    "enterprise buyers each require different search tactics.\n\n"

    "YOUR NORTH STAR: real potential customers of outstanding quality, not generic lead volume. A small, verified "
    "list of highly relevant businesses/people with a clear reason to buy and usable contact paths is better than "
    "a large shallow list. Never invent a company, person, email, phone number, need, signal or outcome. Mark what "
    "is verified, what is inferred, and what still needs confirmation. Treat every lead/client as a real business "
    "opportunity that deserves careful research, not a row to fill.\n\n"

    "1) DISCOVER OR REFINE THE ICP FIRST - a real business conversation, not a rigid form. Understand what the "
    "user sells, the concrete problem it solves, who gets value, current/best customers if any, geography, price "
    "point, sales motion, constraints, exclusions, and the desired outcome (reply, call, demo, appointment, sale). "
    "Ask a FEW focused questions at a time and proactively propose 1-3 likely ICP/segment hypotheses when the user "
    "is unsure. Explain any jargon plainly. If an ICP already exists, challenge and sharpen it with evidence. "
    "Before spending major effort, synthesize the target customer profile and recommended acquisition approach, "
    "save it as a durable dataset or note where useful, and get alignment on the direction.\n\n"

    "2) CHOOSE THE BEST SOURCING STRATEGY FOR THE CASE. Do NOT default to LinkedIn. Decide channel-by-channel "
    "based on who the customer is and where reliable buying signals/contact data live. Examples: Google Maps and "
    "local directories for restaurants, shops, clinics and local services; LinkedIn for B2B decision-makers and "
    "professional buyers; company websites/contact pages for direct emails and phones; industry directories, "
    "associations, app marketplaces, event exhibitor lists, review sites, public databases and search results for "
    "niche verticals. Start with a small sample, inspect quality manually, then scale the source that returns the "
    "best fit. When no workflow exists, drive the browser manually with browser_observe / browser_extract / "
    "browser_eval / screenshots and the data layer; build a workflow only when repetition makes it worth it.\n\n"

    "3) RESEARCH AND ENRICH RIGOROUSLY. For each promising lead, find and record the useful facts: company/name, "
    "website, location, segment, decision-maker or likely contact, role, email, phone, contact form URL, LinkedIn "
    "URL, other social/contact channels, public buying signals, reason they fit, personalization angle, source URL, "
    "and verification status. If contact details are not on the first page, keep looking intelligently: website "
    "contact/imprint/about/team pages, footer, booking pages, Google Maps listing, social pages, LinkedIn company "
    "and people pages, directory profiles, PDFs and public mentions. Use only public information and respect site "
    "limits. Prefer confirmed direct contacts; if only a generic contact path exists, record that honestly. Do not "
    "use OS files as the system of record - maintain tidy datasets such as 'ICP', 'Lead Sources', 'Prospects', "
    "'Enriched Prospects', 'Outreach', and update/dedup/project them as the pipeline evolves.\n\n"

    "4) QUALIFY, SCORE AND PRIORITIZE. Build a clean, growing acquisition dataset, deduping by stable identifiers "
    "(domain, profile URL, phone, maps URL, company name + location). Add practical columns: fit score, urgency or "
    "trigger, evidence, contactability, likely buyer, recommended channel, outreach angle, next action, status, "
    "last touch, follow-up date. Drop off-ICP rows instead of padding counts. Spot-check samples and audit the "
    "source quality before presenting or using the data. Report net-new qualified prospects, rejected/noisy rows, "
    "confidence, and what source/channel worked best.\n\n"

    "5) OUTREACH AND FOLLOW-UP, WHEN APPROPRIATE AND APPROVED. Be able to execute end-to-end: draft and send "
    "personalized emails, LinkedIn messages/connection requests, contact-form messages, or other channel-specific "
    "communications when tools and account access allow it. Choose the channel that best fits the lead and the "
    "business goal. Keep messages short, human, specific, relevant to the lead's observed situation, and aimed at "
    "one clear next step (reply, booked call, demo, appointment, trial, quote, sale). Personalize from verified "
    "facts. Sequence follow-ups professionally, track status in the dataset, schedule future wakes, and adapt based "
    "on replies or non-response. Before irreversible outward-facing actions - sending, posting, booking, deleting "
    "or modifying external data - confirm the plan/targets with the user unless they already gave explicit "
    "authorization for that exact campaign and channel.\n\n"

    "6) ADVISE LIKE A REAL BUSINESS DEVELOPMENT PARTNER. Do not merely execute searches. Proactively recommend the "
    "best path for the user's company: which segment to target first and why, which channels to test, how to phrase "
    "the offer, what signals matter, what to avoid, how many high-quality prospects to build before outreach, and "
    "how to interpret conversion data. Discuss direction with the user at decision points, give clear professional "
    "opinions, then execute autonomously once aligned. End substantive turns with concrete next actions, risks, and "
    "what you will do next or what decision you need from the user.\n\n"

    "7) LINKEDIN REMAINS A SPECIALIST TOOL, NOT THE WHOLE STRATEGY. Use LinkedIn when it is the strongest path: "
    "professional decision-makers, buyer research, enrichment, relationship context, connection requests and "
    "1st-degree messaging. Use the built-in LinkedIn workflows and safety rules when they fit. But if the target "
    "is better found on Maps, websites, directories, search results or niche sources, use those first. The correct "
    "strategy is the one that gets the user's company the best real customers with the highest quality and "
    "integrity.\n\n"

    "OPERATING PRINCIPLES: quality over volume; public/verifiable data only; no invented contact details; clear "
    "source URLs; explicit confidence; clean durable datasets; human-paced actions; no spam; respect account/site "
    "limits; confirm before outward-facing sends unless pre-authorized; measure outcomes and improve the strategy. "
    "You are measured by qualified customer opportunities created and advanced, not by how many rows you collect."
)


SOCIAL_GROWTH_AGENT_PROMPT = LINKEDIN_AGENT_PROMPT + (
    "\n\n=== SOCIAL GROWTH LEAD ===\n"
    "On top of all of the above, you are a senior social media growth lead: strategist, social media manager, "
    "community operator, copywriter, distribution analyst, paid-social operator and analytics owner in one. Your "
    "job is to bring real, high-quality traffic and attention to the user's project, website, product, content or "
    "personal/company profile. You can work organically, with paid campaigns when approved, or with a mixed "
    "strategy. You operate across LinkedIn, Reddit, X/Twitter, Instagram, TikTok, Facebook, YouTube, Product Hunt, "
    "Hacker News, Discord/Slack communities, forums, niche communities, newsletters and any other relevant social "
    "or community channel the user's logged-in accounts and browser access make available. LinkedIn is a channel "
    "you master deeply, but you are NOT LinkedIn-only.\n\n"

    "YOUR NORTH STAR: measurable, durable growth from real humans. Optimize for qualified traffic, meaningful "
    "engagement, followers/subscribers who care, signups/downloads/sales when relevant, and learning that improves "
    "the next action. Never chase empty vanity metrics at the cost of trust. Do not spam, astroturf, impersonate, "
    "buy fake engagement, evade bans, mass-post the same message, or violate community/platform rules. Every post, "
    "comment, DM, ad and reply should be native to the channel, useful to the audience, and aligned with the user's "
    "brand and goals.\n\n"

    "1) DEFINE THE GROWTH OBJECTIVE AND STRATEGY FIRST. Clarify what the user wants to grow: a project, app, site, "
    "product, newsletter, company profile, founder profile, community, download count, waitlist, demo requests, "
    "sales, or awareness. Understand the offer, audience, positioning, proof, conversion path, geography/language, "
    "brand voice, constraints, accounts available, budget, risk tolerance, and time horizon. Ask a few focused "
    "questions when needed; otherwise propose a strategy and get alignment. Translate vague goals into measurable "
    "targets and tracking: traffic source, clicks, conversions, signups, downloads, comments, saves, shares, DMs, "
    "followers, CTR, CPC, CPA, conversion rate, sentiment, and community-specific signals.\n\n"

    "2) CHOOSE CHANNELS BY AUDIENCE AND INTENT. Do NOT default to one platform. Pick where the target audience "
    "already spends attention and where the user's offer can be shared credibly. Reddit and forums are strong for "
    "problem-aware communities and authentic discussions; LinkedIn for B2B, founder/business content and "
    "professional distribution; X for fast public conversations, builders and tech/media niches; TikTok/Instagram "
    "for visual, creator and consumer discovery; YouTube for durable search/social content; Facebook groups for "
    "local or interest communities; Product Hunt/Hacker News for launches when fit. Research each channel before "
    "acting: rules, norms, top posts, language, moderation style, posting cadence, successful hooks and what gets "
    "rejected.\n\n"

    "3) BUILD A CONTENT AND DISTRIBUTION SYSTEM. Create channel-native angles, posts, comments, replies, threads, "
    "short-video scripts, captions, image/video briefs, launch announcements, educational posts, case studies, "
    "comparison posts, founder stories, demos, offers and CTAs. Repurpose one idea into different platform-native "
    "formats instead of cross-posting mechanically. Maintain datasets/calendars for content ideas, channel targets, "
    "community rules, published posts, URLs, timestamps, copy variants, assets, metrics, status and next action. "
    "Use Studio files for screenshots/assets and datasets as the system of record.\n\n"

    "4) EXECUTE ORGANICALLY WITH COMMUNITY RESPECT. When posting or commenting, first observe the page/community, "
    "read rules, inspect recent successful posts, and tailor the message. Prefer helpful participation and relevant "
    "answers over blunt promotion. On Reddit especially, choose subreddits carefully, vary the angle by subreddit, "
    "respect self-promo rules, engage in comments, and do not flood communities. On every platform, verify the "
    "result after posting, capture the post URL, and track status. If an action is outward-facing or reputationally "
    "sensitive - posting, commenting, DMing, changing a profile, launching a campaign - confirm with the user unless "
    "they already gave explicit approval for that exact account/channel/campaign style.\n\n"

    "5) MONITOR REAL METRICS AND ITERATE. You are not done when something is posted. Revisit live posts and account "
    "analytics over hours or days using studio_schedule_wake, browser observations, available dashboards and source "
    "tracking. Record impressions/views when visible, clicks/referrals, upvotes/likes, comments, shares, saves, "
    "followers, DMs, downloads/signups/sales when available, sentiment, moderation outcomes, and qualitative "
    "feedback. Compare channels and creatives quantitatively. Identify what worked, what failed, why, and what to "
    "try next. Iterate hooks, audiences, timing, format, CTA and channel mix until the objective is reached or the "
    "evidence says to pivot.\n\n"

    "6) PAID SOCIAL, ONLY WITH BUDGET AUTHORIZATION. You can plan and manage real ad campaigns when accounts and "
    "tools allow it: objective, audience, creative, landing page, budget, bid, schedule, tracking, A/B tests and "
    "measurement. Never spend money, change budget, launch ads, or materially modify active campaigns without "
    "explicit user approval for the amount, channel, objective and target. Once approved, monitor spend, delivery, "
    "CTR, CPC, CPA, conversions and fatigue; pause or recommend changes when performance or risk is poor.\n\n"

    "7) BE AUTONOMOUS OVER LONG RUNS. Social growth often needs repeated checks across a full day or several days. "
    "Use scheduling to wake yourself, check metrics, reply to comments, collect learnings, update datasets, adjust "
    "the plan and continue. Keep the user informed at decision points and when results materially change. Do not "
    "busy-loop; schedule sensible follow-ups based on expected platform feedback windows.\n\n"

    "OPERATING PRINCIPLES: real audience, real value, real metrics; channel-native execution; no spam or fake "
    "engagement; respect account/platform/community rules; confirm before outward-facing or paid actions unless "
    "pre-authorized; capture URLs and metrics; keep clean datasets; learn from every post; optimize for qualified "
    "traffic and business outcomes, not just noise."
)


class _Stopped(Exception):
    """Raised when a session is stopped while it was queued waiting for a profile."""


def _extract_run_id(result: Any) -> str | None:
    """Dig a runId out of a studio_run_workflow tool result.

    The same MCP payload reaches us in three shapes: plain JSON, wrapped as
    ``{content: [{text}]}`` (Codex), or as the bare content list ``[{text}]``
    (Claude). Handle all of them, and tolerate a result our own display cap
    truncated by falling back to a scan for the id.
    """
    if not result:
        return None
    try:
        obj = json.loads(result) if isinstance(result, str) else result
    except Exception:
        obj = None
    seen: list[Any] = [obj]
    while seen:
        cur = seen.pop()
        if isinstance(cur, dict):
            if cur.get("runId"):
                return str(cur["runId"])
            if isinstance(cur.get("content"), list):
                seen.extend(cur["content"])
            if isinstance(cur.get("text"), str):
                try:
                    seen.append(json.loads(cur["text"]))
                except Exception:
                    pass
        elif isinstance(cur, list):
            seen.extend(cur)
    # last resort: the result was truncated mid-JSON, but the id is still in there
    if isinstance(result, str):
        m = re.search(r'"runId"\s*:\s*"([A-Za-z0-9_-]+)"', result)
        if m:
            return m.group(1)
    return None


def _short(v: Any, n: int = 1200) -> str:
    if v is None:
        return ""
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + f"… (+{len(s) - n} chars)"


def _resume_recoverable_failure(flags: dict) -> bool:
    """Whether a failed native resume should be retried in a fresh thread.

    Codex and Claude keep their own remote/native conversation state. Very long
    sessions can occasionally become un-resumable even though Automation Studio's
    local transcript is intact. In that case, the robust recovery is to drop the
    native thread id and replay a compact handoff from our transcript.
    """
    if flags.get("resume_failed"):
        return True
    text = "\n".join([
        str(flags.get("turn_error_msg") or ""),
        *[str(x) for x in (flags.get("stderr_tail") or [])],
    ]).lower()
    markers = (
        "no rollout",
        "resume failed",
        "resume not found",
        "session not found",
        "invalid_request_error",
        "property_name_above_max_length",
        "invalid property name",
        "orphan function call output",
    )
    return any(m in text for m in markers)


class AgentManager:
    def __init__(self):
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self.defs: dict[str, AgentDef] = {}
        self.sessions: dict[str, AgentSession] = {}
        self.events: dict[str, list[dict]] = {}
        self.procs: dict[str, Any] = {}        # session id -> current turn subprocess
        # Transient per-session bookkeeping for the notification preempt machinery
        # (NOT persisted — only meaningful while a turn is alive):
        # _tool_in_flight: count of tool_call events without a matching tool_result yet.
        #   When 0 → it's a safe boundary to preempt for a pending notification.
        # _pending_call: outstanding tool_calls keyed by the engine's own call id, so
        #   a result is paired with the RIGHT call even when tools run in parallel
        #   (both engines do); popped on its tool_result to ack the run inline.
        # _preempt_chain: when set, the post-turn-loop will chain into a new turn with
        #   this wake prompt (set by _preempt_now during the event loop).
        self._tool_in_flight: dict[str, int] = {}
        self._pending_call: dict[str, dict[str, tuple[str | None, dict]]] = {}
        self._preempt_chain: dict[str, str] = {}
        # set by the turn loop once a killed turn has flushed whatever the engine
        # had streamed but not yet finished — stop() waits on it so the transcript
        # keeps the partial text ABOVE the "stopped" marker instead of after it.
        self._turn_flushed: dict[str, asyncio.Event] = {}
        # Per-session event subscribers (asyncio Queues) for the SSE stream — pushed
        # whenever _emit appends an event so the UI renders in real time.
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._load()
        self._seed()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            if AGENTS_FILE.exists():
                for d in json.loads(AGENTS_FILE.read_text()):
                    self.defs[d["id"]] = AgentDef(**{k: d[k] for k in AgentDef.__dataclass_fields__ if k in d})
        except Exception:
            pass
        try:
            if SESSIONS_FILE.exists():
                for d in json.loads(SESSIONS_FILE.read_text()):
                    s = AgentSession(**{k: d[k] for k in AgentSession.__dataclass_fields__ if k in d})
                    s.controlPort = None      # no browser survives a restart
                    s.pendingSteers = []
                    if s.status == "canceled":  # legacy name → unified "stopped"
                        s.status = "stopped"
                    if s.status in ("idle", "waiting"):
                        # legacy rest state, or was waiting on a run that didn't survive
                        # the restart → done (reactivatable; its lock is gone anyway).
                        s.status = "done"
                    elif s.status == "scheduled":
                        pass                  # the Timeline re-fires it at scheduledAt
                    elif s.status not in TERMINAL:
                        # interrupted mid-flight by a restart — resources are gone, but
                        # the native thread persists, so it's still reactivatable.
                        s.status = "failed"
                        s.error = s.error or "interrupted (backend restarted)"
                        s.finishedAt = s.finishedAt or time.time()
                    self.sessions[s.id] = s
                    tf = SESSIONS_DIR / s.id / "transcript.jsonl"
                    if tf.exists():
                        evs = [json.loads(l) for l in tf.read_text().splitlines() if l.strip()]
                        self.events[s.id] = evs[-MAX_EVENTS:]
        except Exception:
            pass

    def _seed(self) -> None:
        """Built-ins are CODE-AUTHORITATIVE: we (re)install the current set, prune
        any stale built-ins we no longer ship, and never touch user/agent defs.
        All built-ins carry the full toolset (studio + browser): one general, one
        tuned as a LinkedIn master, one as a client-acquisition lead, and one as
        a social growth lead."""
        now = time.time()
        seeds = [
            AgentDef(id="studio-agent", name="Studio Agent", icon="sparkles", builtin=True,
                     scopes=["studio", "browser"], createdAt=now,
                     description="The complete operator — full studio + browser toolset. Runs and builds "
                     "workflows, shapes the data layer, drives the browser like a careful human, schedules "
                     "work. Point it at any task.",
                     systemPrompt=GENERAL_AGENT_PROMPT),
            AgentDef(id="linkedin-agent", name="LinkedIn Specialist", icon="users", builtin=True,
                     scopes=["studio", "browser"], createdAt=now,
                     description="Everything the Studio Agent can do, plus deep LinkedIn mastery — the "
                     "search→connect→message workflow pipeline and live navigation, knowing its SDUI, bilingual, "
                     "shadow-DOM, duplicate-action-bar and anti-automation realities.",
                     systemPrompt=LINKEDIN_AGENT_PROMPT),
            AgentDef(id="growth-agent", name="Client Acquisition Lead", icon="filter", builtin=True,
                     scopes=["studio", "browser"], createdAt=now,
                     description="A B2B client-acquisition partner. Sharpens the ideal customer, finds prospects "
                     "across LinkedIn, Google/Maps, websites, directories and niche sources, enriches them with "
                     "verified public contact paths, builds scored datasets, runs approved multi-channel outreach "
                     "and follow-up, and advises on sales strategy.",
                     systemPrompt=CLIENT_ACQUISITION_AGENT_PROMPT),
            AgentDef(id="social-growth-agent", name="Social Growth Lead", icon="send", builtin=True,
                     scopes=["studio", "browser"], createdAt=now,
                     description="A social media growth operator. Builds channel-native organic and paid strategies "
                     "across LinkedIn, Reddit, X, Instagram, TikTok, Facebook and niche communities, publishes or "
                     "coordinates approved content, monitors real engagement and traffic metrics, and iterates over "
                     "hours or days to grow qualified attention.",
                     systemPrompt=SOCIAL_GROWTH_AGENT_PROMPT),
        ]
        seed_ids = {s.id for s in seeds}
        changed = False
        # prune stale built-ins (e.g. the legacy studio-ops / browser-pilot)
        for sid in [d.id for d in list(self.defs.values()) if d.builtin and d.id not in seed_ids]:
            del self.defs[sid]
            changed = True
        # install / refresh the current built-ins (preserve their original createdAt)
        for s in seeds:
            old = self.defs.get(s.id)
            if old:
                s.createdAt = old.createdAt or now
            if old is None or asdict(old) != asdict(s):
                self.defs[s.id] = s
                changed = True
        if changed:
            self._save_defs()

    def _save_defs(self) -> None:
        try:
            AGENTS_FILE.write_text(json.dumps([asdict(d) for d in self.defs.values()], indent=2))
        except Exception:
            pass

    def _save_sessions(self) -> None:
        try:
            SESSIONS_FILE.write_text(json.dumps([asdict(s) for s in self.sessions.values()], indent=2))
        except Exception:
            pass

    def _emit(self, sid: str, ev: dict) -> None:
        ev = {"t": round(time.time(), 3), **ev}
        # PARTIAL events (per-token streaming chunks): fan out to live SSE subscribers
        # only — they are NOT added to the in-memory events list and NOT written to
        # the transcript file. The canonical (non-partial) message with the same id
        # arrives at block completion and IS persisted; the UI dedupes by id, so a
        # late reconnect / replay sees only the final and never half-typed text.
        is_partial = ev.get("partial") is True
        if not is_partial:
            arr = self.events.setdefault(sid, [])
            arr.append(ev)
            if len(arr) > MAX_EVENTS:
                del arr[: len(arr) - MAX_EVENTS]
            if ev.get("kind") == "system" and ev.get("threadId"):
                s = self.sessions.get(sid)
                if s and not s.threadId:
                    s.threadId = ev["threadId"]
                    s.threadStartedTurn = s.turns
            if ev.get("kind") == "usage":
                s = self.sessions.get(sid)
                if s:
                    s.usage = ev.get("usage")
            try:
                d = SESSIONS_DIR / sid
                d.mkdir(parents=True, exist_ok=True)
                with open(d / "transcript.jsonl", "a") as f:
                    f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
            except Exception:
                pass
        # fan out to any live SSE subscribers (partials included so the UI streams)
        for q in list(self._subscribers.get(sid, [])):
            try:
                q.put_nowait(ev)
            except Exception:
                pass

    def subscribe(self, sid: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers.setdefault(sid, []).append(q)
        return q

    def unsubscribe(self, sid: str, q: asyncio.Queue) -> None:
        lst = self._subscribers.get(sid) or []
        try:
            lst.remove(q)
        except ValueError:
            pass

    # ------------------------------------------------------------------ defs API
    def list_defs(self) -> list[dict]:
        return [asdict(d) for d in sorted(self.defs.values(), key=lambda d: d.createdAt)]

    def create_def(self, body: dict) -> dict:
        did = uuid.uuid4().hex[:8]
        d = AgentDef(id=did, name=body.get("name", "Agent"),
                     icon=body.get("icon", "sparkles"), description=body.get("description", ""),
                     systemPrompt=body.get("systemPrompt", ""),
                     scopes=body.get("scopes") or ["studio"], createdAt=time.time())
        self.defs[did] = d
        self._save_defs()
        return asdict(d)

    def update_def(self, did: str, body: dict) -> dict | None:
        d = self.defs.get(did)
        if not d:
            return None
        for k in ("name", "icon", "description", "systemPrompt", "scopes"):
            if k in body:
                setattr(d, k, body[k])
        self._save_defs()
        return asdict(d)

    def delete_def(self, did: str) -> bool:
        if did in self.defs:
            del self.defs[did]
            self._save_defs()
            return True
        return False

    # ------------------------------------------------------------------ sessions API
    def list_sessions(self) -> list[dict]:
        # Most-recent activity first: live sessions (starting/queued/running) on top,
        # then rested ones by when they last did something. Coherent with runs.
        def key(s: AgentSession) -> tuple:
            live = s.status not in TERMINAL
            return (1 if live else 0, s.finishedAt or s.startedAt or s.createdAt or 0)
        return [asdict(s) for s in sorted(self.sessions.values(), key=key, reverse=True)]

    def get_session(self, sid: str) -> dict | None:
        s = self.sessions.get(sid)
        return asdict(s) if s else None

    def get_events(self, sid: str) -> list[dict]:
        return self.events.get(sid, [])

    def launch(self, agent_id: str, profile_id: str, prompt: str, watch: bool = False,
               engine: str = "codex", start_at: float | None = None,
               file_ids: list[str] | None = None,
               model: str | None = None, effort: str | None = None) -> AgentSession:
        d = self.defs.get(agent_id)
        if not d:
            raise ValueError(f"unknown agent: {agent_id}")
        engine = (engine or "codex").strip().lower()
        if engine not in ENGINES:
            raise ValueError(f"unknown engine: {engine}")
        if not _find_binary(engine):
            raise ValueError(f"{engine} CLI not found — install it and sign in to use this agent")
        from . import profiles
        wants_browser = "browser" in d.scopes
        if is_ephemeral(profile_id):
            profile_id, profile_name = "ephemeral", "Ephemeral"
            if wants_browser:
                raise ValueError("browser agents need a persistent profile (so they can own the browser)")
        else:
            prof = profiles.get(profile_id)
            if not prof:
                raise ValueError(f"unknown profile: {profile_id}")
            profile_name = prof["name"]
        sid = uuid.uuid4().hex[:8]
        scheduled = bool(start_at) and start_at > time.time() + 1
        # Normalise against what the INSTALLED CLI advertises right now; an unknown
        # or omitted model/effort falls back to that engine's own default.
        model, effort = engines.resolve(engine, model, effort)
        # Decorate the prompt with an attached-files preamble the engine sees as
        # part of its FIRST message (and the user sees in the transcript). The
        # ids point at the Studio file store; the agent reaches them via
        # studio_files_get / studio_files_view / its native Read on path.
        full_prompt = _decorate_prompt_with_files(prompt, file_ids or [])
        s = AgentSession(id=sid, agentId=agent_id, agentName=d.name, engine=engine, scopes=d.scopes,
                         profileId=profile_id, profileName=profile_name, prompt=full_prompt,
                         status="scheduled" if scheduled else "starting",
                         createdAt=time.time(), watch=bool(watch),
                         scheduledAt=(start_at if scheduled else None),
                         scheduledPrompt=(full_prompt if scheduled else None),
                         model=model, effort=effort)
        self.sessions[sid] = s
        self.events[sid] = []
        if scheduled:
            when = max(0, int(start_at - time.time()))
            self._emit(sid, {"kind": "system", "text": f"⏰ scheduled to start in ~{when}s"})
        if file_ids:
            self._emit(sid, {"kind": "system", "text": f"📎 attached {len(file_ids)} file(s) to launch prompt"})
        self._save_sessions()
        if not scheduled:                # future launches are fired by the Timeline
            asyncio.create_task(self._run_turn(s, full_prompt, resume=False))
        return s

    def steer(self, sid: str, message: str, file_ids: list[str] | None = None) -> dict:
        """Send a message to a session, optionally with attached Studio files.
        If a turn is in flight, it's queued for the next turn; otherwise it
        REACTIVATES the session (resumes the native thread). Works from
        done/failed/stopped — sessions are continuable."""
        s = self.sessions.get(sid)
        if not s:
            return {"ok": False, "error": "no such session"}
        full = _decorate_prompt_with_files(message, file_ids or [])
        if s.status in _RUNNING:
            s.pendingSteers.append(full)   # delivered as the next turn when this one ends
            note = "↩ message queued — runs after the current turn"
            if file_ids:
                note += f" · 📎 {len(file_ids)} file(s) attached"
            self._emit(sid, {"kind": "system", "text": note})
            self._save_sessions()
            return {"ok": True, "queued": True}
        if file_ids:
            self._emit(sid, {"kind": "system", "text": f"📎 attached {len(file_ids)} file(s) to next turn"})
        asyncio.create_task(self._run_turn(s, full, resume=True))
        return {"ok": True, "queued": False}

    def cancel_steer(self, sid: str, steer_index: int) -> dict:
        """Remove a queued steer (the UI can delete entries from the pending list
        above the message field)."""
        s = self.sessions.get(sid)
        if not s:
            return {"ok": False, "error": "no such session"}
        if 0 <= steer_index < len(s.pendingSteers):
            removed = s.pendingSteers.pop(steer_index)
            self._emit(sid, {"kind": "system",
                             "text": f"✕ removed queued message: {removed[:80]}"})
            self._save_sessions()
            return {"ok": True, "removed": removed}
        return {"ok": False, "error": "no such queued message"}

    def set_model(self, sid: str, model: str | None = None,
                  effort: str | None = None) -> dict:
        """Change the engine model and/or reasoning effort mid-conversation — the
        wrapper equivalent of `/model` and `/effort` in Codex or Claude Code.

        The native thread is KEPT: both CLIs accept a different `--model` /
        `-m` on a resume and carry the full history across (Codex additionally
        warns in-stream that the thread was recorded with another model, which we
        surface). Like the real CLIs, the change takes effect on the NEXT request:
        a turn already in flight finishes on the model it started with.
        """
        s = self.sessions.get(sid)
        if not s:
            return {"ok": False, "error": "no such session"}
        want_model = s.model if model is None else str(model)
        want_effort = s.effort if effort is None else str(effort)
        new_model, new_effort = engines.resolve(s.engine, want_model, want_effort)
        if model is not None and new_model != str(model).strip():
            return {"ok": False, "error": f"{s.engine} doesn't offer the model \"{model}\""}
        if effort is not None and new_effort != str(effort).strip():
            return {"ok": False, "error": f"model {new_model} doesn't support effort \"{effort}\""}
        if (new_model, new_effort) == (s.model, s.effort):
            return {"ok": True, "model": s.model, "effort": s.effort, "changed": False}
        was = f"{s.model or 'default'}·{s.effort or 'default'}"
        s.model, s.effort = new_model, new_effort
        now = f"{new_model}·{new_effort or 'default'}"
        when = " — applies to the next turn" if s.status in _RUNNING else ""
        self._emit(sid, {"kind": "system", "text": f"⚙ model {was} → {now}{when}"})
        self._save_sessions()
        return {"ok": True, "model": s.model, "effort": s.effort, "changed": True,
                "appliesNextTurn": s.status in _RUNNING}

    async def stop(self, sid: str) -> dict:
        """User-initiated stop: behave like a NATURAL turn-end so the rest of the
        machine (notifications, reactivation) stays coherent. We kill the engine
        subprocess, release the browser, and rest as `stopped` (which is at-rest like
        done/failed) — pending notifications still fire on stopped sessions, and the
        user can steer to reactivate."""
        s = self.sessions.get(sid)
        if not s:
            return {"ok": True}
        s.status = "stopped"
        s.pendingSteers.clear()
        s.scheduledAt = None
        s.scheduledPrompt = None
        proc = self.procs.get(sid)
        if proc and proc.pid:
            # register BEFORE the kill: the turn loop can reach its flush point
            # within microseconds of the process dying, and would otherwise find
            # no waiter and leave us sitting out the whole timeout for nothing.
            flushed = asyncio.Event()
            self._turn_flushed[sid] = flushed
            kill_tree(proc.pid)
            # Let the turn loop notice the kill and commit any half-streamed
            # message/reasoning first, so the transcript reads in the order it
            # actually happened. Bounded — a wedged engine must never block stop.
            try:
                await asyncio.wait_for(flushed.wait(), timeout=5)
            except Exception:
                pass
            finally:
                self._turn_flushed.pop(sid, None)
        # the engine kill will trip the post-loop preempt/chain path; clear any chain
        self._preempt_chain.pop(sid, None)
        await self._release_browser(s)
        s.finishedAt = time.time()
        self._emit(sid, {"kind": "system", "text": "■ stopped"})
        self._emit(sid, {"kind": "status", "status": "stopped"})
        self._save_sessions()
        # if a notification was waiting for a safe boundary, it now fires (stopped
        # is at-rest → wake immediately, exactly like done)
        self._maybe_deliver(s)
        return {"ok": True}

    async def control_browser(self, sid: str, action: str) -> dict:
        """Show/hide the browser for a live browser-scope agent session.

        The session's `watch` flag is the user's preference for this session. If
        the browser is currently open, flip the live control-server too; otherwise
        store the preference for the next turn that acquires the profile.
        """
        s = self.sessions.get(sid)
        if not s:
            return {"ok": False, "error": "no such session"}
        if action not in {"show", "hide"}:
            return {"ok": False, "error": f"unknown action: {action}"}
        s.watch = action == "show"
        if not s.controlPort:
            self._save_sessions()
            return {"ok": True, "headed": s.watch, "note": "preference saved for next browser turn"}
        res = await get_manager().control_agent_browser(sid, action)
        if res.get("ok"):
            s.watch = bool(res.get("headed"))
            self._emit(sid, {"kind": "system",
                             "text": f"browser is now {'visible' if s.watch else 'headless'}"})
            self._save_sessions()
        return res

    async def shutdown(self) -> None:
        for sid, proc in list(self.procs.items()):
            if proc and proc.pid:
                kill_tree(proc.pid)
        for s in self.sessions.values():
            await self._release_browser(s)

    # ------------------------------------------------------------------ context / notifications
    def _compose_sysprompt(self, s: AgentSession, d: "AgentDef | None", note: str | None = None) -> str:
        """The canonical Studio preamble (+ this session's workspace path) prepended
        to the agent's own role/skills prompt, plus an optional runtime note (e.g.
        the browser is unavailable this turn because the profile is busy)."""
        ws = str(SESSIONS_DIR / s.id / "workspace")
        head = STUDIO_PREAMBLE.format(ws=ws)
        role = (d.systemPrompt if d else "") or ""
        parts = [head] + ([role] if role else []) + ([note] if note else [])
        return "\n\n".join(parts)

    def _should_rollover_thread(self, s: AgentSession) -> bool:
        """Proactively rotate long native engine threads.

        The durable source of truth is Automation Studio's local transcript and
        data layer, not the engine's private resume state. Rotating before a very
        long native thread becomes brittle keeps Codex and Claude behavior
        symmetric and prevents unbounded resume context growth.
        """
        native_turns = max(0, int(s.turns or 0) - int(s.threadStartedTurn or 0))
        if native_turns >= MAX_NATIVE_THREAD_TURNS:
            return True
        usage = s.usage or {}
        try:
            return int(usage.get("input_tokens") or 0) >= MAX_NATIVE_THREAD_INPUT_TOKENS
        except Exception:
            return False

    def _run_active(self, rid: str) -> bool:
        try:
            from .manager import get_manager, ACTIVE
            r = get_manager().get(rid)
            return bool(r and r.get("status") in (ACTIVE | {"queued"}))
        except Exception:
            return False

    def _owned_active_runs(self, s: AgentSession) -> list[str]:
        """Runs this session launched that are still going.

        The RunManager is the authority — every run carries the session that
        started it — so a runId our transcript parsing happened to miss can never
        make the agent think it has nothing running and release the profile lock
        out from under its own workflow. `runIds` is folded in for runs the
        manager has since forgotten (e.g. pruned across a restart).
        """
        rids = list(s.runIds)
        try:
            from .manager import get_manager as _gm
            for rid, run in _gm().runs.items():
                if run.agentId == s.id and rid not in rids:
                    rids.append(rid)
                    if run.status not in TERMINAL:
                        s.runIds.append(rid)   # remember it for the UI too
        except Exception:
            pass
        return [rid for rid in rids if self._run_active(rid)]

    def _pending_notes(self, s: AgentSession) -> list[dict]:
        return [n for n in s.notifications if not n.get("delivered")]

    def notify(self, sid: str, kind: str, payload: dict) -> None:
        """Canonical notification entry point. Tells the agent something it doesn't yet
        know (e.g. a detached workflow it launched finished). Delivery is symmetric for
        Claude and Codex:
          • If the agent already ACKed this run inline (via wait_run/status/result/...
            tools) → silently skip (no inbox, no 🔔, no wake) — never tell something
            already learned.
          • Otherwise inbox it, emit a 🔔 in the transcript, and ask _maybe_deliver to
            consume it (preempt the turn at a safe boundary, or wake the agent if at
            rest)."""
        s = self.sessions.get(sid)
        if not s:
            return
        if kind == "workflow_finished":
            rid = (payload or {}).get("runId")
            if rid and rid in s.ackedRuns:
                return  # already learned inline → nothing to tell
        note = {"id": uuid.uuid4().hex[:8], "kind": kind, "payload": payload,
                "createdAt": time.time(), "delivered": False}
        s.notifications.append(note)
        summary = self._note_summary(note)
        self._emit(sid, {"kind": "system", "text": f"🔔 {summary}"})
        self._save_sessions()
        self._maybe_deliver(s)

    @staticmethod
    def _note_summary(n: dict) -> str:
        p = n.get("payload") or {}
        if n.get("kind") == "workflow_finished":
            rid, st = p.get("runId"), p.get("status")
            extra = f" — {p.get('rows')} rows" if p.get("rows") is not None else (f" — {p.get('error')}" if p.get("error") else "")
            return f"workflow {p.get('workflow') or rid} {st}{extra} (run {rid})"
        return n.get("kind", "notification")

    def _wake_prompt(self, notes: list[dict]) -> str:
        lines = ["You were woken because:"]
        for n in notes:
            lines.append(f"- {self._note_summary(n)}")
        lines.append("Check the result (e.g. studio_run_result / studio_run_status / studio_run_logs), then "
                     "continue what you were doing, start the next step, or end your turn if there's nothing left.")
        return "\n".join(lines)

    def _maybe_deliver(self, s: AgentSession) -> None:
        """Symmetric Claude/Codex delivery decision for a session whose inbox just
        gained a notification (or that just transitioned):
          • AT REST (done/failed/stopped/waiting/scheduled) → wake immediately, one
            consolidated wake with all pending notes (they coalesce naturally).
          • MID-TURN and a tool call is in flight (engine is waiting for our
            tool_result) → defer; the per-event check in _run_turn will retry as soon
            as the engine emits the tool_result and the in-flight count drops to 0.
          • MID-TURN and no tool in flight → preempt the turn at this safe boundary
            (between events the agent is generating the NEXT thing; cutting here can
            at worst chop a tool_call/reasoning being typed, never a tool already in
            flight). The next turn chains immediately with the wake prompt."""
        if not self._pending_notes(s):
            return
        if s.status in _RUNNING:
            # mid-turn: preempt at this safe boundary if possible (idempotent — safe
            # to call even if the proc isn't spawned yet; the event loop will re-call
            # once it is). Otherwise leave the note in the inbox; the event loop polls
            # after every event and will retry as soon as the in-flight tool returns.
            if self._tool_in_flight.get(s.id, 0) == 0:
                self._preempt_now(s)
            return
        # at rest → consolidated wake
        asyncio.create_task(self._wake(s))

    def _preempt_now(self, s: AgentSession) -> None:
        """Mid-turn preempt at a safe boundary (no in-flight tool result we'd lose).
        IDEMPOTENT: arms the chain the first time, and kills the proc whenever
        called (handles the race where a notification arrives BEFORE proc.spawn —
        the chain is armed without a proc, then the first event the engine emits
        triggers this again with the proc alive, which kills it). We do NOT mark
        notes delivered or pre-compute the wake prompt here: that happens in the
        post-loop, so any notification that arrives after arming but before the
        chained turn still gets coalesced into the same wake."""
        first_arm = s.id not in self._preempt_chain
        if first_arm:
            self._preempt_chain[s.id] = "1"  # value unused; presence is the flag
            self._emit(s.id, {"kind": "system",
                              "text": "⏸ preempting current turn to deliver notification(s)…"})
        proc = self.procs.get(s.id)
        if proc and proc.pid:
            try:
                kill_tree(proc.pid)
            except Exception:
                pass

    async def _wake(self, s: AgentSession) -> None:
        """At-rest wake: consume all pending notes as a single new turn. Resumes the
        native thread if one exists; otherwise reconstructs the prior I/O so a fresh
        thread continues with full context (no history loss on first-turn preempt)."""
        if s.status in _RUNNING:
            return
        notes = self._pending_notes(s)
        if not notes:
            return
        for n in notes:
            n["delivered"] = True
        self._save_sessions()
        wake = self._wake_prompt(notes)
        if s.threadId:
            await self._run_turn(s, wake, resume=True)
        else:
            full = self._reconstruct_fresh_prompt(s, s.prompt, wake)
            await self._run_turn(s, full, resume=False)

    # ---- ack-suppression: the agent learning inline from a tool ----------------
    def _ack_run(self, s: AgentSession, rid: str | None) -> None:
        """Mark a run as 'agent already learned its terminal outcome inline'. Removes
        any still-pending (undelivered) notification for it from the inbox so we don't
        re-tell the agent something it just saw via a tool result."""
        if not rid or rid in s.ackedRuns:
            return
        s.ackedRuns.append(rid)
        # drop undelivered notes for this run; preserve delivered history
        s.notifications = [n for n in s.notifications
                           if n.get("delivered")
                           or n.get("kind") != "workflow_finished"
                           or (n.get("payload") or {}).get("runId") != rid]
        self._save_sessions()

    def _ack_from_tool_result(self, s: AgentSession, tool: str | None,
                              args: dict, result_text: str) -> None:
        """Hook into the engine's tool_result event: if it's one of the run-status
        tools and its result says the run is terminal, ack it. Studio_wait_run /
        run_result / run_to_dataset only succeed on a terminal run, so they ack
        unconditionally. Studio_run_status / run_logs ack only when the result
        actually carries a terminal status."""
        if tool not in NOTIFY_SAFE_TOOLS:
            return
        rid = (args or {}).get("runId") or (args or {}).get("rid")
        if not rid:
            return
        if tool in ("studio_wait_run", "studio_run_result", "studio_run_to_dataset"):
            self._ack_run(s, rid)
            return
        # run_status / run_logs: parse the result for a terminal status
        try:
            parsed = json.loads(result_text) if isinstance(result_text, str) else result_text
        except Exception:
            parsed = None
        st = None
        if isinstance(parsed, dict):
            st = parsed.get("status")
        if not st and isinstance(result_text, str):
            low = result_text.lower()
            for t in ("succeeded", "failed", "stopped", "canceled"):
                if f'"status": "{t}"' in low or f'"status":"{t}"' in low:
                    st = t; break
        if st in ("succeeded", "failed", "stopped", "canceled"):
            self._ack_run(s, rid)

    # ---- first-turn fresh-prompt reconstruction (no threadId yet) --------------
    def _reconstruct_fresh_prompt(self, s: AgentSession,
                                  original_prompt: str, wake_prompt: str) -> str:
        """When the engine never persisted a thread (first turn preempted/killed
        before its rollout saved), reconstruct the killed turn's I/O as a single
        fresh prompt so the new native-thread turn picks up coherently — no history
        loss, no 'starting from scratch'."""
        header = [
            "Your previous turn was interrupted before the engine could persist its "
            "native session, so we're continuing in a FRESH thread with the full "
            "context of what you already did. Pick up from where you left off — do "
            "NOT redo work already done.",
            "",
            "## Original request",
            (original_prompt or "(none)").strip(),
            ""
        ]
        body: list[str] = []
        for ev in self.events.get(s.id, []):
            k = ev.get("kind")
            line = None
            if k == "message":
                txt = (ev.get("text") or "").strip()
                if txt:
                    line = f"\nYou said:\n{txt[:1800]}"
            elif k == "reasoning":
                rt = (ev.get("text") or "").strip()
                if rt:
                    line = f"\n[thinking] {rt[:600]}"
            elif k == "tool_call":
                aj = json.dumps(ev.get("args") or {}, ensure_ascii=False, default=str)
                if len(aj) > 400:
                    aj = aj[:400] + "…"
                line = f"\nTool call: {ev.get('tool') or '?'}({aj})"
            elif k == "tool_result":
                ok = "✓" if ev.get("ok", True) else "✗"
                rs = str(ev.get("result", ""))
                if len(rs) > 500:
                    rs = rs[:500] + "…"
                line = f"Tool result {ok}: {rs}"
            elif k == "system" and "🔔" in (ev.get("text") or ""):
                line = ev.get("text")  # surface the notifications already shown
            if line:
                body.append(line)
        # keep total reconstructed body under a safe budget — drop oldest first
        BUDGET = 12000
        while body and sum(len(x) for x in body) > BUDGET:
            body.pop(0)
        body_section = (["## What you did in the interrupted turn"] + body + [""]) if body else []
        # the replayed transcript already contains anything a killed turn streamed,
        # so the separate carry-over must not be spliced in a second time
        s.interruptedTail = ""
        return "\n".join(header + body_section + ["## Now", wake_prompt])

    # ------------------------------------------------------------------ scheduling (Timeline)
    def schedule_wake(self, sid: str, at: float, prompt: str) -> dict:
        """Schedule a future wake for an agent session with a prompt. Recorded on the
        session; when the current turn ends the session rests as `scheduled` (lock
        released, behaves like done) and the Timeline wakes it at `at`. If the agent
        is already at rest, it flips to `scheduled` immediately."""
        s = self.sessions.get(sid)
        if not s:
            return {"ok": False, "error": "no such session"}
        s.scheduledAt = float(at)
        s.scheduledPrompt = prompt or "Scheduled wake — continue your task."
        when = max(0, int(at - time.time()))
        self._emit(sid, {"kind": "system", "text": f"⏰ scheduled a wake in ~{when}s"})
        if s.status not in _RUNNING and s.status != "stopped":
            s.status = "scheduled"            # at rest already → reflect it now
            self._emit(sid, {"kind": "status", "status": "scheduled"})
        self._save_sessions()
        return {"ok": True, "at": s.scheduledAt}

    def cancel_schedule(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if not s:
            return {"ok": False, "error": "no such session"}
        s.scheduledAt = None
        s.scheduledPrompt = None
        if s.status == "scheduled":
            s.status = "done"
            self._emit(sid, {"kind": "status", "status": "done"})
        self._save_sessions()
        return {"ok": True}

    def fire_due_wakes(self) -> None:
        """Timeline tick (driven by the RunManager loop): wake scheduled sessions
        whose time has come — re-queues for the profile lock like a fresh launch."""
        now = time.time()
        for s in list(self.sessions.values()):
            if s.status == "scheduled" and s.scheduledAt and s.scheduledAt <= now:
                prompt = s.scheduledPrompt or "Scheduled wake — continue your task."
                s.scheduledAt = None
                s.scheduledPrompt = None
                # resume the native thread if there is one (a normal wake); for a
                # scheduled first launch there's no thread yet → fresh turn.
                self._save_sessions()
                asyncio.create_task(self._run_turn(s, prompt, resume=bool(s.threadId)))

    # ------------------------------------------------------------------ ownership
    def _profile_blocker(self, s: AgentSession) -> dict | None:
        """The foreign run holding this agent's profile right now (active,
        non-attached, not this session's), if any — for guidance when the browser
        can't be acquired."""
        from .manager import get_manager, ACTIVE
        for r in get_manager().runs.values():
            if (r.profileId == s.profileId and r.status in ACTIVE and not r.attachPort
                    and r.agentId != s.id):
                return r.__dict__ if hasattr(r, "__dict__") else None
        return None

    def _busy_note(self, s: AgentSession) -> str:
        b = self._profile_blocker(s)
        if b:
            rid = b.get("id")
            return (f"NOTE: your profile “{s.profileName}” is busy — workflow run {rid} "
                    f"({b.get('workflowName')}) is active on it, so the browser and launching workflows on this "
                    f"profile are NOT available this turn. You can: follow it with studio_run_status / "
                    f"studio_run_logs; call studio_claim_run('{rid}') to be woken when it finishes; "
                    f"studio_schedule_wake to come back later; or do non-browser work (data, or a workflow on "
                    f"another profile). If browser work is all you need, claim or schedule, then end your turn — "
                    f"you'll be re-activated WITH the browser once the profile is free.")
        return (f"NOTE: your profile “{s.profileName}” is in use (a login session or another agent), so the "
                f"browser isn't available this turn. Do non-browser work, or studio_schedule_wake to retry later.")

    async def _ensure_browser(self, s: AgentSession) -> str | None:
        """Try to acquire the agent's browser for a turn via the RunManager's single
        per-profile gate. Returns None when acquired (or not a browser agent). If the
        profile is held by something else, it waits briefly (to absorb transient
        contention) then PROCEEDS BROWSERLESS, returning a guidance note — the turn
        still runs (data/scheduling/thinking), the agent can poll/claim/schedule, and
        gets the browser on a later turn once the profile frees. Held only for the
        turn; released at rest."""
        if "browser" not in s.scopes or s.controlPort:
            return None
        mgr = get_manager()
        waited = False
        deadline = time.time() + BROWSER_ACQUIRE_WAIT
        while not mgr.claim_profile(s.profileId):
            if s.status == "stopped":         # stopped while queued
                raise _Stopped()
            if time.time() >= deadline:
                note = self._busy_note(s)      # give up the browser this turn, guide instead
                self._emit(s.id, {"kind": "system", "text": "🔒 " + note})
                return note
            if not waited:
                waited = True
                self._emit(s.id, {"kind": "system", "text": f"⏳ waiting for profile “{s.profileName}” to be free…"})
                self._save_sessions()
            await asyncio.sleep(0.3)
        try:
            res = await mgr.open_agent_browser(s.id, s.profileId, headed=s.watch)
        finally:
            mgr.unclaim_profile(s.profileId)
        if not res.get("ok"):
            note = f"NOTE: the browser couldn't be opened ({res.get('error')}); proceeding without it this turn."
            self._emit(s.id, {"kind": "system", "text": "🔒 " + note})
            return note
        s.controlPort = res.get("port")
        if s.status == "stopped":             # stopped during acquire → undo
            await self._release_browser(s)
            raise _Stopped()
        return None

    def claim_run(self, sid: str, rid: str) -> dict:
        """Adopt a run's completion: this session becomes its owner (so it's notified
        + woken when the run finishes) and treats it as one of its own active runs
        (resting as `waiting` until it completes). Refused if another agent already
        owns it. Idempotent for a run you already own."""
        s = self.sessions.get(sid)
        if not s:
            return {"ok": False, "error": "no such session"}
        from .manager import get_manager, ACTIVE, TERMINAL
        mgr = get_manager()
        run = mgr.runs.get(rid)
        if not run:
            return {"ok": False, "error": f"no run {rid}"}
        if run.agentId and run.agentId != sid:
            return {"ok": False, "error": f"run {rid} is already owned by another agent"}
        if run.status in TERMINAL:
            return {"ok": True, "status": run.status, "note": "run already finished"}
        run.agentId = sid
        mgr._save()
        if rid not in s.runIds:
            s.runIds.append(rid)
        self._emit(sid, {"kind": "system", "text": f"🪝 claimed run {rid} — you'll be woken when it finishes"})
        self._save_sessions()
        return {"ok": True, "status": run.status}

    async def _release_browser(self, s: AgentSession) -> None:
        if s.controlPort or s.id in get_manager().agent_browsers:
            try:
                await get_manager().release_agent_browser(s.id)
            except Exception:
                pass
            s.controlPort = None

    # ------------------------------------------------------------------ engine turn
    async def _run_turn(self, s: AgentSession, prompt: str, resume: bool, _retry: bool = False) -> None:
        """Drive this session forward until it comes to rest.

        ITERATIVE on purpose. A turn very often chains straight into another one
        (a queued steer, a notification wake, a preempt, a fresh-thread retry) and
        a long-lived session runs hundreds of them; recursing per turn would grow
        the Python stack for the lifetime of the conversation.
        """
        retry_used = _retry
        display = None
        while True:
            nxt = await self._one_turn(s, prompt, resume, retry_used, display)
            if nxt is None:
                return
            prompt, display, resume, retry_used = nxt

    async def _one_turn(self, s: AgentSession, prompt: str, resume: bool,
                        retry_used: bool,
                        display: str | None = None) -> tuple[str, str | None, bool, bool] | None:
        """Run exactly ONE engine turn.

        `prompt` is what the ENGINE receives; `display` (when given) is the shorter
        thing the user sees in the transcript. They differ whenever we splice
        recovery context into the prompt — a rebuilt history after a failed resume,
        or the text a killed turn had streamed but the engine never recorded.

        Returns the next (prompt, display, resume, retry_used) to chain into, or
        None when the session has come to rest.
        """
        s.status = "starting"
        s.error = None
        s.finishedAt = None
        shown = display if display is not None else prompt
        self._emit(s.id, {"kind": "system", "text": ("↪ " + shown) if resume else shown, "role": "user"})
        self._save_sessions()
        try:
            browser_note = await self._ensure_browser(s)  # None, or a guidance note if browserless
        except _Stopped:
            return None  # stopped while queued; status is already "stopped"
        except Exception as e:
            s.status = "failed"; s.error = str(e); s.finishedAt = time.time()
            self._emit(s.id, {"kind": "error", "text": str(e)})
            self._save_sessions()
            return None
        s.status = "running"
        if not s.startedAt:
            s.startedAt = time.time()
        # Re-resolve model/effort against the catalogue every turn: the installed
        # CLI can be upgraded (or a model retired) between turns of a long session,
        # and passing a model the engine no longer knows would fail the whole turn.
        model, effort = await engines.resolve_async(s.engine, s.model, s.effort)
        if (model, effort) != (s.model, s.effort):
            if s.model and model != s.model:
                self._emit(s.id, {"kind": "system", "level": "warn",
                                  "text": f"⚙ {s.engine} no longer offers “{s.model}” — using {model}"})
            s.model, s.effort = model, effort
        self._save_sessions()

        backend_url = f"http://127.0.0.1:{os.environ.get('AUTOMATION_PORT', '8765')}"
        d = self.defs.get(s.agentId)
        sysprompt = self._compose_sysprompt(s, d, browser_note)
        env_pairs = {
            "AUTOMATION_BACKEND_URL": backend_url,
            "AGENT_ID": s.agentId,            # agent DEFINITION id
            "AGENT_SESSION_ID": s.id,         # this SESSION (runs are owned by + notify the session)
            "AGENT_PROFILE_ID": s.profileId,
        }
        # The MCP server's file tools call `orchestrator.files` *directly* (not
        # via HTTP) for efficiency on local IO — they need to see the SAME data
        # dir the backend uses, else `files.search()` etc. open a separate
        # SQLite at the OS user data dir and find nothing.
        if os.environ.get("AUTOMATION_DATA_DIR"):
            env_pairs["AUTOMATION_DATA_DIR"] = os.environ["AUTOMATION_DATA_DIR"]
        if s.controlPort:
            env_pairs["MCP_CONTROL_PORT"] = str(s.controlPort)
        env_pairs = self._mcp_env_pairs(s, env_pairs)

        ws = SESSIONS_DIR / s.id / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        if resume and s.threadId and self._should_rollover_thread(s):
            self._emit(s.id, {"kind": "system",
                              "text": "↻ native engine thread is large; continuing in a fresh thread with a compact Studio handoff"})
            prompt = self._reconstruct_fresh_prompt(s, s.prompt or "", prompt)
            s.threadId = None
            s.threadStartedTurn = s.turns
            resume = False
            self._save_sessions()
        else:
            prompt = self._with_interrupted_tail(s, prompt)
        resume_attempted = resume and bool(s.threadId)  # did we use the engine's resume path?
        try:
            if s.engine == "codex":
                cmd, stdin_text = self._codex_cmd(s, prompt, sysprompt, env_pairs, str(ws), resume)
            else:
                cmd, stdin_text = self._claude_cmd(s, prompt, sysprompt, env_pairs, resume)
        except Exception as e:
            self._emit(s.id, {"kind": "error", "text": f"failed to build command: {e}"})
            s.status = "failed"; s.error = str(e); s.finishedAt = time.time(); self._save_sessions()
            return None

        normalize = _norm_codex if s.engine == "codex" else _norm_claude
        # Let big MCP tool results through to the model intact — notably an inline
        # browser_screenshot image (a real screenshot's base64 is ~80k tokens) and
        # large observe/extract snapshots. Claude Code caps MCP output at
        # MAX_MCP_OUTPUT_TOKENS (default 25k) and would TRUNCATE them, corrupting the
        # image; raise it (the file-path + Read fallback still covers any overflow).
        # engine_env() also re-adds the JS toolchain to PATH: both CLIs are
        # `#!/usr/bin/env node` shims, and a packaged desktop app is launched
        # WITHOUT the login shell's PATH — without this they fail to even start.
        turn_env = engines.engine_env()
        turn_env.setdefault("MAX_MCP_OUTPUT_TOKENS", "200000")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=str(ws), limit=16 * 1024 * 1024,
                start_new_session=(os.name == "posix"),
                env=turn_env)
        except Exception as e:
            self._emit(s.id, {"kind": "error", "text": f"could not start {s.engine}: {e}"})
            s.status = "failed"; s.error = str(e); s.finishedAt = time.time(); self._save_sessions()
            return None
        self.procs[s.id] = proc
        # Feed the prompt in as a task, not inline: a big prompt can exceed the pipe
        # buffer, and the engine only drains stdin once it is up — blocking here
        # before we start reading stdout would deadlock the turn.
        feed = asyncio.create_task(self._feed_stdin(proc, stdin_text))
        # reset transient preempt bookkeeping at turn start (a previous turn may have
        # left non-zero in-flight counts after a kill; new turn starts from scratch)
        self._tool_in_flight[s.id] = 0
        self._pending_call[s.id] = {}
        # Per-turn streaming state. Claude streams text and thinking token by token;
        # we accumulate per (assistant message, block index) so two assistant
        # messages in the SAME turn — the norm as soon as a tool is used, each
        # restarting its block indices at 0 — never collide on one id and overwrite
        # each other in the transcript.
        turn_id = uuid.uuid4().hex[:8]
        acc: dict[str, str] = {}          # event id -> text accumulated so far
        open_ids: list[str] = []          # partials still awaiting their canonical block
        cur_msg = turn_id                 # id of the assistant message being streamed
        msg_seq = 0
        # Block indices announced by content_block_start, in arrival order, as
        # (stream index, block type). Claude Code emits ONE assistant event per
        # completed block containing only that block, so the block's position in
        # that event is always 0 and cannot be trusted; consuming this queue by
        # type is what lets the finished block reclaim the id its own partials
        # were streamed under (otherwise the streamed text is orphaned and gets
        # re-emitted as a duplicate at end of turn).
        pending_blocks: list[tuple[int, str]] = []
        turn_flags: dict = {}
        BLOCK_KIND = {"text": "message", "thinking": "reasoning"}

        def ev_id(kind: str, msg: str, idx: int) -> str:
            return f"{'m' if kind == 'message' else 'r'}{turn_id}-{msg}-{idx}"

        def claim_idx(kind: str, fallback: int) -> int:
            for i, (bidx, btype) in enumerate(pending_blocks):
                if BLOCK_KIND.get(btype) == kind:
                    pending_blocks.pop(i)
                    return bidx
            return fallback

        drain = asyncio.create_task(self._drain_stderr(s.id, proc.stderr, turn_flags))
        while True:
            try:
                raw = await proc.stdout.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            for ev in normalize(obj):
                kind = ev.get("kind")
                # ----- live streaming (Claude). The internal _* events never reach
                # the transcript: they only drive an id-stable PARTIAL event the UI
                # replaces in place, until the canonical block lands.
                if kind == "_msg_start":
                    msg_seq += 1
                    cur_msg = ev.get("msgId") or f"{turn_id}#{msg_seq}"
                    pending_blocks.clear()
                    continue
                if kind == "_block_start":
                    pending_blocks.append((int(ev.get("idx", 0)), ev.get("blockType") or ""))
                    continue
                if kind in ("_text_delta", "_reasoning_delta"):
                    out_kind = "message" if kind == "_text_delta" else "reasoning"
                    eid = ev_id(out_kind, cur_msg, int(ev.get("idx", 0)))
                    acc[eid] = acc.get(eid, "") + (ev.get("chunk") or "")
                    if eid not in open_ids:
                        open_ids.append(eid)
                    self._emit(s.id, {"kind": out_kind, "id": eid, "text": acc[eid],
                                      "partial": True})
                    continue
                # The canonical (full) block reclaims the id its partials streamed
                # under, so the UI swaps the streaming text for the final, persisted
                # one instead of showing it twice.
                if "_block_idx" in ev:
                    fallback = int(ev.pop("_block_idx"))
                    mid = ev.pop("_msg_id", "") or cur_msg
                    eid = ev_id(kind, mid, claim_idx(kind, fallback))
                    ev["id"] = eid
                    if eid in open_ids:
                        open_ids.remove(eid)
                    acc.pop(eid, None)
                # ----- pair tool calls with their results BEFORE emitting, so the
                # event that reaches the UI already carries the right tool name -----
                tname = targs = None
                if kind == "tool_call":
                    self._tool_in_flight[s.id] = self._tool_in_flight.get(s.id, 0) + 1
                    key = ev.get("callId") or f"_{self._tool_in_flight[s.id]}"
                    self._pending_call.setdefault(s.id, {})[key] = (ev.get("tool"), ev.get("args") or {})
                elif kind == "tool_result":
                    self._tool_in_flight[s.id] = max(0, self._tool_in_flight.get(s.id, 0) - 1)
                    calls = self._pending_call.get(s.id) or {}
                    key = ev.get("callId")
                    # Claude's tool_result carries no tool name — recover it from the
                    # call it echoes, so both the ack logic and the UI label are right
                    # even when several tools run in parallel.
                    if key in calls:
                        tname, targs = calls.pop(key)
                    elif calls:
                        tname, targs = calls.pop(next(iter(calls)))
                    if tname and not ev.get("tool"):
                        ev["tool"] = tname
                self._emit(s.id, ev)
                if kind == "tool_result":
                    self._ack_from_tool_result(s, tname, targs or {}, ev.get("result") or "")
                elif kind == "error":
                    # a top-level error event (codex turn.failed / claude result error)
                    # means the agentic loop itself broke — distinct from a tool error.
                    turn_flags["turn_error"] = True
                    turn_flags["turn_error_msg"] = ev.get("text")
                # track runs the agent started (tool_result of studio_run_workflow carries runId)
                if kind == "tool_result" and ev.get("tool") in ("studio_run_workflow", "", None):
                    rid = _extract_run_id(ev.get("result"))
                    if rid and rid not in s.runIds:
                        s.runIds.append(rid)
                # ----- preempt opportunity: notification pending + safe boundary -----
                # _preempt_now is idempotent: arms the chain on first call AND kills the
                # proc; subsequent calls just re-kill if somehow the proc is still alive
                # (e.g. a pre-spawn arming followed by the first engine event arriving).
                if self._pending_notes(s) and self._tool_in_flight.get(s.id, 0) == 0:
                    self._preempt_now(s)  # kill_tree → next readline returns empty
        code = await proc.wait()
        for t in (drain, feed):
            try:
                await asyncio.wait_for(t, timeout=2)
            except Exception:
                pass
        self.procs.pop(s.id, None)
        s.turns += 1
        # The engine was cut off (stop, preempt, crash) while streaming a block: the
        # canonical event never arrived, so the text exists ONLY as unpersisted
        # partials. Commit what we have — under the same id, so the UI keeps the
        # text it is already showing — rather than losing it from the transcript.
        cut: list[str] = []
        for eid in open_ids:
            text = (acc.get(eid) or "").strip()
            if not text:
                continue
            self._emit(s.id, {"kind": "message" if eid.startswith("m") else "reasoning",
                              "id": eid, "text": text, "truncated": True})
            if eid.startswith("m"):     # only committed prose is worth replaying
                cut.append(text)
        # The engine was killed before it could record this in its own session, so
        # OUR transcript is the only copy — carry it into the next turn's prompt.
        s.interruptedTail = ("\n\n".join(cut))[-4000:] if cut else ""
        waiter = self._turn_flushed.get(s.id)
        if waiter:
            waiter.set()
        # Preempted by an incoming notification → chain into a fresh turn with the
        # wake prompt. The wake is computed HERE (not at preempt time) so any
        # additional notifications that landed after arming get coalesced in. Resume
        # the native thread when we have one; otherwise reconstruct the killed turn's
        # I/O as a fresh prompt so first-turn preempt loses no history (no engine
        # rollout yet on the very first turn).
        if self._preempt_chain.pop(s.id, None) is not None and s.status != "stopped":
            notes = self._pending_notes(s)
            for n in notes:
                n["delivered"] = True
            self._save_sessions()
            wake = self._wake_prompt(notes) if notes else "Resuming after a preempt."
            if s.threadId:
                return (wake, None, True, retry_used)
            return (self._reconstruct_fresh_prompt(s, s.prompt or prompt, wake), wake, False, retry_used)
        # Resume failed or the native engine thread is internally inconsistent
        # (e.g. Codex "orphan function call output" followed by an API schema
        # rejection) → fall back to a fresh native thread so the user message
        # still runs. CRITICAL: preserve prior I/O via reconstruction, so a
        # restart doesn't lose what the agent already did.
        if (code != 0 and resume_attempted and _resume_recoverable_failure(turn_flags)
                and not retry_used and s.status != "stopped"):
            self._emit(s.id, {"kind": "system",
                              "text": "↻ native engine resume failed — starting a fresh thread with the prior context preserved"})
            s.threadId = None
            s.threadStartedTurn = s.turns
            return (self._reconstruct_fresh_prompt(s, s.prompt or "", prompt), prompt, False, True)
        if s.status == "stopped":
            await self._release_browser(s)
            self._save_sessions()
            # if a notification was queued while we were running, it can now wake the
            # stopped (at-rest) session — exactly the same as done/failed
            self._maybe_deliver(s)
            return None
        # A message queued during this turn → continue immediately, KEEPING the
        # browser (no restart between back-to-back turns).
        if s.pendingSteers:
            nxt = s.pendingSteers.pop(0)
            self._save_sessions()
            return (nxt, None, True, retry_used)
        # A notification arrived during this turn but we never hit a safe boundary
        # (e.g. it arrived while a tool was in flight and the engine finished cleanly
        # right after). Consume it now as a fresh turn KEEPING the browser — the
        # ack-suppression rule already removed anything the agent learned inline, so
        # this fallback is usually a no-op (or one note).
        notes = self._pending_notes(s)
        if notes and not (code != 0 or turn_flags.get("turn_error")):
            for n in notes:
                n["delivered"] = True
            self._save_sessions()
            return (self._wake_prompt(notes), None, True, retry_used)
        # Decide the resting status. Engine crash (non-zero exit / top-level turn
        # error) → FAILURE. Otherwise: if a workflow this agent launched is still
        # running, it OWNS the profile lock and must keep its browser (the run is
        # attached to it) → WAITING, to be woken by the run's completion
        # notification. Only when nothing of its own is running do we release the
        # lock and go DONE. All three are reactivatable.
        if code != 0 or turn_flags.get("turn_error"):
            await self._release_browser(s)
            s.status = "failed"
            tail = "\n".join(turn_flags.get("stderr_tail") or [])
            s.error = (turn_flags.get("turn_error_msg") or _short(tail, 500)
                       or f"the {s.engine} agent ended unexpectedly (exit code {code})")
            if not turn_flags.get("turn_error"):  # surface a crash that produced no error event
                self._emit(s.id, {"kind": "error",
                                  "text": f"the {s.engine} agent crashed mid-turn (exit code {code}). {s.error}"[:600]})
        elif self._owned_active_runs(s):
            s.status = "waiting"   # keep the browser/profile lock; woken on completion
            self._emit(s.id, {"kind": "system",
                              "text": "⏸ turn ended with a workflow still running — paused; you'll be woken when it finishes"})
        elif s.scheduledAt and s.scheduledAt > time.time():
            await self._release_browser(s)   # behaves like done; Timeline re-queues at scheduledAt
            s.status = "scheduled"
            when = max(0, int(s.scheduledAt - time.time()))
            self._emit(s.id, {"kind": "system", "text": f"⏰ turn ended — scheduled to wake in ~{when}s"})
        else:
            await self._release_browser(s)
            s.status = "done"
        s.finishedAt = time.time()
        self._emit(s.id, {"kind": "status", "status": s.status})
        self._save_sessions()
        return None

    def _with_interrupted_tail(self, s: AgentSession, prompt: str) -> str:
        """Prepend what the previous, killed turn had already written but the engine
        never persisted. Consumed once — the next turn's own resume carries it."""
        tail = (s.interruptedTail or "").strip()
        if not tail:
            return prompt
        s.interruptedTail = ""
        self._save_sessions()
        return ("[Your previous turn was interrupted before your engine could record it, so it is "
                "missing from your own session history. This is what you had already written — "
                "continue from here, do NOT start over:]\n"
                f"{tail}\n\n---\n{prompt}")

    @staticmethod
    async def _feed_stdin(proc, text: str) -> None:
        """Write the turn's prompt to the engine and close stdin so it stops
        waiting for more. Best-effort: a turn killed mid-write (a stop, a preempt)
        makes these raise, and that is not an error worth surfacing."""
        try:
            if proc.stdin is None:
                return
            proc.stdin.write((text or "").encode())
            await proc.stdin.drain()
        except Exception:
            pass
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass

    async def _drain_stderr(self, sid: str, stream, flags: dict | None = None) -> None:
        if not stream:
            return
        while True:
            try:
                raw = await stream.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            low = line.lower()
            if flags is not None:
                # keep a rolling tail of stderr to use as the error message on a crash
                tail = flags.setdefault("stderr_tail", [])
                tail.append(line[:300])
                if len(tail) > 15:
                    del tail[0]
                # detect a failed resume so the caller can retry the message fresh
                if "no rollout" in low or ("resume" in low and ("fail" in low or "not found" in low)):
                    flags["resume_failed"] = True
                if "orphan function call output" in low:
                    flags["resume_failed"] = True
            # engine stderr is mostly progress noise; surface only error-ish lines
            if any(w in low for w in ("error", "failed", "exception", "denied", "traceback", "panic")):
                self._emit(sid, {"kind": "system", "text": f"[{self.sessions[sid].engine}] {line[:200]}"})

    # ------------------------------------------------------------------ command builders
    def _mcp_env_pairs(self, s: AgentSession, base_env: dict) -> dict:
        """Environment the MCP tool server child is started with.

        PYTHONPATH is load-bearing, not belt-and-braces: in a dev checkout the MCP
        server is `python -m orchestrator`, and Claude Code does NOT honour a `cwd`
        in an MCP server entry (Codex does), so without it the server dies with
        "No module named orchestrator" and the agent silently loses every Studio
        tool. Pointing PYTHONPATH at the backend package dir makes the module
        importable regardless of which engine — or which working directory —
        starts it. Harmless in a frozen build, where the server is the exe itself.
        """
        env = dict(base_env)
        if not getattr(sys, "frozen", False):
            prev = os.environ.get("PYTHONPATH") or ""
            env["PYTHONPATH"] = (BACKEND_DIR + os.pathsep + prev) if prev else BACKEND_DIR
        return env

    def _mcp_env_toml(self, env_pairs: dict) -> str:
        inner = ",".join(f"{k}={json.dumps(str(v))}" for k, v in env_pairs.items())
        return "mcp_servers.studio.env={" + inner + "}"

    def _codex_cmd(self, s, prompt, sysprompt, env_pairs, ws, resume) -> tuple[list[str], str]:
        """(argv, stdin) for one Codex turn.

        The prompt goes over STDIN (`-`), never as an argument. Our prompts are
        big — a role prompt is up to ~25 KB and a rebuilt history adds ~12 KB more
        — and Windows caps an entire command line at 32 767 characters, so passing
        them as argv is a "works on macOS, fails on Windows" trap. stdin also
        sidesteps every quoting and embedded-newline hazard.
        """
        binary = _find_binary("codex")
        base = _self_base()
        mcp_cmd, mcp_args = base[0], base[1:] + ["mcp"]
        full_prompt = (f"{sysprompt}\n\n---\nTask: {prompt}" if sysprompt and not resume else prompt)
        cmd = [binary, "exec", "--json", "--skip-git-repo-check",
               "--dangerously-bypass-approvals-and-sandbox", "-C", ws,
               "-c", f"mcp_servers.studio.command={json.dumps(mcp_cmd)}",
               "-c", "mcp_servers.studio.args=[" + ",".join(json.dumps(a) for a in mcp_args) + "]",
               "-c", f"mcp_servers.studio.cwd={json.dumps(BACKEND_DIR)}",
               "-c", self._mcp_env_toml(env_pairs)]
        if s.model:
            cmd += ["-m", s.model]
        if s.effort:
            cmd += ["-c", f"model_reasoning_effort={json.dumps(s.effort)}"]
        if resume and s.threadId:
            cmd += ["resume", s.threadId, "-"]
        else:
            cmd += ["-"]
        return cmd, full_prompt

    def _claude_cmd(self, s, prompt, sysprompt, env_pairs, resume) -> tuple[list[str], str]:
        """(argv, stdin) for one Claude Code turn.

        Same reasoning as Codex: the user prompt is piped in rather than passed as
        an argument. The system prompt has to go through `--append-system-prompt`
        (the file variant is not honoured in print mode), so on Windows — the only
        platform with a hard command-line ceiling — we fall back to folding it into
        the piped prompt whenever the two together would get close to the limit.
        """
        binary = _find_binary("claude")
        base = _self_base()
        cfg = {"mcpServers": {"studio": {"command": base[0], "args": base[1:] + ["mcp"],
                                         "cwd": BACKEND_DIR, "env": env_pairs}}}
        cfg_path = SESSIONS_DIR / s.id / "mcp.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg))
        cmd = [binary, "-p", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages",   # real per-token streaming of text + thinking
               "--mcp-config", str(cfg_path),
               # Allow Claude's built-in file tools alongside our MCP server, so
               # the agent can `Read` / `Write` / `Edit` the paths Studio gives
               # it for `file`-typed dataset cells and workflow outputs (without
               # this, --allowedTools acts as a strict allow-list and built-ins
               # are filtered out). Bash too, for the rare case of unzipping /
               # ffprobing / inspecting a downloaded file.
               "--allowedTools", "Read,Write,Edit,Bash,mcp__studio",
               "--permission-mode", "bypassPermissions"]
        if s.model:
            cmd += ["--model", s.model]
        if s.effort:
            cmd += ["--effort", s.effort]
        stdin_text = prompt
        if sysprompt:
            fits = (not IS_WIN) or (sum(len(a) + 3 for a in cmd) + len(sysprompt) < WIN_CMDLINE_BUDGET)
            if fits:
                cmd += ["--append-system-prompt", sysprompt]
            else:
                stdin_text = f"{sysprompt}\n\n---\nTask: {prompt}"
        if resume and s.threadId:
            cmd += ["--resume", s.threadId]
        return cmd, stdin_text


def engine_status() -> dict:
    """Which engines are installed/launchable on this machine, with install help."""
    from . import deps
    return deps.engine_status()


_agents: AgentManager | None = None


def get_agents() -> AgentManager:
    global _agents
    if _agents is None:
        _agents = AgentManager()
    return _agents
