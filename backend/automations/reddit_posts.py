"""Built-in, list-consuming workflow: create a text or media POST in each Reddit community.

Consumes a dataset of Reddit communities (a ``community`` per row in any common
format — ``r/learnpython``, ``/r/learnpython``, ``learnpython``, a community URL
like ``https://www.reddit.com/r/learnpython/``, or any URL whose path contains
``/r/<name>``) and, one community at a time, human-paced, opens that community's
submit page and publishes a post.

Message resolution mirrors LinkedIn Messages exactly (so the two feel like one
family), in priority order:

  1. A per-row ``message`` column on the input dataset (personalised per
     community — overrides everything).
  2. The ``messages`` param: one message used for everyone, or several
     separated by ``||`` to alternate round-robin across the posts actually
     published (a per-row message doesn't consume an alternation slot).

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

**Media (images & video) — same shape as messages**:

  1. A per-row ``media`` column on the input dataset (``file_list``,
     personalised per community — overrides everything).
  2. The ``media`` param (``file_list``): one set of files used for every post
     that doesn't have its own per-row media.

Crucially media is NEVER round-robined: each post is a complete artefact, and
"distribute these N files across N posts" is what the dataset is for. Rules
(determined empirically against the live Reddit UI):

* **Images**: 1 to 20 files, mimes ``image/jpeg|png|webp|gif``. Multi-image
  becomes a Reddit gallery.
* **Video**: exactly 1 file, mimes ``video/mp4|quicktime`` (.mp4 / .mov).
* **Mixed** (any image + any video together) is **rejected** as
  ``media_invalid`` — Reddit's behaviour with mixed uploads is ambiguous
  (the video is silently dropped in some flows), so we never publish a
  partial post.
* Wrong mime / too many / zero size / file missing on disk → ``media_invalid``,
  the post is **not** published (all-or-nothing — we don't fall back to a
  text-only post when media was requested).
* Upload-stage failure (file_chooser timeout / preview never renders) →
  ``media_failed``, post **not** published.

Hard-won realities, learned by submitting real posts by hand:

* **Submit page is plain DOM** (no shadow). Title is a ``<textarea>`` with
  accessible name "title". The body is a ``<div role="textbox">``
  (contenteditable) with accessible name "Post body text field". Submit is a
  ``<button>`` named "Post", starts ``disabled`` and becomes enabled once a
  title is present (the body can stay empty for a title-only post).
* **Media uploads ride the visible "Upload files" button via the file-chooser
  intercept**, NOT the hidden ``<input type=file>`` (Reddit puts five of those
  in shadow roots with no stable identifier; the one the page's JS actually
  wires to the visible button isn't always the first one to a selector match).
  ``sess.file_chooser(index, files, names=..., mimes=...)`` clicks the Upload
  button while ``page.expect_file_chooser()`` is open, then provides the
  files. Reddit renders ``blob:`` previews — we poll for the right preview
  count before considering the upload landed.
* **The body field is labelled differently per post type** — "Post body text
  field" on a Text post, "Optional Body text field" on an Images & Video post.
  When the message has a body part it MUST land: if the body field can't be
  found, or stays empty after typing, we return ``body_failed`` and DON'T
  publish — a media post that silently dropped its body is the exact bug this
  guards against (all-or-nothing, like media).
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
* **Community refuses media** (text-only sub: the Images & Video tab is
  ``disabled`` on its submit form) → we report ``not_postable_media`` and
  don't fall back to text.
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

SUBMIT_URL_TEXT  = "https://www.reddit.com/r/{}/submit/?type=TEXT"
SUBMIT_URL_MEDIA = "https://www.reddit.com/r/{}/submit/?type=IMAGE"

# Media rules (verified against the live submit form's hidden <input> accept lists
# + the gallery / single-video constraints the UI enforces).
IMAGE_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
VIDEO_MIMES = {"video/mp4", "video/quicktime"}
MAX_IMAGES_PER_POST = 20
MAX_VIDEOS_PER_POST = 1

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
    """Resolve the fallback message(s) from the single ``messages`` param: one
    message to send to everyone, or several separated by ``||`` to rotate
    round-robin across the posts actually published. A JSON array is also
    accepted for programmatic callers. We strip surrounding whitespace only —
    embedded newlines stay (the body field preserves them and that's the right
    rendering for a Reddit post)."""
    raw = params.get("messages")
    if not raw:
        return []
    s = str(raw).strip()
    if not s:
        return []
    msgs: list[str] = []
    if s[:1] == "[":
        try:
            msgs = [str(x) for x in json.loads(s)]
        except (ValueError, TypeError):
            msgs = []
    if not msgs:
        msgs = s.split("||")
    return [m.strip() for m in msgs if m and m.strip()]


def _row_message(row: dict) -> str:
    """A per-recipient message carried on the input row (a ``message`` column on
    the dataset). When present it OVERRIDES the ``messages`` param entirely, so
    each community can get a bespoke post."""
    for k in ("message", "messaggio", "msg", "body", "post"):
        if row.get(k) and str(row[k]).strip():
            return str(row[k]).strip()
    return ""


def _resolve_media(params: dict, row: dict) -> list[dict]:
    """Resolve the media files for this post: per-row ``media`` column on the
    input dataset (already expanded to dicts by the orchestrator) wins; else the
    workflow ``media`` param (a list of file records from the registry). Returns
    a list of file dicts each carrying ``path``/``name``/``mime``. The empty
    list means "no media — text post"."""
    files = userkit.input_files(row, "media") or userkit.input_files(row, "files")
    if files:
        return files
    single = userkit.input_file(row, "media")
    if single:
        return [single]
    raw = params.get("media")
    if not raw:
        return []
    # Already-expanded list of file-record dicts (orchestrator preserves these).
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return [m for m in raw if isinstance(m, dict) and m.get("path")]
    if isinstance(raw, dict) and raw.get("path"):
        return [raw]
    # Otherwise we have id(s) to resolve via the file store. Accept ALL the
    # shapes an agent/UI realistically sends: a list of id strings
    # (["754e0fea"]), a single id string ("754e0fea"), or a JSON-array string
    # ('["754e0fea"]'). (The earlier code only handled list-of-dicts, so a bare
    # list of id strings silently resolved to no media → a text-only post.)
    ids: list[str] = []
    if isinstance(raw, list):
        ids = [str(x).strip() for x in raw if str(x).strip()]
    else:
        s = str(raw).strip()
        if s[:1] == "[":
            try:
                ids = [str(x).strip() for x in json.loads(s) if str(x).strip()]
            except (ValueError, TypeError):
                ids = []
        elif s:
            ids = [s]
    if not ids:
        return []
    try:
        from orchestrator import files as _files
    except ImportError:
        return []
    out: list[dict] = []
    for fid in ids:
        rec = _files.get(fid)
        if rec:
            out.append(rec)
    return out


def _classify_media(files: list[dict]) -> tuple[str, str]:
    """Validate ``files`` against Reddit's gallery / single-video / no-mixed
    rules. Returns ``(kind, error)`` where ``kind`` is one of ``""`` (no media
    — text post), ``"images"`` (1..20 images), ``"video"`` (exactly one video),
    and ``error`` is non-empty when the set is invalid."""
    if not files:
        return "", ""
    imgs, videos, bad = [], [], []
    for f in files:
        m = (f.get("mime") or "").lower()
        if m in IMAGE_MIMES:
            imgs.append(f)
        elif m in VIDEO_MIMES:
            videos.append(f)
        else:
            bad.append(f.get("name") or f.get("id") or "?")
    if bad:
        return "", (f"unsupported mime: {bad[:3]} (Reddit accepts "
                    f"jpeg/png/webp/gif images and mp4/mov videos)")
    if imgs and videos:
        return "", "mixed images + video not supported (Reddit drops the video in mixed posts)"
    if videos:
        if len(videos) > MAX_VIDEOS_PER_POST:
            return "", f"too many videos: {len(videos)} (Reddit allows {MAX_VIDEOS_PER_POST} per post)"
        return "video", ""
    if len(imgs) > MAX_IMAGES_PER_POST:
        return "", f"too many images: {len(imgs)} (Reddit gallery cap is {MAX_IMAGES_PER_POST})"
    # check files actually exist on disk + are non-empty (the upload step needs them)
    import os as _os
    for f in imgs:
        p = f.get("path") or ""
        if not p or not _os.path.isfile(p):
            return "", f"missing file on disk: {f.get('name') or p}"
        if _os.path.getsize(p) == 0:
            return "", f"empty file: {f.get('name') or p}"
    return "images", ""


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
    """Identify the submit-form controls by their accessible-name shape
    (stable across communities; observe gives us the label as ``name``).

    * Title — a ``<textarea>`` whose name is "title" (the form label).
    * Body  — a ``<div role="textbox">`` whose name contains "post body"
      (label is "Post body text field" on a Text post, but "Optional Body text
      field" on an Images & Video post — match BOTH, plus a generic "body text"
      fallback, else a media post silently loses its body).
    * Post  — a ``<button>`` whose name is exactly "Post" (rejects "Save Draft"
      and the per-tool toolbar buttons).
    * Upload — a ``<button>`` named "Upload files" (only present on the
      ``?type=IMAGE`` form when the community supports media).
    * Image tab / image tab disabled — the "Images & Video" role=tab button,
      with a flag for whether it's disabled (text-only sub).

    Returns dict of {title, body, post, upload, image_tab, image_tab_disabled}
    (any value may be None if not yet rendered)."""
    title = next((n for n in nodes if n.get("tag") == "textarea"
                  and _name(n).lower() == "title"), None)
    # Both the Text-tab and the Images&Video-tab body textbox can be in the DOM;
    # observe usually prunes the hidden one, but if both surface prefer the
    # in-viewport (active) one so we never type into the hidden twin.
    body_matches = [n for n in nodes if n.get("tag") == "div"
                    and _attrs(n).get("role") == "textbox"
                    and any(s in _name(n).lower() for s in
                            ("post body", "optional body", "body text"))]
    body = next((n for n in body_matches if n.get("inViewport")), None) or \
        (body_matches[0] if body_matches else None)
    post = next((n for n in nodes if n.get("tag") == "button"
                 and _name(n).lower() == "post"), None)
    upload = next((n for n in nodes if n.get("tag") == "button"
                   and _name(n).lower() == "upload files"), None)
    image_tab = next((n for n in nodes if n.get("tag") == "button"
                      and _attrs(n).get("role") == "tab"
                      and "image" in _name(n).lower()), None)
    image_tab_disabled = bool(image_tab and _is_disabled(image_tab))
    return {"title": title, "body": body, "post": post, "upload": upload,
            "image_tab": image_tab, "image_tab_disabled": image_tab_disabled}


async def _body_filled(sess, expected: str) -> bool:
    """Confirm the post-body contenteditable actually holds our text before we
    click Post. The body is a light-DOM ``div[role=textbox]`` whose aria label
    contains 'body' (e.g. 'Post body text field' / 'Optional Body text field');
    we read its innerText and require a meaningful prefix of the body to be
    present (collapsing whitespace so newline rendering differences don't fail
    the check). Defensive: any read error returns True so we don't block a
    legitimate post on a transient eval hiccup."""
    want = " ".join((expected or "").split())[:40].lower()
    if not want:
        return True
    # The Images & Video form keeps BOTH the Text-tab body ("Post body text
    # field") and the active media-tab body ("Optional Body text field") in the
    # DOM — the inactive one is hidden (offsetParent === null, zero-size). Read
    # only VISIBLE body textbox(es) and take the one with the most content, so
    # we verify the field we actually typed into, not the hidden empty twin.
    js = r"""() => {
      const boxes = document.querySelectorAll('div[role="textbox"]');
      let best = '';
      for (const b of boxes) {
        const lbl = ((b.getAttribute('aria-label') || '') + ' ' +
                     (b.getAttribute('aria-placeholder') || '')).toLowerCase();
        if (lbl.indexOf('body') === -1) continue;
        if (b.offsetParent === null) continue;            // hidden (inactive tab)
        const r = b.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;     // zero-size
        const t = b.innerText || b.textContent || '';
        if (t.length > best.length) best = t;
      }
      return best;
    }"""
    try:
        txt = await sess.evaluate(js)
        got = " ".join(str(txt or "").split()).lower()
        return want in got
    except Exception:
        return True


async def _media_preview_count(sess) -> int:
    """How many media previews has Reddit's upload pipeline already rendered?
    Each uploaded image becomes a ``blob:`` <img>, each video becomes a <video>
    with a blob: poster. Walking shadow roots is essential — the preview tiles
    live inside Reddit's design-system shadow DOM. Used to wait for an upload
    to LAND before clicking Post."""
    js = r"""() => {
      const seen = new Set(); let videos = 0;
      function walk(root) {
        for (const img of root.querySelectorAll('img')) {
          const s = img.src || ''; if (s.startsWith('blob:')) seen.add(s);
        }
        videos += root.querySelectorAll('video').length;
        for (const el of root.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot);
      }
      walk(document);
      return { imgs: seen.size, videos };
    }"""
    try:
        d = await sess.evaluate(js)
        return int((d or {}).get("imgs", 0)) + int((d or {}).get("videos", 0))
    except Exception:
        return 0


async def _upload_media(sess, media: list[dict], kind: str) -> tuple[bool, str]:
    """Upload ``media`` (already validated by ``_classify_media``) via the
    visible "Upload files" button + file-chooser intercept. Polls for the
    ``blob:`` previews to land before returning. Returns ``(ok, detail)``."""
    # Poll for the Upload files button to render (the Images & Video tab can
    # take a beat to hydrate after navigation).
    upload_btn = None
    for _ in range(20):  # ~10s
        nodes = await _nodes(sess)
        form = _find_form(nodes)
        if form["upload"]:
            upload_btn = form["upload"]; break
        await sess.sleep(500)
    if not upload_btn:
        return False, "Upload files button never rendered (community may refuse media)"

    paths = [m["path"] for m in media]
    names = [m.get("name") or "" for m in media]
    mimes = [m.get("mime") or "" for m in media]
    try:
        await sess.file_chooser(int(upload_btn["index"]), paths,
                                names=names, mimes=mimes, timeout_ms=15_000)
    except Exception as e:
        return False, f"file chooser failed: {str(e)[:120]}"

    # Wait for the previews to land — exact count for images, ≥1 for video.
    want = len(media)
    deadline_polls = 30  # ~15s for the slowest uploads (small files; bigger may need more)
    for _ in range(deadline_polls):
        await sess.sleep(500)
        seen = await _media_preview_count(sess)
        if seen >= want:
            return True, ""
        # Detect Reddit's inline rejection on bad upload (e.g. unsupported file
        # the server refused after the client passed it through).
        p = await _page(sess)
        if p.get("ratelimit"):
            return False, "Reddit ratelimit during upload"
    return False, f"only {await _media_preview_count(sess)}/{want} preview(s) appeared after upload"


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
async def process_community(sess, community: str, message: str,
                            media: list[dict] | None = None) -> tuple[str, str, str]:
    """Open the submit page for ``community`` and publish ``message`` (plus
    optional ``media`` — a list of validated file records) as a post.
    Returns (status, post_url, detail). All-or-nothing: when media is supplied
    we never fall back to a text-only post if the media flow can't complete."""
    media = media or []
    target = (SUBMIT_URL_MEDIA if media else SUBMIT_URL_TEXT).format(community)
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

    # Media path: confirm the Images & Video tab is selectable (text-only subs
    # render it disabled). Then upload the files BEFORE typing the title — the
    # Post button needs both title AND a landed upload to enable, so this order
    # gives the user no false-positive on a media-required post that lost its
    # media silently.
    if media:
        if form.get("image_tab_disabled"):
            return ("not_postable_media", "",
                    "community doesn't accept media posts (Images & Video tab is disabled)")
        if not form.get("upload"):
            return ("not_postable_media", "", "Upload files button not found on this form")
        ok, err = await _upload_media(sess, media, "video" if media[0].get("mime", "").startswith("video") else "images")
        if not ok:
            return "media_failed", "", err

    title, body = _title_body(message)
    try:
        # Re-resolve form after upload — adding media re-renders the form.
        nodes = await _nodes(sess)
        form = _find_form(nodes)
        if not form.get("title"):
            return "post_failed", "", "title textarea vanished after media upload"
        await sess.type(int(form["title"]["index"]), title, clear=True)
        await sess.sleep(random.randint(250, 500))
        # Body is optional ONLY when the message has no body part. When we DO
        # have body text, it MUST land — never publish a title-only post that
        # silently dropped the body (the media-post form labels the body
        # "Optional Body text field", which an out-of-date selector missed,
        # so posts went out body-less and didn't fail). All-or-nothing.
        if body:
            nodes = await _nodes(sess)
            form = _find_form(nodes)
            if form["body"] is None:
                return ("body_failed", "",
                        "post body field not found — refusing to publish a body-less post")
            await sess.type(int(form["body"]["index"]), body, clear=True)
            await sess.sleep(random.randint(300, 600))
            # Verify the body actually registered in the contenteditable before
            # we commit (a click that didn't focus, an editor that swallowed the
            # text, etc. would otherwise publish an empty body silently).
            if not await _body_filled(sess, body):
                # one more beat for the editor to settle, then re-check
                await sess.sleep(900)
                if not await _body_filled(sess, body):
                    return ("body_failed", "",
                            "post body stayed empty after typing — refusing to publish a body-less post")
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
        userkit.error("no message — provide a 'messages' param (use || to separate "
                      "variants), or a 'message' column in the input")
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

        # Media resolution + validation: per-row 'media' column wins, else the
        # workflow 'media' param. We validate BEFORE driving the browser so the
        # invalid case never sends a half-formed post.
        try:
            media = _resolve_media(params, row)
        except Exception as e:
            media = []
            userkit.log(f"[reddit] r/{community} media resolve error: {e}")
        kind, media_err = _classify_media(media)
        if media_err:
            out.append({"community": community, "post_url": "", "status": "media_invalid",
                        "detail": media_err})
            userkit.progress(i, total, message=f"{i}/{total} r/{display} → media_invalid")
            if i < total:
                await asyncio.sleep(random.uniform(2.0, 4.0))
            continue

        try:
            status, post_url, detail = await process_community(sess, community, message, media=media)
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
