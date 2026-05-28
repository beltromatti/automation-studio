"""Built-in, list-consuming workflow: create a text POST in each Reddit community.

Consumes a dataset of Reddit communities (a ``community`` per row in any common
format — ``r/learnpython``, ``/r/learnpython``, ``learnpython``, a community URL
like ``https://www.reddit.com/r/learnpython/``, or any URL whose path contains
``/r/<name>``) and, one community at a time, human-paced, opens that community's
text-submit page and publishes a post.

Message resolution mirrors LinkedIn Messages exactly (so the two feel like one
family), in priority order:

  1. A per-row ``message`` column on the input dataset (personalised per
     community — overrides everything).
  2. A list of ``messages`` (param) to alternate round-robin across the
     fallback posts actually published (a per-row message doesn't consume an
     alternation slot).
  3. A single ``message`` param.

The message itself becomes the post's title + body, picked to read like a real
human post:

* If the message contains a blank-line break (``\\n\\n``), the part before
  becomes the title and the rest the body — the explicit "I know what I want
  the title to be" path.
* Else if the whole message is short (≤280 chars on one line), it's used as
  the title alone (a clean title-only post, exactly how a human writes a
  one-liner).
* Else the first sentence (≤280 chars) is the title and the full message is
  the body — so a longer paragraph stays readable on the feed.

Hard-won realities, learned by submitting real posts by hand:

* **Submit page is plain DOM** (no shadow). Title is a ``<textarea>`` with
  accessible name "title". The body is a ``<div role="textbox">``
  (contenteditable) with accessible name "Post body text field". Submit is a
  ``<button>`` named "Post", starts ``disabled`` and becomes enabled once a
  title is present (the body can stay empty for a title-only post).
* **Success signal is a URL transition.** After Send, Reddit navigates AWAY
  from ``/submit/`` — typically straight to the new post at
  ``/r/<community>/comments/<id>/<slug>/``, from which we return ``post_url``.
  For the special "personal subreddit" case (``/r/u_<user>/submit/`` →
  ``/user/<user>/submitted/?sort=hot``) we read the first
  ``/user/<user>/comments/<id>/<slug>/`` link in the resulting feed as the
  ``post_url`` fallback. Staying on ``/submit/`` after ~10s means Reddit
  refused the submit (we then read the page text to classify why).
* **Bad / banned / private / quarantined community** → the ``/submit/`` URL
  either redirects to a login or shows an interstitial; we detect via the
  resulting URL and the page text and report ``community_not_found`` or
  ``community_restricted`` without ever typing the message.
* **Flair-required subs** → the Post button can stay ``disabled`` even after
  filling title + body. When that happens we report ``needs_flair`` (instead
  of clicking a disabled button) so the user knows this row needs manual flair
  picking.
* **Rate limit / forbidden** → Reddit shows inline text after the click
  ("you are doing that too much…", "you don't have permission…"). We catch
  the common patterns and either stop the run (``rate_limited``) or mark the
  single row ``not_postable`` and carry on.

One bad row never sinks the run. Runs on a Reddit-authenticated profile (the
declared default is ``c9c42d740f`` — the "second" profile, where the dev
Reddit account is logged in), standalone or attached to a control server.
"""
from __future__ import annotations

import asyncio
import json
import random
import re

from automations import userkit

# Reddit subreddit name rules: 3-21 chars, alphanumeric + underscores, first char
# alphanumeric. Greedy enough to catch URLs, strict enough to reject `u/foo`.
COMMUNITY_RE = re.compile(r"(?:^|/)r/([A-Za-z0-9][A-Za-z0-9_]{2,20})(?=/|$|\?|#)", re.I)
BARE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{2,20}$")

SUBMIT_URL = "https://www.reddit.com/r/{}/submit/?type=TEXT"

