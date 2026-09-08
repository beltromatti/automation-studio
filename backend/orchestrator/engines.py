"""Engine catalog — what the locally-installed Codex / Claude Code CLIs can do.

Automation Studio never hardcodes the model line-up: models and their reasoning
efforts change under us every few weeks as the CLIs update. Instead we ASK the
installed binaries, the same way their own pickers do:

  codex   →  `codex app-server` JSON-RPC `model/list` (the exact call the Codex
             desktop picker makes: ids, display names, per-model reasoning
             efforts, the default). Falls back to `$CODEX_HOME/models_cache.json`
             (the catalogue Codex itself refreshes from the API) and finally to a
             tiny built-in seed so the UI is never empty.
  claude  →  `claude -p "/model"` (its own slash command prints the current model
             plus every accepted alias) and `claude --help` (the authoritative
             list of values `--effort` accepts). Display labels for
             account-specific extras come from `~/.claude.json`.

Everything here is best-effort and never raises: a missing / broken CLI degrades
to the seed catalogue with an `error` note the UI can show.

Also the single place that builds the ENVIRONMENT engine subprocesses run in.
Both CLIs are Node programs behind a `#!/usr/bin/env node` shim (or a .cmd on
Windows), so they need `node` on PATH. A Finder/Explorer-launched Electron app
inherits a bare PATH, so we re-add the toolchain dirs where node/nvm/volta/brew
live — otherwise a packaged build fails with "env: node: No such file or
directory" even though the CLI is installed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import deps

CATALOG_TTL = 900.0          # seconds a fetched catalogue stays fresh
PROBE_TIMEOUT = 25.0         # per-CLI probe budget

ENGINES = ("codex", "claude")

# Last-resort seeds. Deliberately minimal: they only exist so the picker renders
# something when the CLI can't be probed at all (offline, mid-upgrade, missing).
# Anything real comes from the engine itself.
_SEED: dict[str, dict] = {
    "codex": {
        "defaultModel": "gpt-5.5",
        "models": [{"id": "gpt-5.5", "label": "GPT-5.5", "description": "",
                    "efforts": ["low", "medium", "high", "xhigh"],
                    "defaultEffort": "high"}],
    },
    "claude": {
        "defaultModel": "opus",
        "models": [{"id": "opus", "label": "Opus", "description": "",
                    "efforts": ["low", "medium", "high", "xhigh", "max"],
                    "defaultEffort": "high"}],
    },
}

_cache: dict[str, dict] = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ subprocess env
def _extra_path_dirs() -> list[str]:
    """Toolchain bin dirs to graft onto PATH for engine subprocesses."""
    out: list[str] = []
    for d in deps.engine_search_dirs():
        try:
            if d.is_dir():
                out.append(str(d))
        except OSError:
            continue
    return out


def engine_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """os.environ + a PATH that can actually find `node`.

    The engine binaries themselves are resolved by absolute path, but their
    shebang (`#!/usr/bin/env node`) and their own child processes still need the
    JS toolchain on PATH. Packaged desktop apps don't inherit the login shell's
    PATH, so we prepend the standard install dirs (brew, nvm, volta, fnm, npm
    global, …). Existing entries win — we only ever ADD.
    """
    env = {**os.environ}
    sep = os.pathsep
    have = [p for p in (env.get("PATH") or "").split(sep) if p]
    seen = {os.path.normcase(p) for p in have}
    add = [d for d in _extra_path_dirs() if os.path.normcase(d) not in seen]
    if add:
        env["PATH"] = sep.join(have + add)
    if extra:
        env.update(extra)
    return env


def _run(cmd: list[str], timeout: float = PROBE_TIMEOUT) -> tuple[int, str, str]:
    """Run a short-lived probe. Never raises; returns (code, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=engine_env(),
                           errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", "timed out"
    except Exception as e:  # missing binary, permission, …
        return -1, "", str(e)


# ------------------------------------------------------------------ codex
def _codex_home() -> Path:
    h = os.environ.get("CODEX_HOME")
    return Path(h) if h else Path.home() / ".codex"


def _norm_codex_model(m: dict) -> dict | None:
    mid = (m.get("id") or m.get("slug") or m.get("model") or "").strip()
    if not mid:
        return None
    efforts, labels = [], {}
    for e in (m.get("supportedReasoningEfforts") or m.get("supported_reasoning_levels") or []):
        if isinstance(e, str):
            eid, desc = e, ""
        else:
            eid = (e.get("reasoningEffort") or e.get("effort") or "").strip()
            desc = e.get("description") or ""
        if eid and eid not in efforts:
            efforts.append(eid)
            labels[eid] = desc
    default_effort = (m.get("defaultReasoningEffort") or m.get("default_reasoning_level") or "").strip()
    if default_effort and default_effort not in efforts:
        efforts.append(default_effort)
    return {
        "id": mid,
        "label": m.get("displayName") or m.get("display_name") or mid,
        "description": m.get("description") or "",
        "efforts": efforts,
        "effortLabels": labels,
        "defaultEffort": default_effort or (efforts[-1] if efforts else ""),
        "isDefault": bool(m.get("isDefault")),
    }


def _codex_from_app_server(binary: str) -> list[dict]:
    """`codex app-server` speaks JSON-RPC over stdio; `model/list` is the exact
    call its own picker makes. One short-lived process, killed as soon as we
    have the answer."""
    proc = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1, env=engine_env())
    try:
        def send(obj: dict) -> None:
            assert proc.stdin
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "automation-studio",
                                        "version": os.environ.get("AUTOMATION_VERSION", "0")}}})
        send({"jsonrpc": "2.0", "id": 2, "method": "model/list",
              "params": {"includeHidden": False}})
        deadline = time.time() + PROBE_TIMEOUT
        assert proc.stdout
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("id") != 2:
                continue                      # notifications / the initialize reply
            if obj.get("error"):
                raise RuntimeError(str(obj["error"]))
            data = ((obj.get("result") or {}).get("data")) or []
            return [x for x in (_norm_codex_model(m) for m in data if isinstance(m, dict)) if x]
        raise RuntimeError("no model/list response")
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        for s in (proc.stdin, proc.stdout):
            try:
                if s:
                    s.close()
            except Exception:
                pass


