"""External-dependency gateway: resolve + report the tools Automation Studio uses.

Every external binary the app shells out to — the agent engines (codex, claude)
and the browser (system Chrome, with the shipped patchright Chromium as fallback)
— is resolved here, cross-platform and fault-tolerant (never raises), probing PATH
plus the standard per-OS install locations. Also carries per-OS install
instructions so the UI can guide the user when something is missing.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from humanbrowser.config import find_chrome  # browser resolution lives in the engine layer

IS_WIN = os.name == "nt" or sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
PLATFORM = "win" if IS_WIN else "mac" if IS_MAC else "linux"


def _ok(p: str | Path | None) -> bool:
    try:
        return bool(p) and os.path.exists(p)
    except Exception:
        return False


def _node_tool_dirs() -> list[Path]:
    """Standard per-user JS-toolchain bin dirs where npm/brew/etc. drop codex/claude."""
    home = Path.home()
    dirs: list[Path] = []
    try:
        if IS_WIN:
            for v in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
                if v:
                    dirs.append(Path(v) / "npm")
            if os.environ.get("VOLTA_HOME"):
                dirs.append(Path(os.environ["VOLTA_HOME"]) / "bin")
            dirs += [home / "AppData" / "Roaming" / "npm", home / ".volta" / "bin"]
        else:
            dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin"),
                     home / ".local" / "bin", home / ".npm-global" / "bin",
                     home / ".volta" / "bin", home / ".bun" / "bin", home / ".deno" / "bin",
                     home / ".yarn" / "bin", Path("/usr/local/share/npm/bin")]
            if os.environ.get("VOLTA_HOME"):
                dirs.append(Path(os.environ["VOLTA_HOME"]) / "bin")
            # nvm / fnm keep a bin dir under each installed node version
            for base in (home / ".nvm" / "versions" / "node",
                         home / ".fnm" / "node-versions",
                         home / "Library" / "Application Support" / "fnm" / "node-versions"):
                try:
                    if base.is_dir():
                        for v in sorted(base.iterdir(), reverse=True):
                            dirs.append(v / "bin")
                            dirs.append(v / "installation" / "bin")
                except Exception:
                    continue
    except Exception:
        pass
    return dirs


def _names(name: str) -> list[str]:
    return [f"{name}.cmd", f"{name}.exe", name] if IS_WIN else [name]


def find_engine(engine: str) -> str | None:
    """Resolve a locally-installed agent engine binary (codex|claude). Never raises."""
    if engine not in ("codex", "claude"):
        return None
    try:
        f = shutil.which(engine)  # honours PATHEXT on Windows
        if _ok(f):
            return f
        for d in _node_tool_dirs():
            for nm in _names(engine):
                p = d / nm
                if _ok(p):
                    return str(p)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------- install help
def _engine_install(engine: str) -> dict:
    if engine == "codex":
        return {
            "name": "OpenAI Codex CLI",
            "url": "https://developers.openai.com/codex",
            "mac": ["brew install codex", "# or:  npm install -g @openai/codex", "codex login   # sign in with your ChatGPT subscription"],
            "win": ["npm install -g @openai/codex", "codex login"],
            "linux": ["npm install -g @openai/codex", "codex login"],
            "note": "Codex uses your ChatGPT (Plus/Pro) subscription — no API key needed.",
        }
    return {
        "name": "Claude Code",
        "url": "https://docs.claude.com/en/docs/claude-code/overview",
        "mac": ["brew install --cask claude-code", "# or:  npm install -g @anthropic-ai/claude-code", "claude   # then /login with your Claude subscription"],
        "win": ["npm install -g @anthropic-ai/claude-code", "claude   # then /login"],
        "linux": ["curl -fsSL https://claude.ai/install.sh | bash", "# or:  npm install -g @anthropic-ai/claude-code", "claude   # then /login"],
        "note": "Claude Code uses your Claude (Pro/Max) subscription — no API key needed.",
    }


def _chrome_install() -> dict:
    return {
        "name": "Google Chrome",
        "url": "https://www.google.com/chrome/",
        "mac": ["brew install --cask google-chrome", "# or download from google.com/chrome"],
        "win": ["winget install -e --id Google.Chrome", "# or download from google.com/chrome"],
        "linux": ["wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb",
                  "sudo apt install ./google-chrome-stable_current_amd64.deb"],
        "note": "Optional — Automation Studio ships a bundled browser and falls back to it automatically.",
    }


def engine_status() -> dict:
    """{codex|claude: {available, path, install}} — for the agent launch UI."""
    out = {}
    for e in ("codex", "claude"):
        p = find_engine(e)
        out[e] = {"available": p is not None, "path": p, "install": _engine_install(e)}
    return out


def chrome_status() -> dict:
    p = find_chrome()
    return {"available": p is not None, "path": p, "install": _chrome_install()}


def deps_status() -> dict:
    """One snapshot of every external dependency, with per-OS install guidance."""
    return {"platform": PLATFORM, "engines": engine_status(), "chrome": chrome_status()}
