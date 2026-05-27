"""Built-in, list-consuming workflow: send a LinkedIn message to each CONNECTION.

Consumes a dataset of LinkedIn profiles (a ``profile_url`` per row — the exact
shape the *LinkedIn People* / *LinkedIn Connections* workflows output, so the three
chain directly) and, one profile at a time, human-paced, sends a direct message —
**but only to people you are actually 1st-degree connected to**. Accepts one message
or a list of messages to alternate (round-robin across the messages actually sent).
Output = the input list with a ``status`` (and ``detail``) column added.

Built to be resilient to everything a logged-in LinkedIn session does, learned by
driving real profiles by hand (the same way [[linkedin_connections]] was):

* **Connection-gated.** We message ONLY confirmed 1st-degree connections. The
  presence of a "Message" button is NOT proof of connection — LinkedIn shows a
  Message button on many non-connections too (InMail / open-profile), and messaging
  those would be the wrong thing. The reliable 1st-degree signal is the visible
  "· 1st" distance badge in the top card (LinkedIn only renders that badge line for
  1st-degree), backed up by the owner-name aria-label's trailing degree. Anything
  not clearly 1st → ``not_connection`` (or ``pending`` when a pending invite shows).
* **Clicks the IN-CARD Message, not the sticky-header one.** Like Connect, LinkedIn
  renders two duplicate action bars — the profile-card one (inside ``<main>``) and a
  floating sticky-header bar (outside ``<main>``). We scroll to top and select by
  ``<main>`` ancestry so we always hit the real card button.
* **Drives the shadow-DOM message overlay via the engine's ``observe()``** (frame +
  shadow aware, accessible names) — the compose bubble lives in a shadow root that
  main-frame JS can't reach. The compose box is a ``role=textbox`` contenteditable;
  the Send button is disabled when empty.
* **Closes every open conversation bubble first.** LinkedIn persists open message
  bubbles across page loads and stacks them (a leftover "The LinkedIn Team" bubble,
  group bubbles, …). We clear them before each profile so the bubble we then open is
  unambiguously the target's — which also avoids confusing two people with similar
  names — and verify the open bubble carries the owner's name before typing.
* **Bilingual** (EN + IT): the UI flips between English and Italian unpredictably.

**Success detection:** after Send, the compose empties → the Send button goes back to
*disabled*, and a brand-new draft transitions from "Close your draft conversation"
(with a recipient combobox) to a real "Close your conversation with <owner>". We poll
for either signal. One bad row never sinks the run. Runs on the authenticated profile
(``profiles/default``), standalone or attached to a control server.
"""
from __future__ import annotations

import asyncio
import json
import random
import re

from automations import userkit

PROFILE_RE = re.compile(r"/in/([^/?#]+)", re.I)

# ---- bilingual (EN + IT) accessible-name patterns ----------------------------
RX_MESSAGE     = re.compile(r"^(message|messaggio|invia messaggio)$", re.I)
RX_PENDING     = re.compile(r"^(pending|in sospeso)\b", re.I)
RX_SEND        = re.compile(r"^(send|invia)$", re.I)
RX_CLOSE_CONV  = re.compile(r"(close your (draft )?conversation|chiudi (la )?conversazione|chiudi la bozza)", re.I)
RX_COOKIE      = re.compile(r"^(reject|reject all|rifiuta|rifiuta tutto|accept|accept all|accetta|accetta tutto|consenti tutti)$", re.I)
RX_DISMISS     = re.compile(r"^(dismiss|close|chiudi|ignora|annulla|cancel)$", re.I)


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


def _messages(params: dict) -> list[str]:
    """Resolve the message(s) to send. ``messages`` (a JSON array, or items separated
    by ``||`` or a ``---`` line) takes precedence and is alternated round-robin; else
    the single ``message``. Whitespace/newlines are collapsed so a stray newline can't
    submit the compose early."""
    msgs: list[str] = []
    raw = params.get("messages")
    if raw:
        s = str(raw).strip()
        if s[:1] == "[":
            try:
                msgs = [str(x) for x in json.loads(s)]
            except (ValueError, TypeError):
                msgs = []
        if not msgs:
            msgs = re.split(r"\s*\|\|\s*|\n?-{3,}\n?", s)
    if not msgs and params.get("message"):
        msgs = [str(params["message"])]
    return [re.sub(r"\s+", " ", m).strip() for m in msgs if m and m.strip()]


