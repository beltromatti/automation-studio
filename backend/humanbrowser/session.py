"""Unified session interface: drive a browser locally OR attach to a server.

A workflow written against :class:`Session` runs unchanged in two ways:

* **Local** — it owns a :class:`HumanBrowser` (standalone CLI use).
* **Remote** — it attaches to a running control server over HTTP, so the console
  keeps ownership of the window and can pause it, switch headed/headless, take a
  screenshot, or hand control to a human mid-run.

Both expose the same async methods, so the workflow code is identical either way
— the same "scripts and agents are not different" principle, extended to "owned
browser vs shared session are not different".
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from .browser import HumanBrowser
from .config import BrowserConfig
from .context import PageContext


class LocalSession:
    """Owns a HumanBrowser in-process."""

    def __init__(self, config: BrowserConfig):
        self.hb = HumanBrowser(config)

    async def start(self) -> None:
        await self.hb.start()

    async def stop(self) -> None:
        await self.hb.stop()

    async def goto(self, url: str) -> None:
        await self.hb.goto(url)

    async def current_url(self) -> str:
        return self.hb.page.url if self.hb.page else ""

    async def observe(self) -> PageContext:
        return await self.hb.observe()

    async def click(self, index: int) -> None:
        await self.hb.click(index)

    async def type(self, index: int, text: str, *, clear: bool = False, enter: bool = False) -> None:
        await self.hb.type(index, text, clear=clear, enter=enter)

    async def press(self, key: str) -> None:
        await self.hb.press(key)

    async def scroll(self, dy: int) -> None:
        await self.hb.scroll(dy)

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        if arg is None:
            return await self.hb.page.evaluate(js)
        return await self.hb.page.evaluate(js, arg)

    async def wait_for_selector(self, selector: str, timeout_ms: int = 15000) -> bool:
        try:
            await self.hb.page.wait_for_selector(selector, timeout=timeout_ms)
            return True
        except Exception:
            return False

    async def sleep(self, ms: int) -> None:
        await asyncio.sleep(ms / 1000)

    async def screenshot(self, path: str | None = None) -> str:
        return await self.hb.screenshot(path)


class RemoteSession:
    """Attaches to a running control server (humanbrowser.server)."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self._http: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._http = aiohttp.ClientSession()

    async def stop(self) -> None:
        # the console owns the server lifecycle; we only drop our HTTP client
        if self._http:
            await self._http.close()
            self._http = None

    async def _post(self, path: str, body: dict) -> dict:
        assert self._http
        async with self._http.post(self.base + path, json=body) as r:
            data = await r.json()
        # surface server-side refusals (e.g. paused for manual takeover) as errors
        # so an attached workflow actually halts instead of silently no-op'ing.
        if isinstance(data, dict) and data.get("ok") is False:
            raise RuntimeError(data.get("error", f"control server rejected {path}"))
        return data

    async def _get(self, path: str) -> dict:
        assert self._http
        async with self._http.get(self.base + path) as r:
            return await r.json()

    async def goto(self, url: str) -> None:
        await self._post("/goto", {"url": url})

    async def current_url(self) -> str:
        return (await self._get("/status")).get("url") or ""

    async def observe(self) -> PageContext:
        d = await self._get("/observe?format=json")
        return PageContext(
            url=d["url"], title=d["title"], scroll_y=d["scroll_y"],
            scroll_height=d["scroll_height"], inner_height=d["inner_height"],
            has_more_below=d["has_more_below"], num_elements=d["num_elements"],
            truncated=d["truncated"], nodes=d["elements"],
        )

    async def click(self, index: int) -> None:
        await self._post("/act", {"action": "click", "index": index})

    async def type(self, index: int, text: str, *, clear: bool = False, enter: bool = False) -> None:
        await self._post("/act", {"action": "type", "index": index, "text": text, "clear": clear, "enter": enter})

    async def press(self, key: str) -> None:
        await self._post("/act", {"action": "press", "key": key})

    async def scroll(self, dy: int) -> None:
        await self._post("/act", {"action": "scroll", "dy": dy})

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        if arg is None:
            script = js
        else:
            script = f"(() => {{ const __f = ({js}); return __f({json.dumps(arg)}); }})()"
        return (await self._post("/eval", {"script": script})).get("result")

    async def wait_for_selector(self, selector: str, timeout_ms: int = 15000) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        check = f"() => !!document.querySelector({json.dumps(selector)})"
        while asyncio.get_event_loop().time() < deadline:
            if await self.evaluate(check):
                return True
            await asyncio.sleep(0.4)
        return False

    async def sleep(self, ms: int) -> None:
        await asyncio.sleep(ms / 1000)

    async def screenshot(self, path: str | None = None) -> str:
        return (await self._get("/screenshot")).get("path", "")


def open_session(*, headless: bool = True, humanize: bool = True,
                 profile: str | None = None, server: str | None = None) -> tuple[Any, bool]:
    """Return ``(session, owns_browser)``. With ``server`` set, attach remotely;
    otherwise create a local session that owns its browser."""
    if server:
        return RemoteSession(server), False
    cfg = BrowserConfig(headless=headless, humanize=humanize)
    if profile:
        cfg.user_data_dir = profile  # type: ignore[assignment]
        cfg.__post_init__()
    return LocalSession(cfg), True
