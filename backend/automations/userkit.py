"""Authoring kit for user/agent-created workflows.

A user/agent workflow is a plain .py file with a ``main(argv)`` that the
orchestrator invokes exactly like a built-in (``--params-json '{...}' --server URL
-o out.csv``). This kit hides the boilerplate so authoring is a few lines:

    import asyncio
    from automations import userkit

    async def run(params, sess):
        await sess.goto(params.get("url", "https://example.com"))
        title = await sess.evaluate("() => document.title")
        userkit.progress(1, 1, message="done")
        return [{"url": params.get("url", ""), "title": title}]

    def main(argv):
        params, server, output = userkit.parse(argv)
        rows = userkit.run_session(run, params, server)
        userkit.write_csv(output, rows, ["url", "title"])
        return 0

The same ``params``/``server``/output contract works in dev and the packaged app,
and against the agent's shared browser (the orchestrator passes ``--server``).
"""
from __future__ import annotations

import argparse
import asyncio
import csv as _csv
import json
import os
import sys
from typing import Any, Callable

from humanbrowser.session import open_session
from . import _events as _ev

# re-export the run-event emitters so authors `userkit.progress(...)` etc.
emit = _ev.emit
status = _ev.status
progress = _ev.progress
result = _ev.result
error = _ev.error
log = _ev.log


def parse(argv=None) -> tuple[dict, str | None, str | None]:
    """Return (params, server, output) from the standard workflow argv."""
    p = argparse.ArgumentParser()
    p.add_argument("--params-json", default="{}")
    p.add_argument("--server", default=None)
    p.add_argument("-o", "--output", default=None)
    a, _unknown = p.parse_known_args(argv if argv is not None else sys.argv[1:])
    try:
        params = json.loads(a.params_json or "{}")
    except json.JSONDecodeError:
        params = {}
    return params, a.server, a.output


def input_rows(argv=None) -> list[dict]:
    """Rows of the input dataset the run was given (for list-consuming workflows).
    The orchestrator passes ``--input-json <path>``; empty list if none."""
    p = argparse.ArgumentParser()
    p.add_argument("--input-json", default=None)
    a, _unknown = p.parse_known_args(argv if argv is not None else sys.argv[1:])
    if a.input_json and os.path.exists(a.input_json):
        try:
            with open(a.input_json, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def session(server: str | None = None, *, headless: bool = True, profile: str | None = None):
    """Open a session (attaches to the control server when ``server`` is set,
    else owns a local browser). You must call ``await s.start()`` / ``s.stop()``,
    or use ``run_session`` which manages that for you."""
    s, _owns = open_session(server=server, headless=headless, profile=profile)
    return s


def run_session(fn: Callable[[dict, Any], Any], params: dict, server: str | None,
                *, headless: bool = True, profile: str | None = None) -> Any:
    """Run an async ``fn(params, session)`` with a started session, emitting a
    running status and an error event on failure. Returns whatever ``fn`` returns."""
    async def _main():
        s = session(server, headless=headless, profile=profile)
        await s.start()
        _ev.status("running")
        try:
            return await fn(params, s)
        finally:
            await s.stop()
    try:
        return asyncio.run(_main())
    except Exception as e:  # surface as a run-event so the UI shows it
        _ev.error(str(e))
        raise


def write_csv(output: str | None, rows: list[dict], columns: list[str] | None = None) -> None:
    """Write result rows to the run's CSV (``output``) and emit the result event.
    Columns default to the union of keys in row order. Cells that are lists or
    dicts are JSON-encoded so a ``file_list`` (list of ids) or a registered
    file record round-trips through CSV cleanly."""
    rows = list(rows or [])
    if columns is None:
        columns = list({k: None for r in rows for k in r}.keys())
    if not output:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    with open(output, "w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for r in rows:
            out_row = {}
            for c in columns:
                v = r.get(c)
                if v is None:
                    out_row[c] = ""
                elif isinstance(v, (list, dict)):
                    out_row[c] = json.dumps(v, ensure_ascii=False)
                else:
                    out_row[c] = v
            w.writerow(out_row)
    _ev.result(output, len(rows))


# ---------------------------------------------------------------- files helpers
# Convenience for workflows that handle media / documents. The orchestrator
# already auto-expands `file` / `file_list` input columns to dicts before
# invoking us, and auto-registers raw paths emitted in `file` output columns,
# so most workflows just read ``row["image"]["path"]`` and write
# ``row["screenshot"] = "/tmp/shot.png"``. These helpers are for the cases
# where you want explicit control (tags / source hint / fetching from a URL).
def input_file(row: dict, column: str) -> dict | None:
    """Read a single-``file`` input cell. The orchestrator already expanded the
    cell from a bare id to ``{id, path, name, mime, ...}``; this just returns
    None for missing / empty values so a workflow can ``if f:`` cleanly."""
    v = (row or {}).get(column)
    if isinstance(v, dict) and v.get("path"):
        return v
    return None


def input_files(row: dict, column: str) -> list[dict]:
    """Read a ``file_list`` input cell as a list of records. Empty / missing → []."""
    v = (row or {}).get(column)
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict) and x.get("path")]
    return []


def save_output_file(path: str, *, name: str | None = None, source: str | None = None,
                     tags: list[str] | None = None) -> str:
    """Register a local file (produced by this workflow) into the Studio file
    store; returns the file id you should put in the ``file`` output cell.
    The orchestrator's auto-capture path also recognises raw paths in output
    rows, so call this only when you want explicit tags / source hints."""
    from orchestrator import files as _files
    rec = _files.register_from_path(path, name=name, source=source or "workflow", tags=tags)
    return rec["id"]


def fetch_url_file(url: str, *, name: str | None = None,
                   headers: dict | None = None, tags: list[str] | None = None,
                   source: str | None = None) -> str:
    """Plain HTTP fetch → store → returns file id."""
    from orchestrator import files as _files
    rec = _files.register_from_url(url, name=name, headers=headers,
                                   source=source or "workflow:fetch", tags=tags)
    return rec["id"]