# Where the post lives after a successful submit. Two shapes — a regular sub
# (``/r/<sub>/comments/<id>/<slug>/``) or the user's personal sub
# (``/user/<user>/comments/<id>/<slug>/``).
POST_URL_RE = re.compile(
    r"https?://(?:www\.|old\.)?reddit\.com(/(?:r|user)/[^/]+/comments/[^/]+/[^/?#]+)",
    re.I,
)
POST_HREF_RE = re.compile(r"^/(?:r|user)/[^/]+/comments/[^/]+/[^/?#]+/?", re.I)

# Title cap on the new Reddit submit form. We stay one char under to leave room
# for a trailing ellipsis when we have to truncate.
MAX_TITLE = 300

# ---- inline page-text patterns (Reddit's submit UI; English-only in 2026) -----
RX_NO_EXIST = re.compile(
    r"(community doesn'?t exist|community (could not be|not) found"
    r"|there aren'?t any communities on reddit with that name"
    r"|page not found|sorry,? nobody on reddit goes by that name)",
    re.I,
)
RX_RESTRICTED = re.compile(
    r"(private community|restricted community|quarantined|community has been (banned|removed|made private)"
    r"|you need to be (an? approved (member|user)|invited)"
    r"|join this community to post)",
    re.I,
)
RX_RATELIMIT = re.compile(
    r"(you'?re? doing that too much"
    r"|rate ?limit"
    r"|try again in \d+ (second|minute|hour)"
    r"|slow down"
    r"|too many requests)",
    re.I,
)
RX_FORBIDDEN = re.compile(
    r"(you don'?t have permission to post"
    r"|posting is restricted"
    r"|cannot post|can'?t post"
    r"|requires? (a )?minimum (karma|account|age)"
    r"|account is too new"
    r"|not enough karma"
    r"|you are banned from"
    r"|posting privileges (have been )?suspended"
    r"|community requires"
    r"|please verify your email)",
    re.I,
)
RX_NEEDS_FLAIR = re.compile(
    r"(please (select|add|choose) (a )?(post )?flair"
    r"|flair (is )?required"
    r"|posts must (have|include) (a )?flair)",
    re.I,
)


# ---- input normalisation -----------------------------------------------------
def _community(row: dict) -> str:
    """Extract the canonical community name from a row, accepting many input
    formats. Returns the bare name (``learnpython``) or ``''`` when nothing
    recognisable is found (we never silently default to a fallback community)."""
    raw = ""
    for k in ("community", "subreddit", "r", "name", "url", "link", "communityUrl", "subredditUrl"):
        v = row.get(k)
        if v is not None and str(v).strip():
            raw = str(v).strip()
            break
    if not raw:
        return ""
    m = COMMUNITY_RE.search(raw)
    if m:
        return m.group(1)
    name = raw.strip().strip("/")
    if name.lower().startswith(("u/", "user/")):
        return ""  # the workflow posts to communities, not user profiles via DM
    if BARE_NAME_RE.match(name):
        return name
    return ""


def _messages(params: dict) -> list[str]:
    """Resolve the fallback message(s). ``messages`` (a JSON array, or items
    separated by ``||`` or a ``---`` line) wins and is alternated round-robin;
    otherwise the single ``message``. We strip surrounding whitespace only —
    embedded newlines stay (the body field preserves them and that's the right
    rendering for a Reddit post)."""
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
    return [m.strip() for m in msgs if m and m.strip()]


def _row_message(row: dict) -> str:
    """A per-recipient message carried on the input row (a ``message`` column on
    the dataset). When present it OVERRIDES both the ``message`` and ``messages``
    params, so each community can get a bespoke post."""
    for k in ("message", "messaggio", "msg", "body", "post"):
        if row.get(k) and str(row[k]).strip():
            return str(row[k]).strip()
    return ""


