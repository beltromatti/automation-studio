"""Deterministic, human-grade LinkedIn people-search automation.

Two modes, both driven on the authenticated persistent profile (`profiles/default`
— log in once by hand, no credentials in code):

* **short** — walk the people-search *result cards* only (no profile visits) and
  capture everything visible there: name, profile URL, connection degree,
  headline, location, "provides services" badge, extra line.
* **full** (default) — everything ``short`` collects, then open each person's
  profile **main page** and enrich/standardise the row with the richer data the
  page exposes (About, full location, current company, top education, connections
  & followers counts, open-to-work / verified / premium signals). One page load
  per profile, human-paced, so the account stays safe.

Targeting goes well beyond a single query: it accepts the same filters as
LinkedIn's real "All filters" people-search panel. Free-text / enum filters are
written straight into the search URL; entity filters that LinkedIn keys by
internal id (locations, industries) are resolved through the real autocomplete
UI (the typeahead the site itself uses), so we never hard-code brittle ids.

Runs standalone or attached to a console-managed control server (``--server URL``).
Emits structured run events for live progress.

Usage:
    python -m automations.linkedin_people "data scientist" --location "Milan" -n 25
    python -m automations.linkedin_people --current-title "Product Manager" \
        --current-company "Google" --connections "2nd,3rd" --mode full -n 40 -o leads.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import re
import sys
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from urllib.parse import urlencode, quote, urlparse, parse_qs

from humanbrowser.session import open_session
from . import _events as ev

PEOPLE_URL = "https://www.linkedin.com/search/results/people/"
COOKIE_LABELS = ("Reject", "Accept", "Rifiuta", "Accetta")
# connection-degree input ("1st,2nd,3rd") -> LinkedIn's network facet codes
NETWORK_CODES = {"1": "F", "1st": "F", "f": "F",
                 "2": "S", "2nd": "S", "s": "S",
                 "3": "O", "3rd": "O", "3rd+": "O", "o": "O"}

# ---------------------------------------------------------------- result cards (short)
_CARDS_JS = r"""(maxPerPage) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
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
    let name = nameAnchor ? clean(nameAnchor.innerText) : "";
    name = name.replace(degBullet, "").replace(/[•·]\s*$/, "").trim();
    const full = card.innerText || "";
    const dm = full.match(degBullet);
    const degree = dm ? ORD[dm[1]] + (dm[2] || "") : "";
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

