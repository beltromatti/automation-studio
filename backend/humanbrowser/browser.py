"""HumanBrowser — the unified, human-grade browser surface.

A single async object that scripts call directly and the control server exposes
over HTTP. It launches the real installed Chrome through Patchright with a
persistent profile, normalises the fingerprint so headed and headless look
identical to a remote site, and offers an action API addressed by the element
indices produced by :mod:`humanbrowser.context`.
"""
from __future__ import annotations

import asyncio
import os
import random
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from patchright.async_api import async_playwright, Page, BrowserContext, Playwright

from . import humanize
from .config import BrowserConfig
from .context import PageContext, collect


def _ensure_driver_executable() -> None:
    """When running from a frozen (PyInstaller) bundle, the bundled patchright
    Node driver may lose its executable bit. Restore it so the browser can launch.
    No-op in normal (non-frozen) runs."""
    if not getattr(sys, "frozen", False):
        return
    try:
        import patchright
        drv = Path(patchright.__file__).resolve().parent / "driver" / "node"
        if drv.exists():
            drv.chmod(drv.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except Exception:
        pass


class ActionError(Exception):
    """Raised when an action cannot be carried out (e.g. element not found)."""


class HumanBrowser:
    def __init__(self, config: BrowserConfig | None = None):
        self.cfg = config or BrowserConfig()
        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._last_ctx: PageContext | None = None
        self.paused: bool = False
        self.channel_used: str | None = None  # "chrome" | "bundled" — which browser actually launched

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        _ensure_driver_executable()
        self._pw = await async_playwright().start()
        launch_kwargs: dict[str, Any] = dict(
            user_data_dir=str(self.cfg.user_data_dir),
            headless=self.cfg.headless,
            user_agent=self.cfg.user_agent,
            locale=self.cfg.locale,
            viewport={"width": self.cfg.viewport_width, "height": self.cfg.viewport_height},
            device_scale_factor=self.cfg.device_scale_factor,
            args=[f"--window-size={self.cfg.viewport_width},{self.cfg.viewport_height + 120}"],
        )
        if self.cfg.timezone_id:
            launch_kwargs["timezone_id"] = self.cfg.timezone_id
        # Prefer the real system Google Chrome (channel) for best stealth; if it
        # isn't installed (fresh machine), fall back to the bundled patchright
        # Chromium (PLAYWRIGHT_BROWSERS_PATH) so the app works everywhere. The UA
        # is normalised either way, so the observable fingerprint stays consistent.
        if self.cfg.channel:  # prefer system Chrome
            try:
                self.context = await self._pw.chromium.launch_persistent_context(
                    channel=self.cfg.channel, **launch_kwargs
                )
                self.channel_used = "chrome"
            except Exception as e:
                # fresh machine / no system Chrome → fall back to the bundled Chromium
                print(f"[browser] system Chrome unavailable ({type(e).__name__}); "
                      f"falling back to the bundled Chromium", flush=True)
                self.context = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
                self.channel_used = "bundled"
        else:  # explicitly asked for the bundled browser
            self.context = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
            self.channel_used = "bundled"
        print(f"[browser] launched on {'system Google Chrome' if self.channel_used == 'chrome' else 'the bundled Chromium'}",
              flush=True)
        self.context.set_default_timeout(self.cfg.default_timeout_ms)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        # keep "current page" pointed at whatever the user/automation focuses last
        self.context.on("page", self._on_new_page)

    def _on_new_page(self, page: Page) -> None:
        self.page = page

    async def stop(self) -> None:
        udd = str(self.cfg.user_data_dir)
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self.context = self.page = self._pw = None
        self._reap_profile_chrome(udd)

    def _reap_profile_chrome(self, user_data_dir: str) -> None:
        """Guarantee no Chrome lingers for THIS profile after we close.

        patchright's ``context.close()`` can leave a couple of Chrome helper /
        crashpad processes alive, which over many runs would accumulate. We kill
        exactly the Chrome processes that reference our own ``user_data_dir`` —
        never the current Python process (whose argv also contains the path) and
        never the user's personal Chrome (a different data dir).
        """
        try:
            mypid = os.getpid()
            out = subprocess.run(
                ["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5
            ).stdout
            for line in out.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) < 2:
                    continue
                pid_s, cmd = parts
                if user_data_dir not in cmd:
                    continue
                low = cmd.lower()
                if "google chrome" not in low and "chromium" not in low and "crashpad" not in low:
                    continue  # only Chrome/crashpad, not our python or node driver
                try:
                    pid = int(pid_s)
                    if pid != mypid:
                        os.kill(pid, 9)
                except (ValueError, ProcessLookupError):
                    pass
        except Exception:
            pass

    async def switch_mode(self, headless: bool) -> None:
        """Flip headed<->headless, preserving the session via the on-disk profile.

        The persistent profile keeps cookies/storage, so we close the context and
        relaunch in the new mode, then restore the current URL.
        """
        url = self.page.url if self.page else None
        await self.stop()
        self.cfg.headless = headless
        await self.start()
        if url and not url.startswith("about:"):
            await self.goto(url)

    # ------------------------------------------------------------------ helpers
    def _require_page(self) -> Page:
        if not self.page:
            raise ActionError("browser not started")
        return self.page

    async def _think(self) -> None:
        if self.cfg.humanize:
            await humanize.think()

    def _locator(self, index: int):
        page = self._require_page()
        loc = page.locator(f'[data-hb-index="{index}"]')
        return loc

    async def _resolve(self, index: int):
        """Return a locator for ``index``, falling back to the xpath captured at
        observe time if the page mutated and dropped the index attribute."""
        loc = self._locator(index)
        try:
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass
        if self._last_ctx:
            node = self._last_ctx.element(index)
            if node and node.get("xpath"):
                xp = node["xpath"]
                xloc = self._require_page().locator(f"xpath={xp}")
                if await xloc.count() > 0:
                    return xloc.first
        raise ActionError(f"element [{index}] not found (try observe again)")

    # ------------------------------------------------------------------ navigation
    async def goto(self, url: str, *, wait_until: str = "domcontentloaded") -> None:
        page = self._require_page()
        if "://" not in url:
            url = "https://" + url
        await page.goto(url, wait_until=wait_until)
        await self._think()

    async def back(self) -> None:
        await self._require_page().go_back()
        await self._think()

    async def forward(self) -> None:
        await self._require_page().go_forward()
        await self._think()

    async def reload(self) -> None:
        await self._require_page().reload()
        await self._think()

    # ------------------------------------------------------------------ observation
    async def observe(self, *, max_nodes: int = 1200) -> PageContext:
        page = self._require_page()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        ctx = await collect(page, max_nodes=max_nodes)
        self._last_ctx = ctx
        return ctx

    async def screenshot(self, path: str | Path | None = None, *, full_page: bool = False) -> str:
        page = self._require_page()
        if path is None:
            path = self.cfg.artifacts_dir / f"shot_{int(time.time()*1000)}.png"
        path = Path(path)
        await page.screenshot(path=str(path), full_page=full_page)
        return str(path)

    # ------------------------------------------------------------------ actions
    async def click(self, index: int) -> None:
        loc = await self._resolve(index)
        await loc.scroll_into_view_if_needed()
        if self.cfg.humanize:
            box = await loc.bounding_box()
            if box:
                cx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                cy = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                await humanize.click_at(self._require_page(), cx, cy)
                await self._think()
                return
        await loc.click()
        await self._think()

    async def type(self, index: int, text: str, *, clear: bool = False, enter: bool = False) -> None:
        loc = await self._resolve(index)
        await loc.scroll_into_view_if_needed()
        await self.click(index)  # focus like a person would
        if clear:
            await self._require_page().keyboard.press("Meta+A")
            await self._require_page().keyboard.press("Backspace")
        if self.cfg.humanize:
            await humanize.type_text(self._require_page(), text)
        else:
            await loc.type(text)
        if enter:
            await asyncio.sleep(random.uniform(0.1, 0.3))
            await self._require_page().keyboard.press("Enter")
        await self._think()

    async def fill(self, index: int, text: str) -> None:
        """Set a value directly (fast, non-human). Useful for scripts."""
        loc = await self._resolve(index)
        await loc.fill(text)

    async def press(self, key: str) -> None:
        await self._require_page().keyboard.press(key)
        await self._think()

    async def select_option(self, index: int, value: str | None = None, label: str | None = None) -> list[str]:
        loc = await self._resolve(index)
        if label is not None:
            return await loc.select_option(label=label)
        return await loc.select_option(value=value)

    async def scroll(self, dy: int) -> None:
        page = self._require_page()
        if self.cfg.humanize:
            await humanize.scroll_by(page, dy)
        else:
            await page.mouse.wheel(0, dy)
        await asyncio.sleep(random.uniform(0.2, 0.5))

    async def scroll_to_index(self, index: int) -> None:
        loc = await self._resolve(index)
        await loc.scroll_into_view_if_needed()
        await self._think()

    async def wait_for(self, *, state: str = "networkidle", timeout: int = 15000) -> None:
        await self._require_page().wait_for_load_state(state, timeout=timeout)

    async def eval_js(self, script: str) -> Any:
        return await self._require_page().evaluate(script)

    async def get_text(self) -> str:
        return await self._require_page().evaluate("() => document.body.innerText")

    # ------------------------------------------------------------------ status / dispatch
    async def status(self) -> dict[str, Any]:
        page = self.page
        return {
            "started": self.context is not None,
            "headless": self.cfg.headless,
            "humanize": self.cfg.humanize,
            "paused": self.paused,
            "browser": self.channel_used,  # "chrome" | "bundled"
            "url": page.url if page else None,
            "title": (await page.title()) if page else None,
        }

    async def act(self, action: str, **kw) -> Any:
        """Dispatch a named action — the single entry point used by the server
        and by agent loops, so scripts and agents share one vocabulary."""
        action = action.lower()
        if action == "goto":
            return await self.goto(kw["url"])
        if action == "click":
            return await self.click(int(kw["index"]))
        if action == "type":
            return await self.type(int(kw["index"]), kw.get("text", ""), clear=kw.get("clear", False), enter=kw.get("enter", False))
        if action == "fill":
            return await self.fill(int(kw["index"]), kw.get("text", ""))
        if action == "press":
            return await self.press(kw["key"])
        if action == "select":
            return await self.select_option(int(kw["index"]), value=kw.get("value"), label=kw.get("label"))
        if action == "scroll":
            return await self.scroll(int(kw.get("dy", 600)))
        if action == "scroll_to":
            return await self.scroll_to_index(int(kw["index"]))
        if action in ("back", "forward", "reload"):
            return await getattr(self, action)()
        if action == "wait":
            return await self.wait_for(state=kw.get("state", "networkidle"), timeout=int(kw.get("timeout", 15000)))
        raise ActionError(f"unknown action: {action}")
