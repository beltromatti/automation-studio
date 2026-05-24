"""Browser profiles — self-contained browser environments (cookies, logins,
history, storage), managed by the user and chosen per run.

Chrome locks a user-data-dir to a single process, so to allow many parallel runs
on the *same* profile we keep a persistent **master** dir per profile and give
each run a fast **clone** of it (cookies/login preserved, caches/locks skipped).
The master is edited only by a manual "open" session (for logging in / setup).
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

# Directory/file names that are pure cache or single-instance locks: skipped when
# cloning so the clone is small, fast and not seen as "already in use" by Chrome.
_SKIP = {
    "Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache",
    "DawnWebGPUCache", "GrShaderCache", "ShaderCache", "Default Cache",
    "component_crx_cache", "extensions_crx_cache",
    # single-instance locks + transient runtime files (must not be cloned)
    "SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile",
    "RunningChromeVersion", "DevToolsActivePort", "CrashpadMetrics-active.pma",
}

_DEFAULT_COLORS = ["#0072f5", "#2bd576", "#f5a623", "#ff5c5c", "#a855f7", "#06b6d4", "#ec4899"]


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


def _robust_copy(src: Path, dst: Path) -> None:
    """Recursive copy that skips caches/locks, ignores non-regular files
    (sockets/fifos) and tolerates files Chrome removes mid-copy — so cloning a
    live-ish profile never fails."""
    dst.mkdir(parents=True, exist_ok=True)
    try:
        entries = list(src.iterdir())
    except OSError:
        return
    for item in entries:
        if item.name in _SKIP:
            continue
        try:
            if item.is_symlink():
                continue
            if item.is_dir():
                _robust_copy(item, dst / item.name)
            elif item.is_file():
                shutil.copy2(item, dst / item.name)
            # anything else (socket/fifo/device) is skipped
        except (FileNotFoundError, OSError):
            continue  # transient/special file — safe to skip


def clone_for_run(pid: str, run_id: str) -> str:
    """Copy a profile's master into an ephemeral per-run dir (login preserved,
    caches/locks skipped) so the run is isolated and parallel-safe. Returns path."""
    dst = EPHEMERAL_DIR / run_id
    shutil.rmtree(dst, ignore_errors=True)
    src = master_dir(pid)
    if src.exists():
        _robust_copy(src, dst)
    else:
        dst.mkdir(parents=True, exist_ok=True)
    return str(dst)


def fresh_for_run(run_id: str) -> str:
    """A brand-new empty profile for this run (no login, nothing persisted)."""
    dst = EPHEMERAL_DIR / run_id
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True, exist_ok=True)
    return str(dst)