# ---------------------------------------------------------------- profile main page (full)
_PROFILE_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  let lines = (main.innerText || '').split('\n').map(clean).filter(Boolean);
  // drop LinkedIn's global footer so it can't pollute About / chips parsing
  const FOOTER = /^(accessibility|talent solutions|community guidelines|user agreement|privacy policy|cookie policy|©\s*\d{4}|linkedin corporation|about\s+accessibility)/i;
  const fIdx = lines.findIndex(l => FOOTER.test(l));
  if (fIdx > 3) lines = lines.slice(0, fIdx);
  const flat = lines.join(' ');
  const SECTION = /^(about|informazioni|activity|attività|experience|esperienza|education|formazione|highlights|in evidenza|featured|skills|competenze|interests|recommendations|licenses|licenze|languages|lingue|volunteer|organizations|projects|honors|courses|people you may know|more profiles|explore)/i;
  const DEGREE = /^[·•]?\s*(1st|2nd|3rd|3rd\+|°)\s*\+?$/i;

  const name = lines[0] || '';
  // headline: first line after the name that isn't the connection-degree badge.
  let headline = '', hi = 1;
  while (lines[hi] && (DEGREE.test(lines[hi]) || lines[hi] === '·')) hi++;
  if (lines[hi] && !/^contact info$/i.test(lines[hi]) && !SECTION.test(lines[hi])) headline = lines[hi];

  // location: LinkedIn renders "<location> · Contact info"; take the line two
  // before "Contact info" when the line just before it is the bullet.
  let location = '';
  const ciIdx = lines.findIndex(l => /^contact info$|^informazioni di contatto$/i.test(l));
  if (ciIdx >= 2 && lines[ciIdx - 1] === '·') {
    const cand = lines[ciIdx - 2];
    if (cand && cand !== headline && !DEGREE.test(cand)) location = cand;
  }
  const contactInfo = ciIdx >= 0;

  // company / education chips shown right under the intro (between Contact info
  // and the connections/Connect controls).
  let currentCompany = '', education = '';
  if (ciIdx >= 0) {
    const chips = [];
    for (let i = ciIdx + 1; i < lines.length; i++) {
      const l = lines[i];
      if (/connection|collegament|follower|mutual|^connect$|^message$|^segui$|^messaggio$|^collegati$|^iscriviti$|^pending$|^follow$/i.test(l)) break;
      if (l === '·' || SECTION.test(l)) break;
      if (/^[\d.,]+\+?$/.test(l)) break;  // a bare number = the connections count starting
      chips.push(l);
      if (chips.length >= 2) break;
    }
    currentCompany = chips[0] || '';
    education = chips[1] || '';
  }

  const cm = flat.match(/([\d][\d.,]*\+?)\s*(connections|collegament)/i);
  const connections = cm ? cm[1] : '';
  const fm = flat.match(/([\d][\d.,]*)\s*(followers|follower)/i);
  const followers = fm ? fm[1] : '';

  // About: text between a standalone "About" heading and the next section.
  let about = '';
  const aIdx = lines.findIndex(l => /^about$|^informazioni$/i.test(l));
  if (aIdx >= 0) {
    const buf = [];
    for (let i = aIdx + 1; i < lines.length; i++) {
      if (SECTION.test(lines[i])) break;
      if (/^…?see more$|^mostra altro$|^…$/i.test(lines[i])) continue;
      buf.push(lines[i]);
      if (buf.join(' ').length > 4000) break;
    }
    about = buf.join(' ').trim();
  }

  const openToWork = /#opentowork|provides services|open to work/i.test(flat) ||
                     !!main.querySelector('img[title*="#OPEN_TO_WORK" i], [class*="open-to-work" i]');
  const verified = !!main.querySelector('[data-test-icon*="verified" i], svg#verified-medium, [aria-label*="verified" i]');
  const premium = !!main.querySelector('li-icon[type*="premium" i], img[alt*="Premium" i]');

  return { name, headline, location, currentCompany, education, connections, followers,
           about, contactInfo, openToWork, verified, premium };
}"""


@dataclass
class Person:
    rank: int
    name: str
    profile_url: str
    degree: str
    headline: str
    location: str
    connections: str = ""
    followers: str = ""
    current_company: str = ""
    education: str = ""
    about: str = ""
    open_to_work: str = ""
    verified: str = ""
    premium: str = ""
    contact_info: str = ""
    services: str = ""
    extra: str = ""


# ------------------------------------------------------------------ URL building
def _norm_list(s: str | None) -> list[str]:
    if not s:
        return []
    return [p.strip() for p in str(s).split(",") if p.strip()]


def build_search_url(*, keywords: str = "", current_title: str = "", first_name: str = "",
                     last_name: str = "", current_company: str = "", school: str = "",
                     connections: str = "", profile_languages: str = "",
                     geo_urn: list[str] | None = None, industry: list[str] | None = None) -> str:
    """Assemble a LinkedIn people-search URL from the free-text / enum filters plus
    any already-resolved entity ids (geoUrn, industry)."""
    q: dict[str, str] = {}
    if keywords:
        q["keywords"] = keywords
    if current_title:
        q["titleFreeText"] = current_title
    if first_name:
        q["firstName"] = first_name
    if last_name:
        q["lastName"] = last_name
    if current_company:
        q["company"] = current_company
    if school:
        q["schoolFreetext"] = f'"{school}"'
    net = [NETWORK_CODES[c.lower()] for c in _norm_list(connections) if c.lower() in NETWORK_CODES]
    if net:
        q["network"] = "[" + ",".join(f'"{c}"' for c in dict.fromkeys(net)) + "]"
    langs = _norm_list(profile_languages)
    if langs:
        q["profileLanguage"] = "[" + ",".join(f'"{c}"' for c in langs) + "]"
    if geo_urn:
        q["geoUrn"] = "[" + ",".join(f'"{i}"' for i in geo_urn) + "]"
    if industry:
        q["industry"] = "[" + ",".join(f'"{i}"' for i in industry) + "]"
    q["origin"] = "FACETED_SEARCH"
    return PEOPLE_URL + "?" + urlencode(q, quote_via=quote)


# ------------------------------------------------------------------ entity typeahead
async def _open_all_filters(session) -> bool:
    ok = await session.evaluate(
        "() => { const b=[...document.querySelectorAll('button')].find(x=>/all filters|tutti i filtri/i.test(x.innerText)); if(b){b.click(); return true;} return false; }")
    await session.sleep(1500)
    return bool(ok)


async def _add_entity(session, add_label_re: str, value: str) -> bool:
    """Click an 'Add a X' button in the open filter panel, type ``value`` and pick
    the first autocomplete suggestion. Returns True if a suggestion was chosen."""
    clicked = await session.evaluate(
        "(re) => { const dlg=document.querySelector('[role=dialog]')||document; "
        "const b=[...dlg.querySelectorAll('button')].find(x=>new RegExp(re,'i').test(x.innerText)); "
        "if(b){b.click(); return true;} return false; }", add_label_re)
    if not clicked:
        return False
    await session.sleep(900)
    # the entity typeaheads are comboboxes (aria-autocomplete) — distinct from the
    # plain free-text Keywords inputs; type into the most-recently-revealed one.
    typed = await session.evaluate(
        "(v) => { const dlg=document.querySelector('[role=dialog]')||document; "
        "const c=[...dlg.querySelectorAll('input[aria-autocomplete], input[role=combobox]')]; "
        "const inp=c[c.length-1]; if(!inp) return false; inp.focus(); "
        "const set=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set; set.call(inp,v); "
        "inp.dispatchEvent(new Event('input',{bubbles:true})); "
        "const last=v.slice(-1); "
        "inp.dispatchEvent(new KeyboardEvent('keydown',{key:last,bubbles:true})); "
        "inp.dispatchEvent(new KeyboardEvent('keyup',{key:last,bubbles:true})); return true; }", value)
    if not typed:
        return False
    for _ in range(24):  # wait for the async typeahead suggestions, then pick the first
        await session.sleep(500)
        picked = await session.evaluate(
            "() => { const o=document.querySelector('[role=option], [role=listbox] li'); "
            "if(o){o.click(); return true;} return false; }")
        if picked:
            await session.sleep(900)
            return True
    return False


async def resolve_entities(session, locations: list[str], industries: list[str], seed: str = "") -> dict:
    """Resolve location/industry free text to LinkedIn ids by driving the real
    'All filters' typeahead, then reading the ids back out of the resulting URL.
    Best-effort: anything that can't be resolved is simply skipped."""
    if not locations and not industries:
        return {}
    # a populated search makes the filter bar (and its "All filters" button) reliable
    seed = (seed or "people").strip()
    await session.goto(PEOPLE_URL + "?keywords=" + quote(seed) + "&origin=FACETED_SEARCH")
    await session.sleep(3000)
    await _dismiss_cookie_banner(session)
    if not await _open_all_filters(session):
        ev.log("[filters] could not open the All filters panel; skipping location/industry")
        return {}
    added = 0
    for loc in locations:
        if await _add_entity(session, r"add a location|aggiungi una localit", loc):
            added += 1
            ev.log(f"[filters] location resolved: {loc}")
        else:
            ev.log(f"[filters] location not found: {loc}")
    for ind in industries:
        if await _add_entity(session, r"add an industry|aggiungi un settore", ind):
            added += 1
            ev.log(f"[filters] industry resolved: {ind}")
        else:
            ev.log(f"[filters] industry not found: {ind}")
    if not added:
        return {}
    await session.evaluate(
        "() => { const b=[...document.querySelectorAll('[role=dialog] button')].find(x=>/show results|mostra risultati|visualizza risultati/i.test(x.innerText)); if(b)b.click(); }")
    await session.sleep(4000)
    url = await session.current_url()
    qs = parse_qs(urlparse(url).query)
    out: dict[str, list[str]] = {}
    for param in ("geoUrn", "industry"):
        if param in qs and qs[param]:
            try:
                out[param] = [str(x) for x in json.loads(qs[param][0])]
            except Exception:
                pass
    return out