def _codex_from_cache_file() -> list[dict]:
    """Codex keeps the catalogue it fetched from the API in models_cache.json."""
    f = _codex_home() / "models_cache.json"
    raw = json.loads(f.read_text())
    models = raw.get("models") if isinstance(raw, dict) else raw
    out = []
    for m in models or []:
        if not isinstance(m, dict):
            continue
        if (m.get("visibility") or "list") != "list":
            continue                          # hidden from the picker
        n = _norm_codex_model(m)
        if n:
            out.append(n)
    if not out:
        raise RuntimeError("cache file had no listable models")
    return out


def _fetch_codex() -> dict:
    binary = deps.find_engine("codex")
    if not binary:
        return {**_SEED["codex"], "source": "seed", "error": "codex CLI not found"}
    errors = []
    for name, fn in (("app-server", lambda: _codex_from_app_server(binary)),
                     ("cache", _codex_from_cache_file)):
        try:
            models = fn()
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        if models:
            default = next((m["id"] for m in models if m.get("isDefault")), models[0]["id"])
            return {"models": models, "defaultModel": default, "source": name,
                    "error": None}
    return {**_SEED["codex"], "source": "seed", "error": "; ".join(errors)[:300]}


# ------------------------------------------------------------------ claude
_CLAUDE_AVAILABLE_RE = re.compile(r"Available:\s*(.+?)(?:,\s*or a full model ID)?\.?\s*$",
                                  re.IGNORECASE | re.MULTILINE)
