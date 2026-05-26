"""Built-in, list-consuming workflow: send LinkedIn connection requests.

Consumes a dataset of LinkedIn profiles (a ``profile_url`` per row — the exact
shape the *LinkedIn People* workflow outputs, so the two chain directly) and, one
profile at a time, human-paced, sends a connection request. Output = the input
list with a ``status`` (and ``detail``) column added.

Built to be resilient to everything a logged-in LinkedIn session does, learned by
driving real profiles by hand:

* **Drives via the engine's ``observe()``** (accessible names), which sees into
  iframes AND shadow DOM — the invite "Send without a note" dialog lives in a
  shadow root that main-frame JS can't reach. This is the load-bearing fix.
* **Bilingual** — the UI flips between English and Italian unpredictably, so every
  control is matched in both ("Connect"/"Collegati", "Send without a note"/"Invia
  senza nota", "Pending"/"In sospeso", "Follow"/"Segui", …).
* **Clicks the IN-CARD Connect, not the sticky-header one.** LinkedIn renders two
  duplicate action bars: the profile-card one (inside ``<main>``) and a floating
  sticky-header bar (outside ``<main>``). Clicking the sticky-header Connect pops
  a full-screen Premium upsell that cancels the invite; clicking the in-card one
  opens the "Send without a note" dialog directly. We scroll to top and select by
  ``<main>`` ancestry, so we always hit the real button — exactly what a human
  clicks. (If a Premium wall ever does surface, we dismiss + re-click to recover.)
* **Connect hidden behind Follow** → opens the profile-card "More" menu (also
  found by ``<main>`` ancestry, never the nav "Me" menu) and clicks the Connect
  item there. NEVER clicks Follow. No Connect anywhere → ``cannot_connect``.
  (Sending a request makes LinkedIn auto-follow the person; that is its own
  behaviour, not us clicking Follow.)
* Already connected (1st) → ``already_connected``; pending → ``already_pending``;
  verification wall → ``needs_verification``; weekly limit → ``limit_reached``
  (stops); bad/unavailable profile → ``unavailable``. After sending we dismiss the
  "verify to make a stronger impression" upsell (never click Verify).

One bad row never sinks the run. Runs on the authenticated profile
(``profiles/default``), standalone or attached to a control server.
"""
from __future__ import annotations

import asyncio
import random
import re

from automations import userkit

PROFILE_RE = re.compile(r"/in/([^/?#]+)", re.I)

# ---- bilingual (EN + IT) accessible-name patterns ----------------------------
RX_CONNECT      = re.compile(r"^(invite|invita)\b.*\b(to connect|a collegarsi|a entrare in contatto)$", re.I)
RX_CONNECT_WORD = re.compile(r"^(connect|collegati)$", re.I)
RX_PENDING      = re.compile(r"^(pending|in sospeso)\b", re.I)  # name is e.g. "Pending, click to withdraw…"
RX_FOLLOW       = re.compile(r"^(follow|segui)\b", re.I)
RX_MESSAGE      = re.compile(r"^(message|messaggio|invia messaggio)$", re.I)
RX_MORE         = re.compile(r"^(more|more actions|altro|altre azioni)$", re.I)
RX_SEND_NONOTE  = re.compile(r"^(send without a note|invia senza nota)$", re.I)
RX_SEND_NOW     = re.compile(r"^(send now|invia ora|send invitation|invia invito)$", re.I)
RX_SEND_BARE    = re.compile(r"^(send|invia)$", re.I)
RX_ADDNOTE      = re.compile(r"^(add a note|aggiungi nota|aggiungi una nota)$", re.I)
RX_NOTNOW       = re.compile(r"^(not now|non ora|got it|ho capito|maybe later|più tardi|skip|salta)$", re.I)
RX_DISMISS      = re.compile(r"^(dismiss|close|chiudi|ignora|annulla|cancel)$", re.I)
RX_REMOVE       = re.compile(r"(remove connection|rimuovi (il )?collegamento)", re.I)
RX_COOKIE       = re.compile(r"^(reject|reject all|rifiuta|rifiuta tutto|accept|accept all|accetta|accetta tutto|connect none|consenti tutti)$", re.I)


def _profile_url(row: dict) -> str:
    raw = ""
    for k in ("profile_url", "url", "linkedin_url", "linkedinUrl", "link", "profileUrl"):
        if row.get(k):
            raw = str(row[k]).strip()
            break
    if not raw:
        return ""
    if raw.startswith("/in/"):
        raw = "https://www.linkedin.com" + raw
    m = PROFILE_RE.search(raw)
    if m:
        return f"https://www.linkedin.com/in/{m.group(1)}/"
    if not raw.startswith(("http://", "https://")):
        return f"https://www.linkedin.com/in/{raw.strip('/')}/"
    return ""


