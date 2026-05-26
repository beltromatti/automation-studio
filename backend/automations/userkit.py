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
    Columns default to the union of keys in row order."""
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
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in columns})
    _ev.result(output, len(rows))