def _title_body(msg: str) -> tuple[str, str]:
    """Split a free-form message into (title, body) the way a human would post.

    * A blank line (``\\n\\n``) is taken as an explicit title/body separator.
    * A short (≤280 chars) single-line message becomes the title alone — a
      clean title-only post.
    * Otherwise the first sentence (≤280 chars) is the title and the full
      message is the body, so a longer paragraph stays readable on the feed.
    """
    msg = (msg or "").strip()
    if not msg:
        return "Hello", ""
    if "\n\n" in msg:
        head, tail = msg.split("\n\n", 1)
        title = " ".join(head.split())
        body = tail.strip()
        if len(title) > MAX_TITLE - 1:
            title = title[: MAX_TITLE - 2].rstrip() + "…"
        return title or "Hello", body
    if "\n" not in msg and len(msg) <= 280:
        return msg, ""
    first = re.split(r"(?<=[.!?…])\s+", msg, maxsplit=1)[0]
    # title MUST be a single line — Reddit's title is a single-line slug — so
    # collapse any whitespace (incl. newlines) in the chosen first sentence.
    title = " ".join(first.split())
    if len(title) > MAX_TITLE - 1:
        title = title[: MAX_TITLE - 2].rstrip() + "…"
    return title or "Hello", msg


# ---- page facts (one round-trip into the page) -------------------------------
# Accepts an optional ``titleHint`` so the same call also locates the *user's*
# new-post URL on a feed page that includes other posts (community sticky/highlights
# in regular subs, or the user's own posts in the personal-sub feed). The hint is
# matched against the anchor's visible text — Reddit renders the post title
# verbatim in the feed so this is robust even when slugs strip punctuation.
_PAGE_JS = r"""(titleHint) => {
  const body = (document.body && document.body.innerText) || '';
  const url = location.href;
  const norm = (s) => (s || '').toLowerCase().replace(/\s+/g, ' ').trim();
  const want = norm(titleHint).slice(0, 60);
  const POST_HREF = /^\/(r|user)\/[^/]+\/comments\/[^/]+\/[^/?#]+/i;
  let firstPostHref = '';
  let myPostHref = '';
  for (const a of document.querySelectorAll('a[href]')) {
    const h = a.getAttribute('href') || '';
    if (!POST_HREF.test(h)) continue;
    if (!firstPostHref) firstPostHref = h;
    if (want) {
      const t = norm(a.innerText);
      if (t && t.startsWith(want.slice(0, Math.min(40, want.length)))) {
        myPostHref = h;
        break;
      }
    }
  }
  return {
    url,
    is_submit: /\/submit\/?($|\?|#)/i.test(url),
    is_login: /\/login\/?($|\?|#)/i.test(url) || /\/register\/?($|\?|#)/i.test(url),
    is_post: /\/comments\/[^/]+\/[^/?#]+/i.test(url),
    first_post_href: firstPostHref,
    my_post_href: myPostHref,
    no_exist: /(community doesn'?t exist|community (could not be|not) found|there aren'?t any communities on reddit with that name|page not found|sorry,? nobody on reddit goes by that name)/i.test(body),
    restricted: /(private community|restricted community|quarantined|community has been (banned|removed|made private)|you need to be (an? approved (member|user)|invited)|join this community to post)/i.test(body),
    ratelimit: /(you'?re? doing that too much|rate ?limit|try again in \d+ (second|minute|hour)|slow down|too many requests)/i.test(body),
    forbidden: /(you don'?t have permission to post|posting is restricted|cannot post|can'?t post|requires? (a )?minimum (karma|account|age)|account is too new|not enough karma|you are banned from|posting privileges (have been )?suspended|community requires|please verify your email)/i.test(body),
    needs_flair: /(please (select|add|choose) (a )?(post )?flair|flair (is )?required|posts must (have|include) (a )?flair)/i.test(body),
  };
}"""


# ---- observe helpers ---------------------------------------------------------
async def _nodes(sess) -> list:
    try:
        ctx = await sess.observe()
        return getattr(ctx, "nodes", []) or []
    except Exception:
        return []


