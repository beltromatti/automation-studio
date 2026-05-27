"""Configuration for the human-grade browser environment.

A single :class:`BrowserConfig` drives both headed and headless launches so that
the resulting fingerprint and behaviour are identical end-to-end regardless of
whether the GUI is shown. The only thing that changes between modes is the
``headless`` flag; everything that a remote site can observe is kept constant.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "AutomationStudio"
_IS_WIN = os.name == "nt" or sys.platform.startswith("win")
_IS_MAC = sys.platform == "darwin"


def data_dir() -> Path:
    """Cross-platform per-user data directory for all runtime state (profiles,
    artifacts, runs). humanbrowser stays autonomous: its *code* is self-contained,
    its *runtime data* lives in one standard location that the engine, the
    orchestrator and the app all agree on.

    Resolution order:
      1. ``$AUTOMATION_DATA_DIR``  (set in dev to <repo>/dev-data; by the app at runtime)
      2. the OS user-data dir      (~/Library/Application Support/AutomationStudio, %APPDATA%, ~/.local/share)
    """
    env = os.environ.get("AUTOMATION_DATA_DIR")
    if env:
        return Path(env)
    try:
        from platformdirs import user_data_dir
        return Path(user_data_dir(APP_NAME, APP_NAME))
    except Exception:
        return Path.home() / ".automation-studio"


DEFAULT_PROFILE_DIR = data_dir() / "profiles" / "default"
DEFAULT_ARTIFACTS_DIR = data_dir() / "artifacts"

CHROME_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def find_chrome() -> str | None:
    """Locate the installed system Google Chrome (or Chromium), cross-platform and
    fault-tolerant — probe the standard per-OS install locations and PATH. Returns
    the executable path, or None if no system browser is found (→ the app falls
    back to the bundled patchright Chromium). Never raises."""
    def ok(p: str | None) -> bool:
        try:
            return bool(p) and os.path.exists(p)
        except Exception:
            return False

    try:
        cands: list[str] = []
        if _IS_MAC:
            home = os.path.expanduser("~")
            cands = [
                CHROME_MAC,
                f"{home}/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ]
        elif _IS_WIN:
            pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
            pfx = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
            local = os.environ.get("LOCALAPPDATA", "")
            cands = [rf"{pf}\Google\Chrome\Application\chrome.exe",
                     rf"{pfx}\Google\Chrome\Application\chrome.exe"]
            if local:
                cands.append(rf"{local}\Google\Chrome\Application\chrome.exe")
        else:  # linux
            for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
                f = shutil.which(n)
                if ok(f):
                    return f
            cands = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                     "/opt/google/chrome/chrome", "/snap/bin/chromium",
                     "/usr/bin/chromium-browser", "/usr/bin/chromium"]
        for c in cands:
            if ok(c):
                return c
        for n in ("google-chrome", "chrome", "chromium"):
            f = shutil.which(n)
            if ok(f):
                return f
    except Exception:
        pass
    return None


def detect_chrome_major() -> int:
    """Return the installed Google Chrome major version (best effort, cross-platform)."""
    chrome = find_chrome()
    if chrome:
        try:
            out = subprocess.run([chrome, "--version"], capture_output=True, text=True, timeout=10).stdout
            m = re.search(r"(\d+)\.\d+\.\d+\.\d+", out)
            if m:
                return int(m.group(1))
        except Exception:
            pass
    return 148


def canonical_user_agent(major: int | None = None) -> str:
    """The genuine, reduced User-Agent string that real Chrome on macOS sends.

    Real Chrome reduced the UA to ``Chrome/<major>.0.0.0`` and always reports
    ``Intel Mac OS X 10_15_7`` (even on Apple Silicon). Reusing exactly this
    string strips the ``HeadlessChrome`` token that headless mode leaks while
    staying perfectly consistent with the client hints the browser sends.
    """
    if major is None:
        major = detect_chrome_major()
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


@dataclass
class BrowserConfig:
    # --- Engine / profile ---
    channel: str = "chrome"  # use the real installed Google Chrome
    user_data_dir: Path = field(default_factory=lambda: DEFAULT_PROFILE_DIR)
    headless: bool = False

    # --- Rendering / fingerprint (kept identical across headed & headless) ---
    viewport_width: int = 1280
    viewport_height: int = 800
    device_scale_factor: float = 2.0  # macOS retina
    locale: str = "en-US"
    timezone_id: str | None = None  # None -> use the host system's real timezone
    user_agent: str | None = None  # None -> canonical_user_agent()

    # --- Behaviour ---
    humanize: bool = True  # human-like mouse / typing / scrolling / think-time
    default_timeout_ms: int = 30_000

    # --- Control server ---
    host: str = "127.0.0.1"
    port: int = 8787

    artifacts_dir: Path = field(default_factory=lambda: DEFAULT_ARTIFACTS_DIR)

    def __post_init__(self) -> None:
        self.user_data_dir = Path(self.user_data_dir)
        self.artifacts_dir = Path(self.artifacts_dir)
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        if self.user_agent is None:
            self.user_agent = canonical_user_agent()

    @classmethod
    def from_env(cls) -> "BrowserConfig":
        """Build a config, letting environment variables override defaults.

        This is what the control server uses, so the launch mode can be flipped
        without code changes (e.g. ``HB_HEADLESS=1``).
        """
        def _b(name: str, default: bool) -> bool:
            v = os.environ.get(name)
            if v is None:
                return default
            return v.strip().lower() in {"1", "true", "yes", "on"}

        kwargs: dict = {
            "headless": _b("HB_HEADLESS", False),
            "humanize": _b("HB_HUMANIZE", True),
        }
        if os.environ.get("HB_PROFILE"):
            kwargs["user_data_dir"] = Path(os.environ["HB_PROFILE"])
        if os.environ.get("HB_LOCALE"):
            kwargs["locale"] = os.environ["HB_LOCALE"]
        if os.environ.get("HB_TIMEZONE"):
            kwargs["timezone_id"] = os.environ["HB_TIMEZONE"]
        if os.environ.get("HB_PORT"):
            kwargs["port"] = int(os.environ["HB_PORT"])
        # HB_CHANNEL: "chrome" (system, default) or "none"/"chromium"/"" to force
        # the bundled patchright Chromium (used when no system Chrome is present).
        ch = os.environ.get("HB_CHANNEL")
        if ch is not None:
            kwargs["channel"] = None if ch.strip().lower() in {"", "none", "chromium", "bundled"} else ch
        return cls(**kwargs)
