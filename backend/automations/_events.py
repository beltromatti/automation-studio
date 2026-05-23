"""Structured run-event protocol shared by all workflows.

A workflow stays *console-agnostic* by emitting tiny JSON events on stdout. The
console's RunManager parses these to drive live status, progress and results;
when run from a plain shell they're just unobtrusive lines. Every event line is
prefixed with an ASCII Record-Separator (0x1e) so it can never be confused with
ordinary log output.
"""
from __future__ import annotations

import json
import sys
import time

RS = "\x1e"  # record separator marking a structured event line


def emit(event: str, **data) -> None:
    rec = {"hb_event": event, "t": round(time.time(), 3), **data}
    sys.stdout.write(RS + json.dumps(rec, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def status(state: str, **data) -> None:
    emit("status", state=state, **data)


def progress(collected: int, total: int, *, message: str = "", url: str = "", page: int | None = None) -> None:
    emit("progress", collected=collected, total=total, message=message, url=url, page=page)


def result(csv_path: str, rows: int, **data) -> None:
    emit("result", csv=csv_path, rows=rows, **data)


def error(message: str, *, url: str = "") -> None:
    emit("error", message=message, url=url)


def log(message: str) -> None:
    emit("log", message=message)