async def _page(sess, title: str = "") -> dict:
    """Read page facts. ``title`` (the post title we just typed) lets the same
    call also locate *our* new-post URL on a feed page where other posts may sit
    above it (community sticky/highlights, the user-feed sort order, etc.)."""
    try:
        d = await sess.evaluate(_PAGE_JS, title)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _attrs(n) -> dict:
    return n.get("attrs") or {}


def _is_disabled(n) -> bool:
    a = _attrs(n)
    return bool(a.get("disabled")) or str(a.get("aria-disabled")).lower() == "true"


def _name(n) -> str:
    return (n.get("name") or "").strip()


def _find_form(nodes: list) -> dict:
    """Identify the three submit-form controls by their accessible-name shape
    (which is stable across communities; observe gives us the label as ``name``).

    * Title — a ``<textarea>`` whose name is "title" (the form label).
    * Body  — a ``<div role="textbox">`` whose name is "Post body text field".
    * Post  — a ``<button>`` whose name is exactly "Post" (rejects "Save Draft"
      and the per-tool toolbar buttons).

    Returns dict of {title, body, post} (any value may be None if not yet
    rendered)."""
    title = next((n for n in nodes if n.get("tag") == "textarea"
                  and _name(n).lower() == "title"), None)
    body = next((n for n in nodes if n.get("tag") == "div"
                 and _attrs(n).get("role") == "textbox"
                 and "post body" in _name(n).lower()), None)
    post = next((n for n in nodes if n.get("tag") == "button"
                 and _name(n).lower() == "post"), None)
    return {"title": title, "body": body, "post": post}


def _extract_post_url(p: dict) -> str:
    """The canonical post URL after a successful submit, in this order:
    (a) the current URL if it's already a post URL; (b) ``my_post_href`` — the
    anchor matched by the title text (robust against community sticky-posts in
    regular subs); (c) ``first_post_href`` as a last-resort fallback. We never
    return a URL that doesn't conform to the ``/comments/<id>/<slug>`` shape."""
    url = p.get("url") or ""
    m = POST_URL_RE.search(url)
    if m:
        return f"https://www.reddit.com{m.group(1)}/"
    for key in ("my_post_href", "first_post_href"):
        href = p.get(key) or ""
        if POST_HREF_RE.match(href):
            return f"https://www.reddit.com{href.rstrip('/').split('?')[0].split('#')[0]}/"
    return ""