# ---- main-frame page facts (owner, degree, full-page interstitials) ----------
_PAGE_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  const lines = (main.innerText || '').split('\n').map(s => s.replace(/\s+/g, ' ').trim()).filter(Boolean);
  const owner = lines[0] || '';
  let degree = '';
  for (const l of lines.slice(0, 7)) {
    const m = l.match(/[·•]\s*(1|2|3)\s*(?:st|nd|rd|th|°|er|°)?\b/i) || l.match(/\b(1st|2nd|3rd)\b/i);
    if (m) { const d = m[1][0]; degree = d === '1' ? '1st' : d === '2' ? '2nd' : '3rd'; break; }
  }
  const body = document.body.innerText || '';
  return {
    owner, degree,
    premium: /level up your career|Try .{0,12}Premium|Prova .{0,12}Premium|Reactivate Premium|Riattiva Premium|1 month of Premium|mese di Premium/i.test(body),
    sent: /invitation sent|invito inviato/i.test(body),
    verify: /Verify it'?s you|please enter your email|conferma la tua email|verifica la tua identità|verify your identity/i.test(body),
    limit: /reached the (weekly )?(invitation|invite) limit|no more invitations|hai raggiunto il limite|limite settimanale di inviti|nessun altro invito/i.test(body),
    unavailable: /this page doesn.?t exist|page isn.?t available|profile is not available|questa pagina non esiste|profilo non .{0,4}disponibile|pagina non trovata/i.test(body),
  };
}"""


# ---- observe helpers (frame + shadow-DOM aware) ------------------------------
async def _nodes(sess) -> list:
    try:
        ctx = await sess.observe()
        return getattr(ctx, "nodes", []) or []
    except Exception:
        return []


async def _page(sess) -> dict:
    try:
        d = await sess.evaluate(_PAGE_JS)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _find_all(nodes: list, rx: re.Pattern, *, owner: str | None = None, tag: str | None = None) -> list:
    ol = (owner or "").lower()
    hits = []
    for n in nodes:
        nm = (n.get("name") or "").strip()
        if not nm:
            continue
        if tag and n.get("tag") != tag:
            continue
        if owner and ol not in nm.lower():
            continue
        if rx.search(nm):
            hits.append(n)
    return hits


def _find(nodes: list, rx: re.Pattern, *, owner: str | None = None, tag: str | None = None):
    hits = _find_all(nodes, rx, owner=owner, tag=tag)
    return hits[0] if hits else None


def _center_y(n) -> float:
    c = n.get("center") or [0, 0]
    try:
        return float(c[1])
    except Exception:
        return 0.0


def _in_main(n) -> bool:
    """True if the node lives inside the profile's ``<main>`` (the real profile
    card). LinkedIn renders TWO duplicate action bars — the in-card one (under
    ``<main>``) and a floating sticky-header one (a fixed bar appended outside
    ``<main>``, which is what triggers the Premium upsell on Connect) — plus the
    nav "Me" menu (also outside ``<main>``). Index order between the duplicates
    is NOT stable across profiles, so the ``/main`` ancestry is the reliable way
    to pick the real card button and ignore the sticky/nav ones."""
    return "/main[" in (n.get("xpath") or "")


async def _scroll_top(sess) -> None:
    """Return to the top of the page so the sticky-header Connect (which triggers
    the Premium upsell) disappears and the in-card Connect is the target."""
    try:
        await sess.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        await sess.scroll(-4000)


async def _click(sess, n) -> bool:
    if not n:
        return False
    try:
        await sess.click(int(n["index"]))
        return True
    except Exception:
        return False


async def _is_pending(sess) -> bool:
    return _find(await _nodes(sess), RX_PENDING) is not None


async def _dismiss_verify(sess) -> None:
    """After an invite is sent LinkedIn shows a "verify to make a stronger
    impression" upsell — dismiss it (Not now), NEVER click Verify."""
    nn = _find(await _nodes(sess), RX_NOTNOW)
    if nn:
        await _click(sess, nn)
    else:
        await sess.press("Escape")


async def _dismiss_overlays(sess) -> None:
    """Clear cookie bar / EU services-consent / Premium upsell so actions aren't
    blocked. Bilingual, best-effort, idempotent."""
    for _ in range(3):
        p = await _page(sess)
        nodes = await _nodes(sess)
        cookie = _find(nodes, RX_COOKIE, tag="button")
        if cookie:
            await _click(sess, cookie)
            await sess.sleep(900)
            continue
        if p.get("premium"):
            d = _find(nodes, RX_DISMISS)
            if d:
                await _click(sess, d)
            else:
                await sess.press("Escape")
            await sess.sleep(800)
            continue
        return


