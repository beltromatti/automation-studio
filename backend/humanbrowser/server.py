"""Control server: keeps one live browser session and exposes it over HTTP.

This is what unifies the two usage modes. The browser lives in this long-running
process; scripts, agents (including a CLI driven from a shell) and a human all
issue the *same* actions against the *same* session over localhost HTTP. Because
it is headed by default, the user can grab the window at any time — calling
``/pause`` simply tells the automation to stop acting until ``/resume``.
"""
from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from .browser import HumanBrowser, ActionError
from .config import BrowserConfig


class ControlServer:
    def __init__(self, config: BrowserConfig | None = None):
        self.cfg = config or BrowserConfig.from_env()
        self.browser = HumanBrowser(self.cfg)
        self._lock = asyncio.Lock()  # serialize mutating actions

    # ---------------------------------------------------------------- handlers
    async def h_status(self, request: web.Request) -> web.Response:
        return web.json_response(await self.browser.status())

    async def h_goto(self, request: web.Request) -> web.Response:
        data = await request.json()
        if self.browser.paused:
            return web.json_response(
                {"ok": False, "error": "session is paused for manual control; POST /resume first"},
                status=409,
            )
        async with self._lock:
            await self.browser.goto(data["url"])
        return web.json_response({"ok": True, "url": self.browser.page.url})

    async def h_observe(self, request: web.Request) -> web.Response:
        fmt = request.query.get("format", "text")
        max_nodes = int(request.query.get("max_nodes", 1200))
        ctx = await self.browser.observe(max_nodes=max_nodes)
        if fmt == "json":
            return web.json_response(ctx.to_dict())
        return web.json_response({"text": ctx.to_text()})

    async def h_act(self, request: web.Request) -> web.Response:
        data = await request.json()
        if self.browser.paused:
            return web.json_response(
                {"ok": False, "error": "session is paused for manual control; POST /resume first"},
                status=409,
            )
        action = data.pop("action")
        try:
            async with self._lock:
                result = await self.browser.act(action, **data)
            return web.json_response({"ok": True, "result": result})
        except ActionError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def h_pause(self, request: web.Request) -> web.Response:
        self.browser.paused = True
        return web.json_response({"ok": True, "paused": True})

    async def h_resume(self, request: web.Request) -> web.Response:
        self.browser.paused = False
        return web.json_response({"ok": True, "paused": False})

    async def h_screenshot(self, request: web.Request) -> web.Response:
        full = request.query.get("full", "0") in {"1", "true", "yes"}
        path = await self.browser.screenshot(full_page=full)
        return web.json_response({"ok": True, "path": path})

    async def h_switch_mode(self, request: web.Request) -> web.Response:
        data = await request.json()
        async with self._lock:
            await self.browser.switch_mode(bool(data["headless"]))
        return web.json_response({"ok": True, "headless": self.cfg.headless})

    async def h_eval(self, request: web.Request) -> web.Response:
        data = await request.json()
        result = await self.browser.eval_js(data["script"])
        return web.json_response({"ok": True, "result": result})

    async def h_text(self, request: web.Request) -> web.Response:
        return web.json_response({"text": await self.browser.get_text()})

    async def h_shutdown(self, request: web.Request) -> web.Response:
        async def _kill():
            await asyncio.sleep(0.2)
            await self.browser.stop()
            raise web.GracefulExit()
        asyncio.create_task(_kill())
        return web.json_response({"ok": True})

    # ---------------------------------------------------------------- app
    def build_app(self) -> web.Application:
        app = web.Application()
        app.add_routes([
            web.get("/status", self.h_status),
            web.post("/goto", self.h_goto),
            web.get("/observe", self.h_observe),
            web.post("/act", self.h_act),
            web.post("/pause", self.h_pause),
            web.post("/resume", self.h_resume),
            web.get("/screenshot", self.h_screenshot),
            web.post("/switch_mode", self.h_switch_mode),
            web.post("/eval", self.h_eval),
            web.get("/text", self.h_text),
            web.post("/shutdown", self.h_shutdown),
        ])
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def _on_startup(self, app: web.Application) -> None:
        await self.browser.start()

    async def _on_cleanup(self, app: web.Application) -> None:
        await self.browser.stop()

    def run(self) -> None:
        web.run_app(self.build_app(), host=self.cfg.host, port=self.cfg.port, print=None)


def main() -> None:
    ControlServer().run()


if __name__ == "__main__":
    main()