# ---- post to one community ---------------------------------------------------
async def process_community(sess, community: str, message: str) -> tuple[str, str, str]:
    """Open the submit page for ``community`` and publish ``message`` as a
    text post. Returns (status, post_url, detail)."""
    target = SUBMIT_URL.format(community)
    try:
        await sess.goto(target)
    except Exception as e:
        return "unavailable", "", f"goto failed: {str(e)[:120]}"
    await sess.sleep(random.randint(1500, 2800))

    p0 = await _page(sess)
    if p0.get("is_login"):
        return "unavailable", "", "not logged in to Reddit"
    if not p0.get("is_submit") and not p0.get("is_post"):
        # redirected somewhere unexpected — classify by page text first
        if p0.get("no_exist"):
            return "community_not_found", "", "Reddit: community doesn't exist"
        if p0.get("restricted"):
            return "community_restricted", "", "Reddit: private / restricted / quarantined / banned"
        return "unavailable", "", f"submit redirected to {p0.get('url', '')[:120]}"
    if p0.get("no_exist"):
        return "community_not_found", "", "Reddit: community doesn't exist"
    if p0.get("restricted"):
        return "community_restricted", "", "Reddit: private / restricted / quarantined / banned"

    # Poll for the form to render BOTH a title textarea AND a Post button. New
    # Reddit is an SPA, so the previous page's textarea can briefly linger in
    # the DOM during transition — a stricter "form is ready" predicate avoids
    # falsely thinking the form rendered on an error page like Community-not-found.
    nodes, form = [], {"title": None, "post": None, "body": None}
    for _ in range(20):  # ~10s
        nodes = await _nodes(sess)
        form = _find_form(nodes)
        if form["title"] and form["post"]:
            break
        await sess.sleep(500)

    if not form["title"] or not form["post"]:
        # form never rendered — re-read page text now (the SPA may have routed to
        # an error/interstitial in the meantime) and classify accurately.
        p1 = await _page(sess)
        if p1.get("no_exist"):
            return "community_not_found", "", "Reddit: community doesn't exist"
        if p1.get("restricted"):
            return "community_restricted", "", "Reddit: private / restricted / quarantined / banned"
        if p1.get("is_login"):
            return "unavailable", "", "not logged in to Reddit"
        if p1.get("forbidden"):
            return "not_postable", "", "Reddit refused access to the submit form"
        return "unavailable", "", "submit form did not render"

    title, body = _title_body(message)
    try:
        await sess.type(int(form["title"]["index"]), title, clear=True)
        await sess.sleep(random.randint(250, 500))
        # body is OPTIONAL — only type into it when we actually have body text
        # AND the body textbox rendered. A short title-only post stays clean.
        if body:
            # re-resolve in case typing the title shifted indices / hydrated UI
            nodes = await _nodes(sess)
            form = _find_form(nodes)
            if form["body"] is not None:
                await sess.type(int(form["body"]["index"]), body, clear=True)
    except Exception as e:
        return "post_failed", "", f"type failed: {str(e)[:120]}"

    await sess.sleep(random.randint(700, 1200))

    # re-find Post and require it ENABLED. Disabled-after-fill is the strongest
    # signal of a flair-required sub (Reddit blocks submit at the UI level).
    nodes = await _nodes(sess)
    form = _find_form(nodes)
    post = form["post"]
    if post is None:
        return "post_failed", "", "Post button vanished after typing"
    if _is_disabled(post):
        await sess.sleep(900)
        nodes = await _nodes(sess)
        form = _find_form(nodes)
        post = form["post"]
        if post is None or _is_disabled(post):
            p = await _page(sess)
            if p.get("needs_flair"):
                return "needs_flair", "", "Reddit: community requires a post flair"
            if p.get("forbidden"):
                return "not_postable", "", "Reddit: posting not allowed (rules / karma / age)"
            return "needs_flair", "", "Post stayed disabled (community likely requires a flair)"

    try:
        await sess.click(int(post["index"]))
    except Exception as e:
        return "post_failed", "", f"click Post failed: {str(e)[:120]}"

    # confirm — URL transition AWAY from /submit/ is the load-bearing signal.
    # Either we land on /comments/<id>/<slug>/ directly OR (more common on the
    # new Reddit submit flow) on the community feed, from which we recover the
    # post_url by matching the title we just typed against the anchors on the
    # page (so community sticky-posts at the top of the feed can't false-match).
    for _ in range(20):  # ~12s
        await sess.sleep(600)
        p = await _page(sess, title)
        if not p.get("is_submit"):
            # the URL transitioned away — even if extraction misses, the post
            # IS up. Try one more observe with a tiny settle so the feed has
            # time to hydrate the user's brand-new post.
            url = _extract_post_url(p)
            if not url:
                await sess.sleep(800)
                url = _extract_post_url(await _page(sess, title))
            if url or p.get("is_post"):
                return "posted", url, "" if url else "posted (post URL not surfaced on the next page)"
            return "posted", "", "posted (post URL not surfaced on the next page)"
        # still on /submit/ → look for known refusal texts
        if p.get("ratelimit"):
            return "rate_limited", "", "Reddit ratelimit — try again later"
        if p.get("needs_flair"):
            return "needs_flair", "", "Reddit: community requires a post flair"
        if p.get("forbidden"):
            return "not_postable", "", "Reddit refused the post (rules / karma / ban)"
        if p.get("no_exist"):
            return "community_not_found", "", "Reddit: community doesn't exist"
        if p.get("restricted"):
            return "community_restricted", "", "Reddit: private / restricted / quarantined"
        if p.get("is_login"):
            return "unavailable", "", "session lost (logged out mid-post)"
    return "post_failed", "", "could not confirm post (still on /submit/ after timeout)"