async def _click_connect(sess, owner: str) -> bool:
    """Click the owner's IN-CARD Connect (accessible name 'Invite <owner> to
    connect' / 'Invita <owner> a collegarsi') — owner-scoped so sidebar/"people
    also viewed" connects can't be hit.

    There are two owner Connects once you scroll: the sticky-header one (y≈27,
    which pops the Premium upsell and cancels the invite) and the in-card one
    (large y, which opens the invite dialog directly). We scroll back to top
    (dissolving the sticky header) AND pick the largest-y match, so we always hit
    the real button — exactly what a human clicks."""
    await _scroll_top(sess)
    await sess.sleep(random.randint(250, 450))
    matches = _find_all(await _nodes(sess), RX_CONNECT, owner=owner)
    if not matches:
        return False
    # the real in-card Connect is under <main>; the sticky-header duplicate (which
    # pops the Premium upsell) is not. Prefer in-<main>, then visible, then top-most.
    in_main = [n for n in matches if _in_main(n)]
    pool = in_main or matches
    visible = [n for n in pool if n.get("inViewport")]
    pool = visible or pool
    pool.sort(key=_center_y)
    return await _click(sess, pool[-1])


def _profile_more(nodes: list, owner: str):
    """The PROFILE-card "More" (…) button — the one INSIDE ``<main>``, next to the
    owner's Message/Follow. The nav "Me" menu and the sticky-header "More" are
    outside ``<main>`` and are ignored (clicking the nav one opens the account
    menu, not the profile menu). Among in-card "More" buttons pick the one whose
    y is closest to the owner action cluster."""
    ol = (owner or "").lower()
    anchors = [
        n for n in nodes
        if _in_main(n) and (
            RX_MESSAGE.search(n.get("name") or "")
            or (RX_FOLLOW.search(n.get("name") or "") and ol in (n.get("name") or "").lower())
            or (RX_CONNECT.search(n.get("name") or "") and ol in (n.get("name") or "").lower())
        )
    ]
    mores = [
        n for n in nodes
        if RX_MORE.search((n.get("name") or "").strip()) and n.get("tag") == "button" and _in_main(n)
    ]
    if not mores:
        return None
    if anchors:
        ay = min(_center_y(a) for a in anchors)  # the card action row
        mores.sort(key=lambda m: abs(_center_y(m) - ay))
    return mores[0]


def _menu_connect(nodes: list, owner: str):
    """A Connect entry inside the open "More" dropdown — the owner-scoped invite,
    or a bare 'Connect'/'Collegati' menu item. The dropdown is a portal OUTSIDE
    ``<main>``, so we only accept a bare 'Connect' that isn't in-card/sidebar
    (sidebar 'Connect' buttons live under ``<main>``) — never invite the wrong
    person."""
    oc = _find(nodes, RX_CONNECT, owner=owner)
    if oc:
        return oc
    for n in nodes:
        nm = (n.get("name") or "").strip()
        if RX_CONNECT_WORD.search(nm) and not _in_main(n):
            return n
    return None


async def _open_more_and_connect(sess, owner: str) -> str:
    """Follow-primary profile: open the profile "More" menu and find Connect there.
    Returns 'connect' / 'already_connected' / '' (never follows). Opening the menu
    can need a beat (its items are injected late), so poll, and re-click once if
    the dropdown didn't take."""
    for click_attempt in range(2):
        more = _profile_more(await _nodes(sess), owner)
        if not await _click(sess, more):
            return ""
        for _ in range(5):
            await sess.sleep(500)
            nodes = await _nodes(sess)
            if _find(nodes, RX_REMOVE):
                await sess.press("Escape")
                return "already_connected"
            item = _menu_connect(nodes, owner)
            if item:
                if await _click(sess, item):
                    return "connect"
                break
    await sess.press("Escape")
    return ""


