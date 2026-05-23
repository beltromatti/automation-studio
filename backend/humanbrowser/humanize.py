"""Human-like interaction primitives.

These helpers add the small imperfections a real person produces: curved mouse
paths, variable typing cadence, eased scrolling and short "think" pauses. They
run identically in headed and headless mode, so a remote site sees the same
behavioural signature either way.
"""
from __future__ import annotations

import asyncio
import math
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchright.async_api import Page


async def think(min_s: float = 0.25, max_s: float = 0.9) -> None:
    """Pause for a short, human-plausible amount of time."""
    await asyncio.sleep(random.uniform(min_s, max_s))


def _bezier_point(p0, p1, p2, p3, t):
    mt = 1 - t
    x = (mt**3) * p0[0] + 3 * (mt**2) * t * p1[0] + 3 * mt * (t**2) * p2[0] + (t**3) * p3[0]
    y = (mt**3) * p0[1] + 3 * (mt**2) * t * p1[1] + 3 * mt * (t**2) * p2[1] + (t**3) * p3[1]
    return x, y


async def move_mouse(page: "Page", x: float, y: float, *, start: tuple | None = None) -> None:
    """Move the cursor to (x, y) along a randomized cubic Bezier curve.

    Playwright's ``mouse.move`` jumps in a straight line; real cursors arc and
    vary in speed. We sample a curve with two random control points and emit a
    realistic, distance-dependent number of intermediate moves.
    """
    sx, sy = start if start else (random.uniform(0, 200), random.uniform(0, 200))
    dist = math.hypot(x - sx, y - sy)
    # control points offset perpendicular-ish from the straight line
    c1 = (sx + (x - sx) * 0.3 + random.uniform(-60, 60), sy + (y - sy) * 0.3 + random.uniform(-60, 60))
    c2 = (sx + (x - sx) * 0.7 + random.uniform(-60, 60), sy + (y - sy) * 0.7 + random.uniform(-60, 60))
    steps = max(8, min(40, int(dist / 12)))
    for i in range(1, steps + 1):
        t = i / steps
        # ease-in-out so the cursor accelerates then decelerates
        te = 0.5 - 0.5 * math.cos(math.pi * t)
        px, py = _bezier_point((sx, sy), c1, c2, (x, y), te)
        await page.mouse.move(px, py)
        await asyncio.sleep(random.uniform(0.004, 0.016))


async def click_at(page: "Page", x: float, y: float) -> None:
    """Move to a point then click with a natural press duration."""
    await move_mouse(page, x, y)
    await asyncio.sleep(random.uniform(0.03, 0.12))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.04, 0.11))
    await page.mouse.up()


async def type_text(page: "Page", text: str, *, mistakes: bool = True) -> None:
    """Type text key-by-key with human cadence and occasional corrections.

    Cadence varies per character, with longer pauses after spaces/punctuation
    and a small chance of a typo that is immediately backspaced and fixed.
    """
    kb = page.keyboard
    for ch in text:
        if mistakes and ch.isalpha() and random.random() < 0.015:
            wrong = random.choice("asdfghjklqwertyuiop")
            await kb.type(wrong)
            await asyncio.sleep(random.uniform(0.08, 0.2))
            await kb.press("Backspace")
            await asyncio.sleep(random.uniform(0.05, 0.15))
        await kb.type(ch)
        delay = random.uniform(0.05, 0.16)
        if ch in " ":
            delay += random.uniform(0.04, 0.12)
        elif ch in ".,?!":
            delay += random.uniform(0.08, 0.2)
        if random.random() < 0.03:  # occasional brief hesitation
            delay += random.uniform(0.25, 0.7)
        await asyncio.sleep(delay)


async def scroll_by(page: "Page", dy: int, *, steps: int = 8) -> None:
    """Scroll vertically in eased increments rather than one jump."""
    remaining = dy
    for i in range(steps):
        frac = (math.sin((i + 1) / steps * math.pi / 2)) - (math.sin(i / steps * math.pi / 2))
        delta = int(dy * frac)
        remaining -= delta
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(random.uniform(0.03, 0.09))
    if remaining:
        await page.mouse.wheel(0, remaining)
