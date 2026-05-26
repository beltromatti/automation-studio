"""Turn a live page into a compact, navigable text snapshot for LLM agents.

The snapshot interleaves indexed interactive elements with the visible text
around them in document order, so a non-vision model can reason about the page
the way a person reading it would. Each interactive element carries a stable
``[index]`` that the action API uses to target it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from patchright.async_api import Page

_JS = (Path(__file__).resolve().parent / "dom_collect.js").read_text()


@dataclass
class PageContext:
    url: str
    title: str
    scroll_y: int
    scroll_height: int
    inner_height: int
    has_more_below: bool
    num_elements: int
    truncated: bool
    nodes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def elements(self) -> list[dict[str, Any]]:
        return [n for n in self.nodes if n.get("type") == "element"]

    def element(self, index: int) -> dict[str, Any] | None:
        for n in self.elements:
            if n.get("index") == index:
                return n
        return None

    def find(self, name_substr: str, *, tag: str | None = None) -> int | None:
        """Return the index of the first interactive element whose accessible
        name contains ``name_substr`` (case-insensitive). Lets scripts and agents
        target elements by what they read, not by brittle selectors."""
        needle = name_substr.lower()
        for n in self.elements:
            if tag and n.get("tag") != tag:
                continue
            if needle in (n.get("name") or "").lower():
                return n["index"]
        return None

    def find_all(self, name_substr: str, *, tag: str | None = None) -> list[int]:
        needle = name_substr.lower()
        out = []
        for n in self.elements:
            if tag and n.get("tag") != tag:
                continue
            if needle in (n.get("name") or "").lower():
                out.append(n["index"])
        return out

    def _fmt_element(self, n: dict[str, Any]) -> str:
        a = n.get("attrs", {})
        parts = []
        for key in ("type", "role", "href", "value", "placeholder", "expanded", "selected", "checked", "disabled"):
            if key in a:
                v = a[key]
                if v is True:
                    parts.append(key)
                else:
                    parts.append(f'{key}="{v}"')
        attr_str = (" " + " ".join(parts)) if parts else ""
        name = n.get("name", "")
        marker = "" if n.get("inViewport", True) else " (offscreen)"
        body = f" {name}" if name else ""
        return f'[{n["index"]}]<{n["tag"]}{attr_str}>{body}{marker}'

    def to_text(self, max_chars: int = 14_000) -> str:
        """Render the snapshot as an LLM-friendly outline."""
        header = (
            f"URL: {self.url}\nTITLE: {self.title}\n"
            f"SCROLL: y={self.scroll_y} of {self.scroll_height} (viewport {self.inner_height}px)"
            f"{'  [more content below — scroll to reveal]' if self.has_more_below else '  [bottom of page]'}\n"
            f"INTERACTIVE ELEMENTS: {self.num_elements}{' (truncated)' if self.truncated else ''}\n"
            "--- page ---\n"
        )
        lines: list[str] = []
        for n in self.nodes:
            if n.get("type") == "element":
                lines.append(self._fmt_element(n))
            else:
                lines.append(n.get("text", ""))
        body = "\n".join(l for l in lines if l)
        if len(body) > max_chars:
            body = body[:max_chars] + "\n… [snapshot truncated]"
        return header + body

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "scroll_y": self.scroll_y,
            "scroll_height": self.scroll_height,
            "inner_height": self.inner_height,
            "has_more_below": self.has_more_below,
            "num_elements": self.num_elements,
            "truncated": self.truncated,
            "elements": self.elements,
            # full ordered snapshot (interactive elements interleaved with text);
            # additive — `elements` stays the element-only list for existing callers.
            "nodes": self.nodes,
        }


async def collect(page: "Page", *, max_nodes: int = 1200) -> PageContext:
    raw = await page.evaluate(_JS, {"maxNodes": max_nodes})
    return PageContext(
        url=raw["url"],
        title=raw["title"],
        scroll_y=raw["scrollY"],
        scroll_height=raw["scrollHeight"],
        inner_height=raw["innerHeight"],
        has_more_below=raw["hasMoreBelow"],
        num_elements=raw["numElements"],
        truncated=raw["truncated"],
        nodes=raw["nodes"],
    )