async def _try_invite(sess, owner: str, max_rounds: int = 3) -> str:
    """A Connect was just clicked. Drive the invite dialog (bilingual, shadow-DOM
    aware) to actually send. The invite dialog ("Send without a note") may take a
    moment to render, so we POLL for it rather than Escaping early. If it never
    shows, a Premium interstitial likely swallowed the click — recover by
    dismissing overlays and re-clicking Connect, then poll again. Never follows."""
    for rnd in range(max_rounds):
        for _ in range(8):  # ~6s of polling for the dialog / outcome
            await sess.sleep(700)
            nodes = await _nodes(sess)
            send = _find(nodes, RX_SEND_NONOTE) or _find(nodes, RX_SEND_NOW)
            if not send and _find(nodes, RX_ADDNOTE):  # bare Send only inside the dialog
                send = _find(nodes, RX_SEND_BARE)
            if send:
                await _click(sess, send)
                await sess.sleep(random.randint(1500, 2400))
                await _dismiss_verify(sess)
                return "sent"
            if _find(nodes, RX_PENDING):  # went through instantly / already pending
                await _dismiss_verify(sess)
                return "sent"
            p = await _page(sess)
            if p.get("sent"):
                await _dismiss_verify(sess)
                return "sent"
            if p.get("verify"):
                await sess.press("Escape")
                return "needs_verification"
            if p.get("limit"):
                await sess.press("Escape")
                return "limit_reached"

        # dialog never surfaced — recover through the intermittent Premium wall
        if rnd < max_rounds - 1:
            await _dismiss_overlays(sess)
            if not await _click_connect(sess, owner):
                if await _is_pending(sess):
                    return "sent"
                if await _open_more_and_connect(sess, owner) != "connect":
                    break

    if await _is_pending(sess):
        return "sent"
    return "blocked_premium" if (await _page(sess)).get("premium") else "connect_failed"


async def process_profile(sess, url: str) -> tuple[str, str, str]:
    await sess.goto(url)
    await sess.sleep(random.randint(1800, 2800))
    if not await sess.wait_for_selector("main", 12000):
        return "", "unavailable", "page did not load"
    # nudge lazy content to hydrate, then return to top: the in-card Connect (not
    # the sticky-header one, which triggers the Premium upsell) is what we want.
    await sess.scroll(random.randint(400, 700))
    await sess.sleep(random.randint(300, 500))
    await _scroll_top(sess)
    await sess.sleep(random.randint(300, 500))
    await _dismiss_overlays(sess)

    p = await _page(sess)
    owner = p.get("owner", "")
    if not owner:
        return "", "unavailable", "profile not readable"
    if p.get("unavailable"):
        return owner, "unavailable", "profile unavailable"
    if p.get("verify"):
        return owner, "needs_verification", "verification wall"
    if p.get("limit"):
        return owner, "limit_reached", "invite limit"
    if await _is_pending(sess):
        return owner, "already_pending", "invitation already pending"
    if p.get("degree") == "1st":
        return owner, "already_connected", "already a 1st-degree connection"

    # 1) primary Connect
    if await _click_connect(sess, owner):
        return owner, await _try_invite(sess, owner), ""
    # 2) Connect hidden in the "More" menu (follow-primary profiles)
    more = await _open_more_and_connect(sess, owner)
    if more == "already_connected":
        return owner, "already_connected", "already connected (menu)"
    if more == "connect":
        return owner, await _try_invite(sess, owner), "connect via More menu"
    # 3) no way to connect — never follow
    return owner, "cannot_connect", "no Connect option (follow-only or restricted)"


# ---- run ---------------------------------------------------------------------
async def run(params, sess, inputs):
    max_invites = int(params.get("maxInvites") or 0)  # 0 = no cap
    out, total, sent, stop = [], len(inputs), 0, False
    for i, row in enumerate(inputs, 1):
        url = _profile_url(row)
        name_in = str(row.get("name") or "").strip()
        if not url:
            out.append({"profile_url": str(row.get("profile_url") or ""), "name": name_in,
                        "status": "invalid_url", "detail": "no LinkedIn profile URL in row"})
            userkit.progress(i, total, message=f"{i}/{total} (no url)")
            continue
        if stop:
            out.append({"profile_url": url, "name": name_in, "status": "skipped", "detail": "stopped (limit)"})
            continue
        try:
            owner, status, detail = await process_profile(sess, url)
        except Exception as e:
            owner, status, detail = name_in, "error", str(e)[:160]
            userkit.log(f"[connections] {url} error: {e}")
        out.append({"profile_url": url, "name": owner or name_in, "status": status, "detail": detail})
        if status == "sent":
            sent += 1
        if status == "limit_reached":
            stop = True
        userkit.progress(i, total, message=f"{i}/{total} {owner or url} → {status}", url=url)
        if max_invites and sent >= max_invites:
            userkit.log(f"[connections] reached maxInvites={max_invites}; stopping")
            stop = True
        if i < total and not stop:
            await asyncio.sleep(random.uniform(3.0, 7.0))  # human pace between profiles
    return out


def main(argv=None):
    params, server, output = userkit.parse(argv)
    inputs = userkit.input_rows(argv)
    cols = ["profile_url", "name", "status", "detail"]
    if not inputs:
        userkit.error("no input rows — bind a dataset of LinkedIn profile URLs to this run")
        userkit.write_csv(output, [], cols)
        return 1
    rows = userkit.run_session(lambda p, s: run(p, s, inputs), params, server)
    userkit.write_csv(output, rows, cols)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
