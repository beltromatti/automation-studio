"""Deterministic LinkedIn people-search automation.

Given a query it walks LinkedIn's *people* search results (the public cards,
without opening profiles) and writes an ordered CSV with one row per person and
every field visible on that screen: name, profile URL, connection degree,
headline, location, "provides services" badge and any extra line.

Runs on the authenticated persistent profile (`profiles/default`) — log in once
by hand, no credentials in code. Works standalone or attached to a console-managed
control server (`--server URL`). Emits structured run events for live progress.

Usage:
    python -m automations.linkedin_people "software engineer milano" --limit 50
    python -m automations.linkedin_people "growth marketing" -n 30 --headed -o leads.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import random
import re
import sys
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from urllib.parse import quote_plus

from humanbrowser.session import open_session
from . import _events as ev

PEOPLE_URL = "https://www.linkedin.com/search/results/people/?keywords={kw}"
COOKIE_LABELS = ("Reject", "Accept", "Rifiuta", "Accetta")

_EXTRACT_JS = r"""(maxPerPage) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const degRe = /(1st|2nd|3rd)\s*\+?/i;
  // Container-agnostic: LinkedIn renders results as <ul><li> OR as
  // <div role="list"><div role="listitem">. Pick whichever container holds the
  // most profile links, then take its item children as cards.
  const itemsOf = (ct) => {
    let it = [...ct.querySelectorAll(':scope > li, :scope > [role="listitem"]')];
    if (!it.length) it = [...ct.querySelectorAll('[role="listitem"]')];
    if (!it.length) it = [...ct.querySelectorAll(':scope > li')];
    return it;
  };
  const containers = [...document.querySelectorAll('ul, [role="list"]')];
  let best = null, bestN = 0;
  for (const ct of containers) {
    const n = itemsOf(ct).filter((it) => it.querySelector('a[href*="/in/"]')).length;
    if (n > bestN) { bestN = n; best = ct; }
  }
  if (!best) return [];
  // degree badge like "• 3°+" (IT) or "• 3rd+" (EN), bullet-anchored so we don't
  // mistake "3rd party" inside a headline for a connection degree.
  const degBullet = /[•·]\s*([123])\s*(?:°|st|nd|rd|th)\s*(\+)?/i;
  const ORD = { "1": "1st", "2": "2nd", "3": "3rd" };
  const DROP = /^(collegati|connetti|connect|segui|following|follow|messaggio|message|invia messaggio|iscriviti|join|status is offline|visualizza profilo|view profile)$/i;
  const SERVICES = /^(provides services|offre servizi)\s*[-:]?\s*(.*)$/i;

  const out = [];
  for (const card of itemsOf(best).filter((it) => it.querySelector('a[href*="/in/"]'))) {
    const anchors = [...card.querySelectorAll('a[href*="/in/"]')];
    if (!anchors.length) continue;
    let nameAnchor = null;
    for (const a of anchors) {
      if (a.querySelector('img')) continue;
      const t = clean(a.innerText);
      if (!t || /^(provides services|offre servizi)/i.test(t)) continue;
      nameAnchor = a; break;
    }
    const hrefSrc = (nameAnchor || anchors[0]).href;
    const m = hrefSrc.match(/\/in\/([^/?#]+)/);
    const url = m ? "https://www.linkedin.com/in/" + decodeURIComponent(m[1]) : "";
    // name: anchor text, with any trailing "• 3°+" degree badge stripped
    let name = nameAnchor ? clean(nameAnchor.innerText) : "";
    name = name.replace(degBullet, "").replace(/[•·]\s*$/, "").trim();

    const full = card.innerText || "";
    const dm = full.match(degBullet);
    const degree = dm ? ORD[dm[1]] + (dm[2] || "") : "";

    // Layout-independent text parse: card.innerText reads the same human-visible
    // lines whether LinkedIn renders <ul><li> with <div> subtitles or
    // role=list with everything wrapped in spans inside the anchor.
    let services = "";
    const info = [];
    const seen = new Set();
    for (const raw of full.split("\n")) {
      let s = clean(raw);
      if (!s) continue;
      if (name) s = s.split(name).join(" ");
      s = s.replace(degBullet, " ").replace(/^[•·\s]+/, "").replace(/\s+/g, " ").trim();
      if (!s || DROP.test(s)) continue;
      const sm = s.match(SERVICES);
      if (sm) { services = sm[2] || services; continue; }
      const key = s.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      info.push(s);
    }
    if (!name && !url) continue;
    out.push({ name, url, degree, headline: info[0] || "", location: info[1] || "", services, extra: info.slice(2).join(" | ") });
    if (out.length >= maxPerPage) break;
  }
  return out;
}"""


@dataclass
class Person:
    rank: int
    name: str
    profile_url: str
    degree: str
    headline: str
    location: str
    services: str
    extra: str


async def _dismiss_cookie_banner(session) -> None:
    try:
        ctx = await session.observe()
    except Exception:
        return
    for label in COOKIE_LABELS:
        idx = ctx.find(label, tag="button")
        if idx is not None:
            try:
                await session.click(idx)
                await session.sleep(800)
            except Exception:
                pass
            return


_RESULTS_SEL = '[role="list"] a[href*="/in/"], ul li a[href*="/in/"]'


async def _load_page(session, timeout_ms: int = 15000) -> bool:
    if not await session.wait_for_selector(_RESULTS_SEL, timeout_ms):
        return False
    for _ in range(4):
        await session.scroll(random.randint(500, 800))
    await session.sleep(random.randint(600, 1100))
    return True


async def scrape_people(query: str, *, limit: int = 50, headless: bool = True,
                        humanize: bool = True, profile: str | None = None,
                        server: str | None = None,
                        page_delay: tuple[float, float] = (2.5, 5.0)) -> list[Person]:
    session, _owns = open_session(headless=headless, humanize=humanize, profile=profile, server=server)
    await session.start()
    ev.status("running", workflow="linkedin-people", query=query)

    people: list[Person] = []
    seen: set[str] = set()
    max_pages = min(100, (limit // 10) + 5)
    try:
        for page_num in range(1, max_pages + 1):
            url = PEOPLE_URL.format(kw=quote_plus(query))
            if page_num > 1:
                url += f"&page={page_num}"
            await session.goto(url)
            if page_num == 1:
                await _dismiss_cookie_banner(session)
                if "search/results/people" not in (await session.current_url()):
                    await session.goto(url)

            if not await _load_page(session):
                break

            raw = await session.evaluate(_EXTRACT_JS, 10)
            new = 0
            for r in raw:
                key = r["url"] or f'{r["name"]}|{r["headline"]}'
                if not key or key in seen:
                    continue
                seen.add(key)
                people.append(Person(
                    rank=len(people) + 1, name=r["name"], profile_url=r["url"],
                    degree=r["degree"], headline=r["headline"], location=r["location"],
                    services=r["services"], extra=r["extra"],
                ))
                new += 1
                if len(people) >= limit:
                    break
            ev.progress(len(people), limit, message=f"page {page_num}", url=await session.current_url(), page=page_num)
            if len(people) >= limit or new == 0:
                break
            await asyncio.sleep(random.uniform(*page_delay))
        return people
    finally:
        await session.stop()


def write_csv(people: list[Person], path: Path) -> None:
    cols = [f.name for f in fields(Person)]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for p in people:
            w.writerow(asdict(p))


def _slug(query: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-") or "linkedin"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Deterministic LinkedIn people-search automation -> CSV")
    p.add_argument("query")
    p.add_argument("-n", "--limit", type=int, default=50)
    p.add_argument("-o", "--output", default=None)
    p.add_argument("--headed", action="store_true")
    p.add_argument("--no-humanize", action="store_true")
    p.add_argument("--profile", default=None)
    p.add_argument("--server", default=None, help="attach to a control server instead of launching a browser")
    args = p.parse_args(argv)

    try:
        people = asyncio.run(scrape_people(
            args.query, limit=args.limit, headless=not args.headed,
            humanize=not args.no_humanize, profile=args.profile, server=args.server,
        ))
    except Exception as e:
        ev.error(str(e))
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    out = Path(args.output) if args.output else Path(f"{_slug(args.query)}.csv")
    write_csv(people, out)
    ev.result(str(out.resolve()), len(people))
    print(f"Collected {len(people)} profiles -> {out.resolve()}")
    for p_ in people[:5]:
        print(f"  {p_.rank:>2}. {p_.name} — {p_.headline} [{p_.location}] {p_.profile_url}")
    if len(people) > 5:
        print(f"  … and {len(people) - 5} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
