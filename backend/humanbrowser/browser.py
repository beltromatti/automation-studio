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
        # Where patchright drops in-flight downloads BEFORE we save_as() them
        # into the file store. Fixed location (instead of a random tmpdir) so we
        # can clean up reliably across runs and never lose a file to PID exit.
        downloads_path = self.cfg.artifacts_dir / "downloads"
        downloads_path.mkdir(parents=True, exist_ok=True)
        launch_kwargs: dict[str, Any] = dict(
            user_data_dir=str(self.cfg.user_data_dir),
            headless=self.cfg.headless,
            user_agent=self.cfg.user_agent,
            locale=self.cfg.locale,
            viewport={"width": self.cfg.viewport_width, "height": self.cfg.viewport_height},
            device_scale_factor=self.cfg.device_scale_factor,
            accept_downloads=True,           # default in modern Playwright; explicit for clarity
            downloads_path=str(downloads_path),
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

    # ------------------------------------------------------------------ files
    async def upload(self, files: list, *, index: int | None = None,
                     selector: str | None = None,
                     names: list | None = None, mimes: list | None = None) -> dict:
        """Upload one or more local files to an `<input type=file>`.

        Target the input EITHER by ``index`` (resolved from observe — works for
        inputs the snapshot saw) OR by ``selector`` (a CSS selector; Playwright
        pierces shadow DOM by default, so this reaches inputs our observe
        doesn't enumerate — e.g. Reddit's hidden upload <input> living in a
        shadow root). With a selector, the FIRST matching input is used.

        Works even when the input is hidden (the common pattern where a styled
        button triggers the real `<input>`) — ``set_input_files`` calls the CDP
        method directly and doesn't gate on visibility.

        When ``names``/``mimes`` are supplied (per-file, parallel to ``files``),
        we read the bytes and pass FilePayloads to ``set_input_files`` — so the
        page sees the ORIGINAL filename instead of the content-addressed
        sha256.ext that lives in the Studio store. Without names, we pass the
        raw path (fine for raw-path uploads where there's no original name)."""
        if not files:
            raise ActionError("upload: provide at least one file path")
        if selector:
            loc = self._require_page().locator(selector).first
        elif index is not None:
            loc = await self._resolve(int(index))
        else:
            raise ActionError("upload: provide either `index` or `selector`")
        names = names or []
        mimes = mimes or []
        payloads: list[Any] = []
        for i, p in enumerate(files):
            nm = (names[i] if i < len(names) else "") or ""
            mm = (mimes[i] if i < len(mimes) else "") or "application/octet-stream"
            if nm:
                # Browser sees this filename + mime; site can't see the on-disk path.
                with open(p, "rb") as fh:
                    data = fh.read()
                payloads.append({"name": nm, "mimeType": mm, "buffer": data})
            else:
                payloads.append(str(p))
        await loc.set_input_files(payloads)
        await self._think()
        return {"uploaded": [{"path": str(p),
                              "name": (names[i] if i < len(names) else None) or os.path.basename(str(p)),
                              "mime": (mimes[i] if i < len(mimes) else None)}
                             for i, p in enumerate(files)],
                "count": len(files)}

    async def download_click(self, index: int, timeout_ms: int = 30_000) -> dict:
        """Click ``index`` AND capture the resulting download in one call.
        Saves into ``downloads_path`` and returns the path + suggested filename
        + originating URL. The caller (MCP server) then registers into the file
        store and unlinks this temp path."""
        page = self._require_page()
        loc = await self._resolve(index)
        async with page.expect_download(timeout=timeout_ms) as dl_info:
            await loc.click()
        dl = await dl_info.value
        dst = self.cfg.artifacts_dir / "downloads" / dl.suggested_filename
        # patchright auto-uniquifies via download id; if two downloads share a
        # suggested name within one session, fall back to download.path() (its
        # GUID under downloads_path).
        try:
            await dl.save_as(str(dst))
            path = str(dst)
        except Exception:
            p = await dl.path()
            path = str(p) if p else ""
        return {"path": path, "suggested_filename": dl.suggested_filename, "url": dl.url}

    async def expect_download(self, timeout_ms: int = 30_000) -> dict:
        """Wait for the next download triggered by page JS (no click here).
        Same return shape as ``download_click``."""
        page = self._require_page()
        async with page.expect_download(timeout=timeout_ms) as dl_info:
            pass  # the trigger already happened (or will happen) outside us
        dl = await dl_info.value
        dst = self.cfg.artifacts_dir / "downloads" / dl.suggested_filename
        try:
            await dl.save_as(str(dst))
            path = str(dst)
        except Exception:
            p = await dl.path()
            path = str(p) if p else ""
        return {"path": path, "suggested_filename": dl.suggested_filename, "url": dl.url}

    async def fetch(self, url: str, headers: dict | None = None, timeout_ms: int = 30_000) -> dict:
        """Authenticated HTTP GET via the browser's request context — sends the
        page's session cookies (right tool for session-locked assets). Writes
        the body to ``downloads_path`` and returns metadata + path."""
        if not self.context:
            raise ActionError("browser not started")
        resp = await self.context.request.get(url, headers=headers or {}, timeout=timeout_ms)
        try:
            body = await resp.body()
        finally:
            await resp.dispose()
        # Derive a sensible filename: Content-Disposition > URL path tail > "download"
        cd = ""
        try:
            cd = (resp.headers or {}).get("content-disposition", "") or ""
        except Exception:
            pass
        import re as _re
        m = _re.search(r'filename\*?=(?:UTF-8\'\')?"?([^"\;]+)', cd)
        if m:
            name = m.group(1)
        else:
            from urllib.parse import urlparse
            tail = (urlparse(url).path or "").rstrip("/").rsplit("/", 1)[-1]
            name = tail or "download"
        dst = self.cfg.artifacts_dir / "downloads" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(body)
        ctype = (resp.headers or {}).get("content-type", "").split(";")[0].strip() if hasattr(resp, "headers") else ""
        return {"ok": resp.ok, "status": resp.status, "url": resp.url, "path": str(dst),
                "suggested_filename": name, "contentType": ctype}

    async def resolve_url(self, url: str, *, max_hops: int = 5,
                          timeout_ms: int = 15_000) -> dict:
        """Follow a redirect chain WITHOUT downloading the destination.

        Search engines increasingly hide result links behind their own tracking
        redirect (Google serves `/goto?url=<opaque>`), so the href in the DOM is
        no longer the real destination. Reading each hop's `location` header
        resolves it for the cost of one tiny request per hop — as opposed to
        `fetch`, which would pull the whole target page down to disk.
        Cookies come from the page's own context, so session-locked redirects
        resolve too. Never raises: an unresolvable url comes back unchanged.
        """
        if not self.context:
            raise ActionError("browser not started")
        from urllib.parse import urljoin
        cur, hops = url, 0
        status = 0
        while hops < max_hops:
            try:
                resp = await self.context.request.get(cur, max_redirects=0, timeout=timeout_ms)
            except Exception:
                break
            try:
                status = resp.status
                loc = (resp.headers or {}).get("location") or ""
            finally:
                await resp.dispose()
            if not (300 <= status < 400) or not loc:
                break
            cur = urljoin(cur, loc)
            hops += 1
        return {"url": cur, "hops": hops, "status": status}

    async def file_chooser(self, index: int, files: list, *, names: list | None = None,
                           mimes: list | None = None, timeout_ms: int = 15_000) -> dict:
        """Click ``index`` WHILE expecting a file-chooser popup, then provide
        the file(s). For sites where the upload UI is a custom button that
        opens the OS picker (no `<input type=file>` reachable directly).
        For the standard `<input>` case use ``upload`` — it's simpler."""
        if not files:
            raise ActionError("file_chooser: provide at least one file path")
        page = self._require_page()
        loc = await self._resolve(index)
        names = names or []
        mimes = mimes or []
        payloads: list[Any] = []
        for i, p in enumerate(files):
            nm = (names[i] if i < len(names) else "") or ""
            mm = (mimes[i] if i < len(mimes) else "") or "application/octet-stream"
            if nm:
                with open(p, "rb") as fh:
                    data = fh.read()
                payloads.append({"name": nm, "mimeType": mm, "buffer": data})
            else:
                payloads.append(str(p))
        async with page.expect_file_chooser(timeout=timeout_ms) as fc_info:
            await loc.click()
        chooser = await fc_info.value
        await chooser.set_files(payloads)
        await self._think()
        return {"uploaded": [{"path": str(p),
                              "name": (names[i] if i < len(names) else None) or os.path.basename(str(p))}
                             for i, p in enumerate(files)],
                "count": len(files), "is_multiple": chooser.is_multiple()}

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
        # files (the MCP server passes file ids → paths before reaching us)
        if action == "upload":
            return await self.upload(list(kw.get("files") or []),
                                     index=(int(kw["index"]) if kw.get("index") is not None else None),
                                     selector=kw.get("selector"),
                                     names=list(kw.get("names") or []),
                                     mimes=list(kw.get("mimes") or []))
        if action == "download_click":
            return await self.download_click(int(kw["index"]), timeout_ms=int(kw.get("timeout_ms", 30_000)))
        if action == "expect_download":
            return await self.expect_download(timeout_ms=int(kw.get("timeout_ms", 30_000)))
        if action == "fetch":
            return await self.fetch(kw["url"], headers=kw.get("headers"),
                                    timeout_ms=int(kw.get("timeout_ms", 30_000)))
        if action == "resolve_url":
            return await self.resolve_url(kw["url"], max_hops=int(kw.get("max_hops", 5)),
                                          timeout_ms=int(kw.get("timeout_ms", 15_000)))
        if action == "file_chooser":
            return await self.file_chooser(int(kw["index"]), list(kw.get("files") or []),
                                           names=list(kw.get("names") or []),
                                           mimes=list(kw.get("mimes") or []),
                                           timeout_ms=int(kw.get("timeout_ms", 15_000)))
        raise ActionError(f"unknown action: {action}")