# ---- run ---------------------------------------------------------------------
async def run(params, sess, inputs):
    fallback = _messages(params)
    n_personalised = sum(1 for r in inputs if _row_message(r))
    if not fallback and not n_personalised:
        userkit.error("no message — provide a 'message'/'messages' param, "
                      "or a 'message' column in the input")
        return [{"community": _community(r) or str(r.get("community") or ""),
                 "post_url": "", "status": "error", "detail": "no message configured"}
                for r in inputs]

    max_posts = int(params.get("maxPosts") or 0)  # 0 = no cap
    out, total, posted, fb_sent, stop = [], len(inputs), 0, 0, False
    userkit.log(
        f"[reddit] {total} rows"
        f"{f' · {n_personalised} personalised (per-row override)' if n_personalised else ''}"
        f"{f' · {len(fallback)} fallback variant(s)' if fallback else ''}"
        f"{' · alternating' if len(fallback) > 1 else ''}"
        f"{f' · cap {max_posts}' if max_posts else ''}"
    )

    for i, row in enumerate(inputs, 1):
        community = _community(row)
        raw_input = str(row.get("community") or row.get("subreddit") or row.get("url") or row.get("name") or "").strip()
        display = community or raw_input or "(empty)"

        if not community:
            out.append({"community": raw_input, "post_url": "", "status": "invalid_input",
                        "detail": "no Reddit community name in row (need r/<name>, a community URL, or the bare name)"})
            userkit.progress(i, total, message=f"{i}/{total} (invalid)")
            continue

        if stop:
            out.append({"community": community, "post_url": "", "status": "skipped",
                        "detail": "stopped (cap or ratelimit)"})
            continue

        row_msg = _row_message(row)
        if row_msg:
            message = row_msg
        elif fallback:
            message = fallback[fb_sent % len(fallback)]
        else:
            out.append({"community": community, "post_url": "", "status": "no_message",
                        "detail": "no per-row message and no fallback param"})
            userkit.progress(i, total, message=f"{i}/{total} (no message)")
            continue

        try:
            status, post_url, detail = await process_community(sess, community, message)
        except Exception as e:
            status, post_url, detail = "error", "", str(e)[:160]
            userkit.log(f"[reddit] r/{community} error: {e}")

        out.append({"community": community, "post_url": post_url, "status": status, "detail": detail})
        if status == "posted":
            posted += 1
            if not row_msg:  # only fallback posts advance the alternation cursor
                fb_sent += 1
        if status == "rate_limited":
            userkit.log("[reddit] ratelimit — stopping run")
            stop = True
        userkit.progress(i, total, message=f"{i}/{total} r/{display} → {status}", url=f"r/{community}")
        if max_posts and posted >= max_posts:
            userkit.log(f"[reddit] reached maxPosts={max_posts}; stopping")
            stop = True
        if i < total and not stop:
            # human pace between communities — Reddit ratelimits posting hard.
            await asyncio.sleep(random.uniform(8.0, 18.0))

    return out


def main(argv=None):
    params, server, output = userkit.parse(argv)
    inputs = userkit.input_rows(argv)
    cols = ["community", "post_url", "status", "detail"]
    if not inputs:
        userkit.error("no input rows — bind a dataset of Reddit communities to this run")
        userkit.write_csv(output, [], cols)
        return 1
    rows = userkit.run_session(lambda p, s: run(p, s, inputs), params, server)
    userkit.write_csv(output, rows, cols)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