# ------------------------------------------------------------------ navigation helpers
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


async def _load_results(session, timeout_ms: int = 15000) -> bool:
    if not await session.wait_for_selector(_RESULTS_SEL, timeout_ms):
        return False
    for _ in range(4):
        await session.scroll(random.randint(500, 800))
        await session.sleep(random.randint(250, 500))
    await session.sleep(random.randint(500, 900))
    return True


async def _enrich_profile(session, person: Person) -> None:
    """Open the profile main page and merge the richer fields onto ``person``.
    Tolerant: whatever LinkedIn doesn't expose is just left blank."""
    if not person.profile_url:
        return
    await session.goto(person.profile_url)
    await session.sleep(random.randint(1800, 2800))
    if not await session.wait_for_selector("main", 12000):
        return
    for _ in range(3):  # nudge so the lazy About/intro blocks render
        await session.scroll(random.randint(500, 800))
        await session.sleep(random.randint(250, 450))
    try:
        d = await session.evaluate(_PROFILE_JS)
    except Exception:
        return
    if not isinstance(d, dict):
        return
    if d.get("name"):
        person.name = d["name"] or person.name
    if d.get("headline"):
        person.headline = d["headline"]
    if d.get("location"):
        person.location = d["location"]  # main-page location is richer than the card's
    person.current_company = d.get("currentCompany", "") or person.current_company
    person.education = d.get("education", "")
    person.connections = d.get("connections", "")
    person.followers = d.get("followers", "")
    person.about = d.get("about", "")
    person.contact_info = "yes" if d.get("contactInfo") else ""
    person.open_to_work = "yes" if d.get("openToWork") else ""
    person.verified = "yes" if d.get("verified") else ""
    person.premium = "yes" if d.get("premium") else ""


