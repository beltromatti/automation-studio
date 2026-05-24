"""Browser profiles — self-contained browser environments (cookies, logins,
history, storage), managed by the user and chosen per run.

A **persistent** profile keeps one on-disk **master** dir. Runs and the manual
"open to log in" session both run *directly* on that dir, so it accumulates
cookies/login/history and **ages genuinely**, like a real returning user. Chrome
locks a user-data-dir to one process, so the orchestrator serialises a persistent
profile (one run/session at a time); other profiles run in parallel. We clear any
stale lock left by a previous hard-kill before each launch (``clear_locks``).

The **ephemeral** profile is the exception: each run gets a brand-new throwaway
dir (``fresh_for_run``), discarded afterwards — so ephemeral runs need no login,
never dirty a real profile, and run fully in parallel.
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from humanbrowser.config import data_dir

PROFILES_DIR = data_dir() / "profiles"
EPHEMERAL_DIR = PROFILES_DIR / "_ephemeral"
META_FILE = PROFILES_DIR / "profiles.json"

_DEFAULT_COLORS = ["#0072f5", "#2bd576", "#f5a623", "#ff5c5c", "#a855f7", "#06b6d4", "#ec4899"]

# Single-instance lock / transient runtime files Chrome leaves at the root of the
# user-data-dir. If a previous session was hard-killed they can linger and make
# the next launch think the profile is "already in use" — we clear them before
# (re)opening a persistent profile, which is safe because runs on a persistent
# profile are serialised (never two at once).
_LOCK_FILES = {
    "SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile",
    "RunningChromeVersion", "DevToolsActivePort", "CrashpadMetrics-active.pma",
}


def _load() -> dict:
    try:
        return json.loads(META_FILE.read_text())
    except Exception:
        return {"profiles": []}


def _save(meta: dict) -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    META_FILE.write_text(json.dumps(meta, indent=2))


def _ensure_default(meta: dict) -> dict:
    """Register a 'Default' profile pointing at the pre-existing profiles/default
    dir (which already holds any earlier login), if not tracked yet."""
    ids = {p["id"] for p in meta["profiles"]}
    if "default" not in ids:
        meta["profiles"].insert(0, {
            "id": "default", "name": "Default", "color": _DEFAULT_COLORS[0],
            "createdAt": time.time(), "lastUsedAt": None,
        })
        (PROFILES_DIR / "default").mkdir(parents=True, exist_ok=True)
        _save(meta)
    return meta


def master_dir(pid: str) -> Path:
    return PROFILES_DIR / pid


def clear_locks(pid: str) -> None:
    """Remove stale single-instance lock / transient files from a profile's master
    dir so the next launch starts cleanly. Ensures the dir exists too."""
    d = master_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    for name in _LOCK_FILES:
        p = d / name
        try:
            if p.is_symlink() or p.exists():
                p.unlink()
        except OSError:
            pass


def list_profiles() -> list[dict]:
    meta = _ensure_default(_load())
    out = []
    for p in meta["profiles"]:
        d = master_dir(p["id"])
        size = 0
        try:
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        except Exception:
            pass
        out.append({**p, "sizeBytes": size})
    return out


def get(pid: str) -> dict | None:
    return next((p for p in _ensure_default(_load())["profiles"] if p["id"] == pid), None)


def create(name: str) -> dict:
    meta = _ensure_default(_load())
    pid = uuid.uuid4().hex[:10]
    color = _DEFAULT_COLORS[len(meta["profiles"]) % len(_DEFAULT_COLORS)]
    entry = {"id": pid, "name": name.strip() or "Profile", "color": color,
             "createdAt": time.time(), "lastUsedAt": None}
    meta["profiles"].append(entry)
    master_dir(pid).mkdir(parents=True, exist_ok=True)
    _save(meta)
    return entry


def rename(pid: str, name: str) -> dict | None:
    meta = _ensure_default(_load())
    for p in meta["profiles"]:
        if p["id"] == pid:
            p["name"] = name.strip() or p["name"]
            _save(meta)
            return p
    return None


def delete(pid: str) -> bool:
    if pid == "default":
        return False  # keep the default profile
    meta = _ensure_default(_load())
    before = len(meta["profiles"])
    meta["profiles"] = [p for p in meta["profiles"] if p["id"] != pid]
    if len(meta["profiles"]) == before:
        return False
    _save(meta)
    shutil.rmtree(master_dir(pid), ignore_errors=True)
    return True


def touch(pid: str) -> None:
    meta = _ensure_default(_load())
    for p in meta["profiles"]:
        if p["id"] == pid:
            p["lastUsedAt"] = time.time()
            _save(meta)
            return


def fresh_for_run(run_id: str) -> str:
    """A brand-new empty profile dir for an *ephemeral* run — no login, nothing
    persisted, discarded on teardown. Each ephemeral run gets its own, so any
    number of them run in parallel without ever touching a real profile."""
    dst = EPHEMERAL_DIR / run_id
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True, exist_ok=True)
    return str(dst)