# ---- main-frame page facts (owner, degree, interstitials) --------------------
_PAGE_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  const lines = (main.innerText || '').split('\n').map(s => s.replace(/\s+/g, ' ').trim()).filter(Boolean);
  const owner = lines[0] || '';
  const MAP = {'1': '1st', '2': '2nd', '3': '3rd'};
  let degree = '';
  // (a) the standalone visible distance badge near the name. LinkedIn renders this
  // line ("· 1st" / "· 1°") for 1st-degree connections; it's the reliable signal.
  for (const l of lines.slice(0, 6)) {
    let m = l.match(/^[·•]\s*(1st|2nd|3rd)(\+)?$/i) || l.match(/^[·•]\s*([123])(\s*°|\+)?$/);
    if (m) {
      const base = /^[123]$/.test(m[1]) ? MAP[m[1]] : m[1].toLowerCase();
      degree = base + (m[2] === '+' ? '+' : '');
      break;
    }
  }
  // (b) fallback: the owner-name link/heading aria-label ends with the degree
  // (e.g. "Anna Bernbaum Verified Profile 3rd+").
  if (!degree && owner) {
    const ol = owner.toLowerCase().slice(0, 10);
    for (const el of main.querySelectorAll('[aria-label]')) {
      const t = (el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
      if (!t || !t.toLowerCase().startsWith(ol)) continue;
      const m = t.match(/\b(1st|2nd|3rd)(\+)?\s*$/i);
      if (m) { degree = m[1].toLowerCase() + (m[2] || ''); break; }
    }
  }
  const btns = [...main.querySelectorAll('button, a')]
    .map(b => (b.getAttribute('aria-label') || b.innerText || '').replace(/\s+/g, ' ').trim());
  const body = document.body.innerText || '';
  return {
    owner, degree,
    isFirst: degree === '1st',
    pending: btns.some(b => /^(pending|in sospeso)\b/i.test(b)),
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


def _in_main(n) -> bool:
    return "/main[" in (n.get("xpath") or "")


def _in_overlay(n) -> bool:
    """The message overlay is a shadow root (frame contains '/shadow'); the profile's
    light-DOM sidebars (also <aside>s, full of names) are NOT — this excludes them."""
    return "/shadow" in (n.get("frame") or "")


def _center_y(n) -> float:
    c = n.get("center") or [0, 0]
    try:
        return float(c[1])
    except Exception:
        return 0.0


async def _click(sess, n) -> bool:
    if not n:
        return False
    try:
        await sess.click(int(n["index"]))
        return True
    except Exception:
        return False


def _find(nodes: list, rx: re.Pattern, *, pred=None):
    for n in nodes:
        nm = (n.get("name") or "").strip()
        if nm and rx.search(nm) and (pred is None or pred(n)):
            return n
    return None


async def _scroll_top(sess) -> None:
    try:
        await sess.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        await sess.scroll(-4000)


async def _dismiss_overlays(sess) -> None:
    """Clear cookie bar / EU consent / generic upsell so the action bar is clickable."""
    for _ in range(3):
        nodes = await _nodes(sess)
        cookie = _find(nodes, RX_COOKIE, pred=lambda n: n.get("tag") == "button")
        if cookie:
            await _click(sess, cookie)
            await sess.sleep(800)
            continue
        return


async def _close_all_bubbles(sess, rounds: int = 8) -> None:
    """Close every open message overlay bubble (they persist across page loads and
    stack), so the next Message click yields exactly one unambiguous bubble."""
    for _ in range(rounds):
        closers = [n for n in await _nodes(sess) if _in_overlay(n) and RX_CLOSE_CONV.search(n.get("name") or "")]
        if not closers:
            return
        await _click(sess, closers[0])
        await sess.sleep(600)


def _bubble_of(xp: str) -> str | None:
    m = re.search(r"(aside\[\d+\]/div\[\d+\])", xp or "")
    return m.group(1) if m else None


def _overlay_textboxes(nodes: list) -> list:
    return [n for n in nodes if _in_overlay(n) and (n.get("attrs") or {}).get("role") == "textbox"]


def _owner_compose(nodes: list, owner: str):
    """Pick the compose textbox of the bubble that belongs to ``owner``. With bubbles
    cleared first there is normally exactly one; if several are open we choose the one
    whose bubble carries the owner's (full) name, so similar names can't cross over."""
    boxes = _overlay_textboxes(nodes)
    if not boxes:
        return None, None
    ol = " ".join((owner or "").lower().split())
    if len(boxes) == 1 and _bubble_owns(nodes, _bubble_of(boxes[0].get("xpath")), ol):
        b = _bubble_of(boxes[0].get("xpath"))
        return boxes[0], b
    for box in boxes:
        b = _bubble_of(box.get("xpath"))
        if b and _bubble_owns(nodes, b, ol):
            return box, b
    # single box but owner-name not yet rendered (brand-new draft still settling):
    # accept the lone box.
    if len(boxes) == 1:
        return boxes[0], _bubble_of(boxes[0].get("xpath"))
    return None, None


def _bubble_owns(nodes: list, bubble: str | None, owner_lc: str) -> bool:
    if not bubble or not owner_lc:
        return False
    for n in nodes:
        if _bubble_of(n.get("xpath")) == bubble and owner_lc in (n.get("name") or "").lower():
            return True
    return False


def _send_button(nodes: list, bubble: str | None):
    """The Send button INSIDE a specific overlay bubble (one per bubble). Strictly
    bubble-scoped so a leftover bubble's (always-disabled) Send can never be picked."""
    if not bubble:
        return None
    for n in nodes:
        if _in_overlay(n) and _bubble_of(n.get("xpath")) == bubble and RX_SEND.search((n.get("name") or "").strip()):
            return n
    return None


def _bubble_has_combobox(nodes: list, bubble: str | None) -> bool:
    """A recipient combobox in this bubble ⇒ it's a brand-new DRAFT (no history yet)."""
    return any(_in_overlay(n) and (n.get("attrs") or {}).get("role") == "combobox"
               and _bubble_of(n.get("xpath")) == bubble for n in nodes)


def _is_disabled(n) -> bool:
    a = n.get("attrs") or {}
    return bool(a.get("disabled")) or str(a.get("aria-disabled")).lower() == "true"


# ---- send to one connection --------------------------------------------------
async def _send_message(sess, owner: str, message: str) -> tuple[str, str]:
    """Open the in-card Message compose for ``owner`` and send ``message``.
    Returns (status, detail). Assumes the caller verified 1st-degree."""
    await _scroll_top(sess)
    await sess.sleep(random.randint(250, 450))
    await _close_all_bubbles(sess)

    # click the IN-CARD Message (top-most in-<main> match; sidebar people have no
    # Message, and the sticky-header duplicate is outside <main>).
    msg_btns = [n for n in await _nodes(sess) if RX_MESSAGE.search((n.get("name") or "").strip()) and _in_main(n)]
    if not msg_btns:
        return "not_messageable", "no in-card Message button"
    msg_btns.sort(key=_center_y)
    if not await _click(sess, msg_btns[0]):
        return "not_messageable", "could not click Message"

    # poll for the compose bubble to render (shadow DOM, can take a beat)
    box = bubble = None
    for _ in range(10):
        await sess.sleep(500)
        box, bubble = _owner_compose(await _nodes(sess), owner)
        if box:
            break
    if not box:
        return "not_messageable", "compose box did not open"

    # type into the compose textbox (Playwright pierces shadow DOM by index)
    try:
        await sess.type(int(box["index"]), message)
    except Exception as e:
        return "message_failed", f"type failed: {str(e)[:80]}"
    await sess.sleep(random.randint(700, 1100))

    # the Send button must now be ENABLED (typing un-disables it). Re-find it strictly
    # within the OWNER's bubble (indices shift after typing; never a leftover bubble).
    # Refuse to click a still-disabled Send (text didn't register).
    nodes = await _nodes(sess)
    _, bubble2 = _owner_compose(nodes, owner)
    bubble = bubble2 or bubble
    was_draft = _bubble_has_combobox(nodes, bubble)  # brand-new conversation?
    send = _send_button(nodes, bubble)
    if send is None:
        return "message_failed", "no Send button in the conversation"
    if _is_disabled(send):
        await sess.sleep(900)  # one more beat for the editor to register the text
        nodes = await _nodes(sess)
        _, bubble2 = _owner_compose(nodes, owner)
        bubble = bubble2 or bubble
        was_draft = was_draft or _bubble_has_combobox(nodes, bubble)
        send = _send_button(nodes, bubble)
        if send is None or _is_disabled(send):
            return "message_failed", "Send stayed disabled (text not registered)"
    if not await _click(sess, send):
        return "message_failed", "could not click Send"

    # confirm: the OWNER bubble's Send goes back to DISABLED (compose cleared = sent) —
    # the universal signal for both new and existing conversations. For a brand-new
    # draft we ALSO accept the draft→real transition (recipient combobox gone + a real
    # "Close your conversation with <owner>" header) as confirmation. We never use the
    # transition alone for existing chats (their header pre-exists → false positive).
    ol = " ".join(owner.lower().split())
    for _ in range(12):
        await sess.sleep(600)
        nodes = await _nodes(sess)
        _, b2 = _owner_compose(nodes, owner)
        if b2:
            s = _send_button(nodes, b2)
            if s is not None and _is_disabled(s):
                return "sent", ""
        if was_draft:
            real = [_bubble_of(n.get("xpath")) for n in nodes if _in_overlay(n)
                    and ol in (n.get("name") or "").lower()
                    and re.search(r"close your conversation with|conversazione con", n.get("name") or "", re.I)]
            if real and not any(_bubble_has_combobox(nodes, b) for b in real):
                return "sent", ""
    return "message_failed", "could not confirm send"


async def process_profile(sess, url: str, message: str) -> tuple[str, str, str]:
    await sess.goto(url)
    await sess.sleep(random.randint(1800, 2800))
    if not await sess.wait_for_selector("main", 12000):
        return "", "unavailable", "page did not load"
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
    # connection gate — only 1st-degree connections get a message
    if p.get("pending"):
        return owner, "pending", "invitation pending — not a connection yet"
    if not p.get("isFirst"):
        deg = p.get("degree") or "not 1st-degree"
        return owner, "not_connection", f"not a 1st-degree connection ({deg})"

    status, detail = await _send_message(sess, owner, message)
    # tidy up: close the bubble we just used so bubbles don't accumulate over the run
    await _close_all_bubbles(sess, rounds=3)
    return owner, status, detail


# ---- run ---------------------------------------------------------------------
async def run(params, sess, inputs):
    messages = _messages(params)
    if not messages:
        userkit.error("no message — provide a 'message' (or a 'messages' list to alternate)")
        return [{"profile_url": _profile_url(r) or str(r.get("profile_url") or ""),
                 "name": str(r.get("name") or ""), "status": "error", "detail": "no message configured"}
                for r in inputs]
    max_messages = int(params.get("maxMessages") or 0)  # 0 = no cap
    out, total, sent, stop = [], len(inputs), 0, False
    userkit.log(f"[messages] {total} rows · {len(messages)} message variant(s)"
                f"{' · alternating' if len(messages) > 1 else ''}"
                f"{f' · cap {max_messages}' if max_messages else ''}")
    for i, row in enumerate(inputs, 1):
        url = _profile_url(row)
        name_in = str(row.get("name") or "").strip()
        if not url:
            out.append({"profile_url": str(row.get("profile_url") or ""), "name": name_in,
                        "status": "invalid_url", "detail": "no LinkedIn profile URL in row"})
            userkit.progress(i, total, message=f"{i}/{total} (no url)")
            continue
        if stop:
            out.append({"profile_url": url, "name": name_in, "status": "skipped", "detail": "stopped (cap reached)"})
            continue
        message = messages[sent % len(messages)]  # alternate across messages actually sent
        try:
            owner, status, detail = await process_profile(sess, url, message)
        except Exception as e:
            owner, status, detail = name_in, "error", str(e)[:160]
            userkit.log(f"[messages] {url} error: {e}")
        out.append({"profile_url": url, "name": owner or name_in, "status": status, "detail": detail})
        if status == "sent":
            sent += 1
        userkit.progress(i, total, message=f"{i}/{total} {owner or url} → {status}", url=url)
        if max_messages and sent >= max_messages:
            userkit.log(f"[messages] reached maxMessages={max_messages}; stopping")
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