# ------------------------------------------------------------------ main scrape
async def scrape_people(*, keywords: str = "", current_title: str = "", first_name: str = "",
                        last_name: str = "", current_company: str = "", school: str = "",
                        locations: str = "", industries: str = "", connections: str = "",
                        profile_languages: str = "", mode: str = "full", limit: int = 25,
                        headless: bool = True, humanize: bool = True, profile: str | None = None,
                        server: str | None = None,
                        page_delay: tuple[float, float] = (2.5, 5.0),
                        profile_delay: tuple[float, float] = (2.0, 4.5)) -> list[Person]:
    mode = (mode or "full").lower().strip()
    session, _owns = open_session(headless=headless, humanize=humanize, profile=profile, server=server)
    await session.start()
    ev.status("running", workflow="linkedin-people", query=keywords or current_title or "(filters)")

    people: list[Person] = []
    seen: set[str] = set()
    try:
        # 1) resolve entity filters (location/industry) via the real autocomplete
        resolved = await resolve_entities(session, _norm_list(locations), _norm_list(industries),
                                          seed=keywords or current_title or current_company)
        # graceful fallback: if a requested location couldn't be resolved to an id,
        # fold it into the fuzzy keywords so location targeting still happens.
        if _norm_list(locations) and not resolved.get("geoUrn"):
            keywords = " ".join(x for x in [keywords, *_norm_list(locations)] if x).strip()
            ev.log(f"[filters] location not resolved to an id — using it as fuzzy keywords")
        base_url = build_search_url(
            keywords=keywords, current_title=current_title, first_name=first_name, last_name=last_name,
            current_company=current_company, school=school, connections=connections,
            profile_languages=profile_languages, geo_urn=resolved.get("geoUrn"), industry=resolved.get("industry"))
        ev.log(f"[search] {base_url}")

        # 2) collect result cards across pages (short data for everyone)
        max_pages = min(100, (limit // 10) + 5)
        for page_num in range(1, max_pages + 1):
            url = base_url + (f"&page={page_num}" if page_num > 1 else "")
            await session.goto(url)
            if page_num == 1:
                await _dismiss_cookie_banner(session)
            if not await _load_results(session):
                break
            raw = await session.evaluate(_CARDS_JS, 10)
            new = 0
            for r in raw:
                key = r["url"] or f'{r["name"]}|{r["headline"]}'
                if not key or key in seen:
                    continue
                seen.add(key)
                people.append(Person(
                    rank=len(people) + 1, name=r["name"], profile_url=r["url"], degree=r["degree"],
                    headline=r["headline"], location=r["location"], services=r["services"], extra=r["extra"]))
                new += 1
                if len(people) >= limit:
                    break
            ev.progress(len(people), limit, message=f"results page {page_num}",
                        url=await session.current_url(), page=page_num)
            if len(people) >= limit or new == 0:
                break
            await asyncio.sleep(random.uniform(*page_delay))

        # 3) full mode: enrich each person from their profile main page (human-paced)
        if mode == "full" and people:
            for i, person in enumerate(people, 1):
                await _enrich_profile(session, person)
                ev.progress(i, len(people), message=f"enriched {i}/{len(people)}: {person.name}",
                            url=person.profile_url)
                if i < len(people):
                    await asyncio.sleep(random.uniform(*profile_delay))
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


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "linkedin"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Human-grade LinkedIn people-search automation -> CSV")
    p.add_argument("query", nargs="?", default="", help="general keywords (fuzzy)")
    p.add_argument("--current-title", default="", help="current job title (free text)")
    p.add_argument("--first-name", default="")
    p.add_argument("--last-name", default="")
    p.add_argument("--current-company", default="", help="current company (free text)")
    p.add_argument("--school", default="", help="school (free text)")
    p.add_argument("--location", "--locations", dest="locations", default="",
                   help="comma-separated locations (resolved via LinkedIn autocomplete)")
    p.add_argument("--industries", default="", help="comma-separated industries (resolved via autocomplete)")
    p.add_argument("--connections", default="", help="connection degrees, e.g. '1st,2nd,3rd'")
    p.add_argument("--profile-languages", default="", help="profile languages, e.g. 'en,it'")
    p.add_argument("--mode", choices=["short", "full"], default="full")
    p.add_argument("-n", "--limit", type=int, default=25)
    p.add_argument("-o", "--output", default=None)
    p.add_argument("--headed", action="store_true")
    p.add_argument("--no-humanize", action="store_true")
    p.add_argument("--profile", default=None)
    p.add_argument("--server", default=None, help="attach to a control server instead of launching a browser")
    args = p.parse_args(argv)

    if not any([args.query, args.current_title, args.first_name, args.last_name,
                args.current_company, args.school, args.locations, args.industries,
                args.connections, args.profile_languages]):
        ev.error("provide at least a query or one filter")
        print("ERROR: provide at least a query or one filter", file=sys.stderr)
        return 2

    try:
        people = asyncio.run(scrape_people(
            keywords=args.query, current_title=args.current_title, first_name=args.first_name,
            last_name=args.last_name, current_company=args.current_company, school=args.school,
            locations=args.locations, industries=args.industries, connections=args.connections,
            profile_languages=args.profile_languages, mode=args.mode, limit=args.limit,
            headless=not args.headed, humanize=not args.no_humanize, profile=args.profile, server=args.server,
        ))
    except Exception as e:
        ev.error(str(e))
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    out = Path(args.output) if args.output else Path(f"{_slug(args.query or args.current_title)}.csv")
    write_csv(people, out)
    ev.result(str(out.resolve()), len(people))
    print(f"Collected {len(people)} profiles ({args.mode}) -> {out.resolve()}")
    for p_ in people[:5]:
        print(f"  {p_.rank:>2}. {p_.name} — {p_.headline} [{p_.location}] {p_.profile_url}")
    if len(people) > 5:
        print(f"  … and {len(people) - 5} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