_CLAUDE_CURRENT_RE = re.compile(r"Current model:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
# `--effort <level>  Effort level for the current session (low, medium, high, xhigh, max)`
_CLAUDE_EFFORT_RE = re.compile(r"--effort\s+<level>[\s\S]{0,200}?\(([^)]*)\)")


def _alias_key(value: str) -> str:
    """Reduce a concrete model id to the alias family the CLI accepts, so
    `claude-fable-5[1m]` and the alias `fable[1m]` line up."""
    v = value.strip().lower()
    suffix = "[1m]" if "[1m]" in v else ""
    v = v.replace("[1m]", "")
    v = re.sub(r"^claude-", "", v)
    v = re.sub(r"-?\d+(?:-\d+)*$", "", v)     # drop the version tail (…-4-5, …-5)
    return f"{v}{suffix}"


def _claude_extra_labels() -> dict[str, dict]:
    """Account-specific model options Claude Code cached for the picker
    (`~/.claude.json`), keyed by the alias family the CLI accepts."""
    out: dict[str, dict] = {}
    try:
        raw = json.loads((Path.home() / ".claude.json").read_text())
    except Exception:
        return out
    for opt in raw.get("additionalModelOptionsCache") or []:
        if not isinstance(opt, dict) or opt.get("disabled"):
            continue
        val = str(opt.get("value") or "").strip()
        if not val:
            continue
        meta = {"label": opt.get("label") or val,
                "description": opt.get("description") or ""}
        out.setdefault(val, meta)
        key = _alias_key(val)
        if key:
            out.setdefault(key, meta)
            if key.endswith("[1m]"):          # the plain alias shares the blurb
                out.setdefault(key[: -len("[1m]")], meta)
    return out


def _claude_efforts(binary: str) -> list[str]:
    """The values `--effort` actually accepts, straight from `claude --help`.
    (The interactive `/effort` command advertises a couple of extra aliases the
    flag rejects, so the help text is the authoritative source for us.)"""
    code, out, err = _run([binary, "--help"], timeout=20)
    m = _CLAUDE_EFFORT_RE.search(out or err or "")
    if not m:
        return []
    vals = [v.strip() for v in m.group(1).split(",")]
    return [v for v in vals if re.fullmatch(r"[a-z][a-z0-9_-]*", v)]


def _claude_models(binary: str) -> tuple[list[str], str]:
    """Ask Claude Code itself: `/model` prints the current model and every alias
    it accepts. Runs as a local slash command — no tokens, no API call."""
    code, out, err = _run([binary, "-p", "/model", "--output-format", "json"])
    text = ""
    try:
        obj = json.loads(out)
        text = str(obj.get("result") or "")
    except Exception:
        text = out or ""
    if not text:
        raise RuntimeError((err or "no output from /model").strip()[:200])
    m = _CLAUDE_AVAILABLE_RE.search(text)
    if not m:
        raise RuntimeError("could not parse the /model listing")
    raw = m.group(1)
    raw = re.sub(r",?\s*or a full model ID\.?$", "", raw.strip(), flags=re.IGNORECASE)
    aliases, seen = [], set()
    for part in raw.split(","):
        a = part.strip().strip(".")
        if a and a not in seen and re.fullmatch(r"[A-Za-z0-9._\[\]-]+", a):
            seen.add(a)
            aliases.append(a)
    if not aliases:
        raise RuntimeError("the /model listing was empty")
    cur = _CLAUDE_CURRENT_RE.search(text)
    return aliases, (cur.group(1).strip() if cur else "")


def _pretty_alias(a: str) -> str:
    base = a.replace("[1m]", "").strip()
    label = base[:1].upper() + base[1:] if base else a
    return f"{label} (1M context)" if "[1m]" in a else label


def _fetch_claude() -> dict:
    binary = deps.find_engine("claude")
    if not binary:
        return {**_SEED["claude"], "source": "seed", "error": "claude CLI not found"}
    try:
        aliases, current = _claude_models(binary)
    except Exception as e:
        return {**_SEED["claude"], "source": "seed", "error": f"/model: {e}"[:300]}
    efforts = _claude_efforts(binary) or list(_SEED["claude"]["models"][0]["efforts"])
    extras = _claude_extra_labels()
    # "default" resolves to whatever the user's own Claude Code default is — keep
    # it out of the picker (we always pass an explicit model) but let the rest of
    # the aliases through in the order the CLI listed them.
    models = []
    for a in aliases:
        if a == "default":
            continue
        meta = extras.get(a, {})
        models.append({
            "id": a,
            # the alias itself is the clearest label ("Opus (1M context)"); the
            # cached picker entry only contributes its one-line blurb
            "label": _pretty_alias(a),
            "description": meta.get("description") or "",
            "efforts": list(efforts),
            "effortLabels": {},
            # Claude Code picks the effort per model itself when we don't force
            # one; "high" is its documented balanced point and is always valid.
            "defaultEffort": "high" if "high" in efforts else (efforts[-1] if efforts else ""),
            "isDefault": False,
        })
    if not models:
        return {**_SEED["claude"], "source": "seed", "error": "no usable aliases"}
    # Prefer the alias that matches the CLI's own current model, else the first
    # non-1M alias (the plain, cheapest-to-cache form).
    default = ""
    low = current.lower()
    for m in models:
        if m["id"].replace("[1m]", "").lower() in low:
            default = m["id"]
            break
    if not default:
        default = next((m["id"] for m in models if "[1m]" not in m["id"]), models[0]["id"])
    return {"models": models, "defaultModel": default, "source": "cli",
            "error": None, "current": current}


# ------------------------------------------------------------------ public API
def _fetch(engine: str) -> dict:
    data = _fetch_codex() if engine == "codex" else _fetch_claude()
    data["engine"] = engine
    data["fetchedAt"] = time.time()
    for m in data["models"]:
        m.setdefault("effortLabels", {})
        m.setdefault("description", "")
        m.setdefault("isDefault", False)
    return data


def catalog(engine: str, refresh: bool = False) -> dict:
    """The model/effort catalogue for one engine, cached for CATALOG_TTL."""
    engine = (engine or "").strip().lower()
    if engine not in ENGINES:
        raise ValueError(f"unknown engine: {engine}")
    with _lock:
        hit = _cache.get(engine)
        fresh = hit and not refresh and (time.time() - hit["fetchedAt"]) < CATALOG_TTL
        if fresh:
            return hit
    data = _fetch(engine)
    with _lock:
        # A failed probe must not evict a good catalogue we already have.
        prev = _cache.get(engine)
        if data.get("source") == "seed" and prev and prev.get("source") != "seed":
            prev = {**prev, "error": data.get("error")}
            _cache[engine] = prev
            return prev
        _cache[engine] = data
    return data


def catalogs(refresh: bool = False) -> dict:
    return {e: catalog(e, refresh) for e in ENGINES}


# Probing an engine means spawning it, which takes up to a couple of seconds on a
# cold cache. Everything on the backend's event loop must go through these so a
# refresh never stalls SSE streams, live runs or other agents.
async def catalog_async(engine: str, refresh: bool = False) -> dict:
    import asyncio
    return await asyncio.to_thread(catalog, engine, refresh)


async def catalogs_async(refresh: bool = False) -> dict:
    import asyncio
    return await asyncio.to_thread(catalogs, refresh)


async def resolve_async(engine: str, model: str | None, effort: str | None) -> tuple[str, str]:
    import asyncio
    return await asyncio.to_thread(resolve, engine, model, effort)


def resolve(engine: str, model: str | None, effort: str | None) -> tuple[str, str]:
    """Normalise a (model, effort) pair for `engine`, filling in the engine's own
    defaults and dropping anything the installed CLI doesn't advertise. Returning
    "" means "let the CLI decide" — we then omit the flag entirely."""
    try:
        cat = catalog(engine)
    except Exception:
        return (model or "", effort or "")
    models = cat.get("models") or []
    by_id = {m["id"]: m for m in models}
    mid = (model or "").strip()
    if mid not in by_id:
        mid = cat.get("defaultModel") or (models[0]["id"] if models else "")
    m = by_id.get(mid) or {}
    eff = (effort or "").strip()
    allowed = m.get("efforts") or []
    if allowed and eff not in allowed:
        eff = m.get("defaultEffort") or ""
        if allowed and eff not in allowed:
            eff = allowed[-1]
    return mid, eff
