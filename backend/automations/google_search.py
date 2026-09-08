"""Deterministic Google search automation.

Given a query it drives the human-grade browser as a person would — handling the
EU consent dialog, typing with human cadence, submitting and extracting the
organic results — and returns a structured, deterministic result set.

Runs identically whether it owns a local browser (standalone CLI) or attaches to
a control server managed by the console (`--server URL`). Emits structured run
events (see :mod:`automations._events`) so a console can show live progress.

Usage:
    python -m automations.google_search "best espresso machine under 500"
    python -m automations.google_search "claude ai" -n 10 --json --headed
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from dataclasses import asdict, dataclass
import re
from urllib.parse import quote_plus, urlsplit

from humanbrowser.session import open_session
from . import _events as ev

CONSENT_LABELS = ("Reject all", "Rifiuta tutto", "Accept all", "Accetta tutto")
_RESULTS_SEL = "#search a h3, #rso a h3"
_COUNT_JS = "() => document.querySelectorAll('#search a h3, #rso a h3').length"

_EXTRACT_JS = r"""(maxResults) => {
  const root = document.querySelector('#search') || document.querySelector('#rso') || document.body;
  const seen = new Set(); const results = [];
  const isGoogle = (h) => /(^|\.)google\.[a-z.]+$/i.test(h);
  // Google no longer puts the destination in the href: an organic result points at
  // Google's own /goto?url=<opaque token> tracker (the `ping` attribute is opaque
  // too). We therefore report the tracker as `redirect` and let the caller resolve
  // it. The visible <cite> is only a HINT — it is a breadcrumb, so it can be
  // elided ("…") or show labels rather than real path segments — so it is used for
  // the host, and as a URL only when the redirect cannot be resolved.
  const fromCite = (c) => {
    if (!c) return null;
    const cite = c.querySelector('cite'); if (!cite) return null;
    let t = (cite.innerText || '').trim().split('\n')[0].trim(); if (!t) return null;
    const elided = /[…]|\.\.\./.test(t);
    t = t.replace(/\s*[›>]\s*/g, '/').replace(/[…]|\.\.\./g, '').replace(/\/+$/, '');
    if (!/^https?:\/\//i.test(t)) t = 'https://' + t;
    try {
      const u = new URL(t);
      // a breadcrumb like "r/codex · 185,2k+ followers" parses as a URL but is not
      // a hostname — require a real dotted domain before trusting it
      if (!/^[a-z0-9-]+(\.[a-z0-9-]+)+$/i.test(u.hostname)) return null;
      if (isGoogle(u.hostname)) return null;
      return { url: u.href, host: u.hostname, elided };
    } catch (e) { return null; }
  };
  for (const h3 of root.querySelectorAll('a h3')) {
    if (results.length >= maxResults) break;
    const a = h3.closest('a'); if (!a) continue;
    const title = (h3.innerText || '').trim(); if (!title) continue;
    const c = h3.closest('div.g') || h3.closest('[data-hveid]') || a.parentElement;
    const href = a.href || '';
    const cited = fromCite(c);
    let direct = null;
    if (/^https?:/i.test(href)) {
      try { const u = new URL(href); if (!isGoogle(u.hostname)) direct = u; } catch (e) {}
    }
    let url = '', host = '', redirect = '';
    if (direct) {                        // classic markup: the href IS the target
      url = direct.href; host = direct.hostname;
    } else if (/^https?:/i.test(href)) { // tracker href → resolve it later
      redirect = href;
      if (cited) { url = cited.url; host = cited.host; }
    } else if (cited) {
      url = cited.url; host = cited.host;
    } else { continue; }
    const key = url || redirect;
    if (!key || seen.has(key)) continue;
    let snippet = '';
    if (c) {
      const sn = c.querySelector('div[data-sncf], .VwiC3b');
      snippet = sn ? sn.innerText.trim() : (c.innerText || '').replace(title, ' ').replace(/\s+/g, ' ').trim().slice(0, 320);
    }
    seen.add(key);
    results.push({ title, url, host, snippet: snippet.slice(0, 400), redirect });
  }
  return results;
}"""


@dataclass
class SearchResult:
    rank: int
    title: str
    url: str
    host: str
    snippet: str


async def _dismiss_consent(session) -> None:
    await session.sleep(800)
    ctx = await session.observe()
    if "consent" not in ctx.url and all(ctx.find(l) is None for l in CONSENT_LABELS):
        return
    for label in CONSENT_LABELS:
        idx = ctx.find(label, tag="button") or ctx.find(label)
        if idx is not None:
            await session.click(idx)
            await session.sleep(1200)
            return


async def _find_search_box(session) -> int:
    ctx = await session.observe()
    for finder in (lambda: ctx.find("Search", tag="textarea"),
                   lambda: ctx.find("Cerca", tag="textarea"),
                   lambda: next((e["index"] for e in ctx.elements if e["tag"] == "textarea"), None),
                   lambda: ctx.find("Search")):
        idx = finder()
        if idx is not None:
            return idx
    raise RuntimeError("could not locate Google search box")


async def _wait_results_settled(session, timeout: float = 20.0) -> int:
    if not await session.wait_for_selector(_RESULTS_SEL, int(timeout * 1000)):
        return 0
    last, stable, elapsed = -1, 0, 0.0
    while elapsed < timeout:
        n = await session.evaluate(_COUNT_JS)
        if n == last and n > 0:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last = n
        await session.sleep(400)
        elapsed += 0.4
    return last


_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", re.I)
_GOOGLE_HOST_RE = re.compile(r"(^|\.)google\.[a-z.]+$", re.I)


async def _resolve_tracked(session, raw: list[dict], budget: int) -> None:
    """Replace Google's tracking redirect with the page it actually points at.

    Google serves organic results as `/goto?url=<opaque>`, so the DOM no longer
    contains the destination. The visible <cite> is a *breadcrumb*: often the real
    path, but sometimes elided or made of labels, so trusting it alone yields
    confident-looking wrong URLs. Following the redirect is authoritative and
    costs one small request per result (headers only — no page download), so we
    do that whenever a tracker link exists and keep the cite purely as a fallback.
    """
    if budget <= 0 or not hasattr(session, "resolve_url"):
        return
    for r in raw[:max(0, budget)]:
        redirect = r.get("redirect")
        if not redirect:
            continue
        final = ""
        try:
            final = ((await session.resolve_url(redirect)) or {}).get("url") or ""
        except Exception:
            final = ""
        host = ""
        if final.startswith("http"):
            try:
                host = urlsplit(final).hostname or ""
            except ValueError:
                host = ""
        if host and _DOMAIN_RE.match(host) and not _GOOGLE_HOST_RE.search(host):
            r["url"], r["host"] = final, host
        elif not r.get("url"):
            # unresolvable and no usable cite — the tracker link still navigates
            r["url"] = redirect


async def search(query: str, *, num_results: int = 10, headless: bool = True,
                 humanize: bool = True, profile: str | None = None,
                 server: str | None = None) -> list[SearchResult]:
    session, _owns = open_session(headless=headless, humanize=humanize, profile=profile, server=server)
    await session.start()
    ev.status("running", workflow="google-search", query=query)
    try:
        # Page 1: a real human search (type the query + Enter).
        await session.goto("https://www.google.com/?hl=en")
        await _dismiss_consent(session)
        box = await _find_search_box(session)
        ev.progress(0, num_results, message=f"searching “{query}”", url=await session.current_url())
        await session.type(box, query, clear=True, enter=True)
        if not await _wait_results_settled(session):
            await session.press("Enter")
            await _wait_results_settled(session)

        # Then paginate (Google shows ~10 organic results per page) by navigating
        # to &start=10,20,… exactly as clicking "Next" does, accumulating unique
        # results until we have the requested number or run out of pages.
        results: list[SearchResult] = []
        seen: set[str] = set()
        max_pages = max(1, min(50, (num_results + 9) // 10 + 3))  # safety ceiling
        for page_i in range(max_pages):
            if page_i > 0:
                url = f"https://www.google.com/search?q={quote_plus(query)}&hl=en&start={page_i * 10}"
                await session.goto(url)
                if not await _wait_results_settled(session):
                    break  # no results on this page → end of results
            raw = await session.evaluate(_EXTRACT_JS, 50)  # all organic results on this page
            await _resolve_tracked(session, raw, num_results - len(results))
            new = 0
            for r in raw:
                url = r.get("url") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                results.append(SearchResult(rank=len(results) + 1, title=r.get("title", ""),
                                            url=url, host=r.get("host", ""),
                                            snippet=r.get("snippet", "")))
                new += 1
                if len(results) >= num_results:
                    break
            ev.progress(len(results), num_results, message=f"page {page_i + 1}",
                        url=await session.current_url(), page=page_i + 1)
            if len(results) >= num_results or new == 0:
                break  # got enough, or page added nothing new → end
            await session.sleep(random.randint(1500, 3500))  # be gentle between pages
        return results[:num_results]
    finally:
        await session.stop()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Autonomous, human-grade Google search")
    p.add_argument("query")
    p.add_argument("-n", "--num-results", type=int, default=10)
    p.add_argument("--headed", action="store_true")
    p.add_argument("--no-humanize", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("-o", "--output", default=None, help="write results as CSV to this path")
    p.add_argument("--profile", default=None)
    p.add_argument("--server", default=None, help="attach to a control server instead of launching a browser")
    args = p.parse_args(argv)

    try:
        results = asyncio.run(search(
            args.query, num_results=args.num_results, headless=not args.headed,
            humanize=not args.no_humanize, profile=args.profile, server=args.server,
        ))
    except Exception as e:
        ev.error(str(e))
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.output:
        import csv
        with open(args.output, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=["rank", "title", "url", "host", "snippet"])
            w.writeheader()
            for r in results:
                w.writerow(asdict(r))
        ev.result(args.output, len(results))

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False))
    elif not args.output:
        for r in results:
            print(f"{r.rank:>2}. {r.title}\n    {r.url}\n    [{r.host}] {r.snippet[:140]}\n")
    if args.output:
        print(f"Collected {len(results)} results -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
