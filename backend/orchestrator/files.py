"""Files — the content-addressed file store + metadata registry.

This is the binary-data peer of [[datastore]]: a *single* place where every file
(image, video, document, anything) lives so it can be referenced from dataset
cells, passed in/out of workflows, attached to browser fields, scraped from
pages, or handed to an agent — without duplication or scattered paths.

Design:
- **Content-addressed on disk** at ``data_dir()/files/<sha256>.<ext>``: the same
  bytes uploaded twice yield one blob. Files there are immutable; nothing
  rewrites a stored file.
- **Metadata in SQLite** (``files`` table, in the SAME ``studio.sqlite`` as the
  datasets — one DB, one transaction story). Each *registration* gets an opaque
  short ``id`` (8-hex, the user/cell-facing handle). Multiple registrations can
  point to the same ``sha256`` if a caller wants distinct names/tags for the
  same content; the physical blob is still one.
- **No size cap.** Dedup makes the worst case "one big upload, ever".
- **Reference-safe delete**: ``delete(id)`` refuses if any dataset cell (a
  ``file`` or ``file_list`` column) still references the id, unless the caller
  passes ``force=True``.

The orchestrator and userkit build on top: workflows declare ``file``/
``file_list`` columns in their contracts, and the run plumbing automatically
expands ids → ``{id,path,name,mime}`` dicts on input and auto-registers
emitted paths in ``file``-typed output columns on the way out.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable

from humanbrowser.config import data_dir

from . import datastore  # share _db() and _lock with the datasets registry


def files_dir() -> Path:
    """Where physical blobs live. Created lazily."""
    d = data_dir() / "files"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- magic-byte sniffing for common formats when extension is missing/wrong --
_SIGS: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n",            "image/png",        "png"),
    (b"\xff\xd8\xff",                  "image/jpeg",       "jpg"),
    (b"GIF87a",                        "image/gif",        "gif"),
    (b"GIF89a",                        "image/gif",        "gif"),
    (b"RIFF",                          "image/webp",       "webp"),  # check WEBP at +8 below
    (b"BM",                            "image/bmp",        "bmp"),
    (b"%PDF-",                         "application/pdf",  "pdf"),
    (b"PK\x03\x04",                    "application/zip",  "zip"),
    (b"\x1f\x8b\x08",                  "application/gzip", "gz"),
    (b"\x00\x00\x00\x18ftypmp4",       "video/mp4",        "mp4"),
    (b"\x00\x00\x00 ftypisom",        "video/mp4",        "mp4"),
    (b"\x00\x00\x00\x14ftypqt",        "video/quicktime",  "mov"),
    (b"ID3",                           "audio/mpeg",       "mp3"),
    (b"OggS",                          "audio/ogg",        "ogg"),
    (b"fLaC",                          "audio/flac",       "flac"),
    (b"\xff\xfb",                      "audio/mpeg",       "mp3"),
    (b"\xff\xf3",                      "audio/mpeg",       "mp3"),
    (b"\xff\xf2",                      "audio/mpeg",       "mp3"),
    (b"<svg",                          "image/svg+xml",    "svg"),
    (b"<?xml",                         "application/xml",  "xml"),
    (b"{",                             "application/json", "json"),
    (b"[",                             "application/json", "json"),
]

# Case-insensitive HTML sniffing (DOCTYPE / html start can be any case).
_HTML_PREFIXES = (b"<!doctype html", b"<html", b"<!DOCTYPE HTML")

_TEXT_MIME_RE = re.compile(r"^(text/|application/(json|xml|x-yaml|toml|x-sh|x-python|javascript|sql))", re.I)


def is_textual(mime: str) -> bool:
    """Whether a mime is safe to read as utf-8 text (controls view_file)."""
    return bool(_TEXT_MIME_RE.match(mime or ""))


def _sniff(head: bytes) -> tuple[str | None, str | None]:
    """Return (mime, ext) by magic bytes, or (None, None)."""
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", "webp"
    for sig, mime, ext in _SIGS:
        if head.startswith(sig):
            return mime, ext
    head_lc = head[:32].lower()
    if any(head_lc.startswith(p.lower()) for p in _HTML_PREFIXES):
        return "text/html", "html"
    # textual?
    try:
        head.decode("utf-8")
        return "text/plain", "txt"
    except UnicodeDecodeError:
        return None, None


def _detect(path: Path, hint_name: str | None = None) -> tuple[str, str]:
    """Return (mime, ext) for a file on disk.

    Order: 1) magic bytes (most reliable); 2) the hint filename's extension
    (caller knows best); 3) the path's extension. Falls back to
    ``application/octet-stream`` + ``bin``."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
        mime, ext = _sniff(head)
        if mime:
            return mime, ext
    except OSError:
        pass
    for nm in (hint_name, str(path)):
        if not nm:
            continue
        guess = mimetypes.guess_type(nm)[0]
        if guess:
            ext = Path(nm).suffix.lstrip(".").lower() or (mimetypes.guess_extension(guess) or ".bin").lstrip(".")
            return guess, ext
    return "application/octet-stream", "bin"


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- table init
_TABLE_READY = False


def _init_table() -> None:
    global _TABLE_READY
    if _TABLE_READY:
        return
    c = datastore._db()
    c.execute(
        """CREATE TABLE IF NOT EXISTS files (
              id          TEXT PRIMARY KEY,
              sha256      TEXT NOT NULL,
              ext         TEXT NOT NULL,
              name        TEXT NOT NULL,
              mime        TEXT NOT NULL,
              size        INTEGER NOT NULL,
              created_at  REAL NOT NULL,
              source      TEXT,
              tags        TEXT,
              meta        TEXT )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS files_sha256 ON files(sha256)")
    c.execute("CREATE INDEX IF NOT EXISTS files_mime   ON files(mime)")
    c.execute("CREATE INDEX IF NOT EXISTS files_source ON files(source)")
    c.commit()
    _TABLE_READY = True


def _new_id() -> str:
    """8-hex opaque id, unique across the files table."""
    _init_table()
    c = datastore._db()
    while True:
        i = uuid.uuid4().hex[:8]
        if not c.execute("SELECT 1 FROM files WHERE id=?", (i,)).fetchone():
            return i


def _row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["path"] = str(files_dir() / f"{d['sha256']}.{d['ext']}")
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except (TypeError, ValueError):
        d["tags"] = []
    try:
        d["meta"] = json.loads(d.get("meta") or "null")
    except (TypeError, ValueError):
        d["meta"] = None
    return d


# ---------------------------------------------------------------- registration
def _stage_to_store(src: Path, sha: str, ext: str) -> Path:
    """Move/copy ``src`` into the store as ``<sha>.<ext>``. If the destination
    already exists (same content already in the store), ``src`` is unlinked when
    ``move=True`` (we own it) — the existing blob is what we keep."""
    dst = files_dir() / f"{sha}.{ext}"
    if dst.exists():
        return dst
    # use shutil.copy2 to preserve mtime; rename across mounts can fail
    shutil.copy2(src, dst)
    return dst


def register_from_path(src: str | os.PathLike, *, name: str | None = None,
                       source: str | None = None, tags: list[str] | None = None,
                       meta: dict | None = None, copy: bool = True,
                       mime: str | None = None) -> dict:
    """Register a file on disk into the store. Returns the public record.

    ``name`` defaults to the source basename. ``copy=True`` copies bytes into
    the store; ``copy=False`` is only legal when ``src`` is ALREADY under
    ``files_dir()`` (used internally to avoid double-copying). A caller-supplied
    ``mime`` overrides the magic-byte sniff (use for an authoritative
    Content-Type from an HTTP response)."""
    _init_table()
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"not a file: {src}")
    name = name or src.name
    sha = _sha256(src)
    sniff_mime, ext = _detect(src, hint_name=name)
    mime = mime or sniff_mime
    if copy:
        _stage_to_store(src, sha, ext)
    else:
        expected = files_dir() / f"{sha}.{ext}"
        if Path(src).resolve() != expected.resolve():
            raise ValueError(f"copy=False but src isn't already at {expected}")
    size = src.stat().st_size
    fid = _new_id()
    now = time.time()
    with datastore._lock:
        datastore._db().execute(
            "INSERT INTO files (id, sha256, ext, name, mime, size, created_at, source, tags, meta) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (fid, sha, ext, name, mime, size, now, source,
             json.dumps(tags or []), json.dumps(meta) if meta is not None else None),
        )
        datastore._db().commit()
    return get(fid)  # type: ignore[return-value]


def register_from_bytes(data: bytes, *, name: str, mime: str | None = None,
                        source: str | None = None, tags: list[str] | None = None,
                        meta: dict | None = None) -> dict:
    """Register raw bytes. ``name`` is required (used to derive ext when ``mime``
    is None). Writes to a temp file under files_dir to compute sha + detect, then
    moves into place atomically."""
    _init_table()
    tmp = files_dir() / f".tmp-{uuid.uuid4().hex}"
    try:
        tmp.write_bytes(data)
        if mime:
            ext = (mimetypes.guess_extension(mime) or Path(name).suffix or ".bin").lstrip(".")
        else:
            mime, ext = _detect(tmp, hint_name=name)
        sha = _sha256(tmp)
        dst = files_dir() / f"{sha}.{ext}"
        if not dst.exists():
            shutil.move(str(tmp), str(dst))
        else:
            tmp.unlink(missing_ok=True)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    fid = _new_id()
    size = dst.stat().st_size
    now = time.time()
    with datastore._lock:
        datastore._db().execute(
            "INSERT INTO files (id, sha256, ext, name, mime, size, created_at, source, tags, meta) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (fid, sha, ext, name, mime, size, now, source,
             json.dumps(tags or []), json.dumps(meta) if meta is not None else None),
        )
        datastore._db().commit()
    return get(fid)  # type: ignore[return-value]


def register_from_text(text: str, *, name: str, mime: str = "text/plain",
                       source: str | None = None, tags: list[str] | None = None) -> dict:
    return register_from_bytes(text.encode("utf-8"), name=name, mime=mime, source=source, tags=tags)


def register_from_url(url: str, *, name: str | None = None,
                      headers: dict | None = None, source: str | None = None,
                      tags: list[str] | None = None, timeout: float = 60) -> dict:
    """Plain HTTP fetch (no browser cookies). For session-locked downloads use
    the browser_fetch tool (uses the browser's cookie jar via patchright's
    ``context.request``)."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip() or None
            if not name:
                cd = r.headers.get("Content-Disposition") or ""
                m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^"\;]+)', cd)
                name = m.group(1) if m else Path(url.split("?")[0]).name or "download"
    except urllib.error.HTTPError as e:
        raise ValueError(f"HTTP {e.code} fetching {url}") from None
    except urllib.error.URLError as e:
        raise ValueError(f"cannot fetch {url}: {e.reason}") from None
    return register_from_bytes(data, name=name, mime=ctype,
                               source=source or f"fetch:{re.sub(r'^https?://', '', url).split('/')[0]}",
                               tags=tags)


# ---------------------------------------------------------------- reads
def get(fid: str) -> dict | None:
    _init_table()
    r = datastore._db().execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    return _row_to_dict(r) if r else None


def get_by_sha(sha: str) -> list[dict]:
    """All registrations pointing at the same physical blob (rare; useful for
    dedup auditing)."""
    _init_table()
    rs = datastore._db().execute("SELECT * FROM files WHERE sha256=? ORDER BY created_at",
                                 (sha,)).fetchall()
    return [_row_to_dict(r) for r in rs]


def path_of(fid: str) -> Path | None:
    f = get(fid)
    return Path(f["path"]) if f else None


def view_text(fid: str, max_bytes: int = 200_000) -> str | None:
    """Read a textual file's content as utf-8 (truncated to ``max_bytes``).
    Returns None when the mime isn't textual."""
    f = get(fid)
    if not f or not is_textual(f["mime"]):
        return None
    try:
        with open(f["path"], "rb") as fh:
            data = fh.read(max_bytes + 1)
        text = data[:max_bytes].decode("utf-8", errors="replace")
        if len(data) > max_bytes:
            text += "\n… [truncated]"
        return text
    except OSError:
        return None


def list_files(*, mime: str | None = None, source: str | None = None,
               tag: str | None = None, search: str | None = None,
               limit: int = 200, offset: int = 0) -> dict:
    """List files with optional filters.

    - ``mime`` glob: ``image/*``, ``video/*``, ``application/json`` (exact).
    - ``source`` prefix (e.g. ``run:`` to list run-produced files).
    - ``tag`` exact-match against a tag in the user tags list.
    - ``search`` substring on name (case-insensitive).
    """
    _init_table()
    where: list[str] = []
    params: list[Any] = []
    if mime:
        if mime.endswith("/*"):
            where.append("mime LIKE ?"); params.append(mime[:-1] + "%")
        else:
            where.append("mime = ?"); params.append(mime)
    if source:
        where.append("source LIKE ?"); params.append(source + "%")
    if search:
        where.append("LOWER(name) LIKE ?"); params.append(f"%{search.lower()}%")
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    total = datastore._db().execute(f"SELECT COUNT(*) AS n FROM files {sql_where}", params).fetchone()["n"]
    rows = datastore._db().execute(
        f"SELECT * FROM files {sql_where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [max(1, min(int(limit), 5000)), max(0, int(offset))],
    ).fetchall()
    out = [_row_to_dict(r) for r in rows]
    if tag:
        out = [f for f in out if tag in (f.get("tags") or [])]
    return {"count": total, "files": out}


def search(query: str, limit: int = 50) -> list[dict]:
    """Best-effort search across name + tags (substring, case-insensitive)."""
    _init_table()
    q = (query or "").strip().lower()
    if not q:
        return []
    rs = datastore._db().execute(
        "SELECT * FROM files WHERE LOWER(name) LIKE ? OR LOWER(COALESCE(tags,'')) LIKE ? "
        "ORDER BY created_at DESC LIMIT ?",
        (f"%{q}%", f"%{q}%", int(limit)),
    ).fetchall()
    return [_row_to_dict(r) for r in rs]


def rename(fid: str, new_name: str) -> dict | None:
    """Rename the *display* name only (physical blob unchanged)."""
    _init_table()
    if not new_name or not new_name.strip():
        return get(fid)
    with datastore._lock:
        datastore._db().execute("UPDATE files SET name=? WHERE id=?", (new_name.strip(), fid))
        datastore._db().commit()
    return get(fid)


def set_tags(fid: str, tags: list[str]) -> dict | None:
    _init_table()
    clean = [str(t).strip() for t in (tags or []) if str(t).strip()]
    # de-dup, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in clean:
        if t not in seen:
            out.append(t); seen.add(t)
    with datastore._lock:
        datastore._db().execute("UPDATE files SET tags=? WHERE id=?", (json.dumps(out), fid))
        datastore._db().commit()
    return get(fid)


# ---------------------------------------------------------------- references
def references(fid: str) -> list[dict]:
    """Where is ``fid`` referenced in dataset cells? Scans every ``file`` /
    ``file_list`` column of every dataset. Used by delete-safety + UI."""
    _init_table()
    out: list[dict] = []
    for ds in datastore.list_datasets():
        for c in ds["columns"]:
            if c["type"] not in {"file", "file_list"}:
                continue
            phys = c["name"]
            rows = datastore._db().execute(
                f'SELECT "_rid", {datastore._q(phys)} AS v FROM {datastore._q(ds["table"])} '
                f"WHERE {datastore._q(phys)} IS NOT NULL"
            ).fetchall()
            for r in rows:
                v = r["v"]
                if v is None:
                    continue
                if c["type"] == "file":
                    if str(v) == fid:
                        out.append({"datasetId": ds["id"], "datasetName": ds["name"],
                                    "column": c["display"], "rid": r["_rid"]})
                else:  # file_list
                    try:
                        ids = json.loads(v) if isinstance(v, str) else []
                    except ValueError:
                        ids = []
                    if fid in (ids or []):
                        out.append({"datasetId": ds["id"], "datasetName": ds["name"],
                                    "column": c["display"], "rid": r["_rid"]})
    return out


def delete(fid: str, *, force: bool = False) -> dict:
    """Delete a file registration. Refuses (returning ``{ok:false,refs:[...]}``)
    if any dataset cell still references the id, unless ``force=True``. When the
    last registration pointing at a physical blob is removed, the blob is unlinked
    from disk too."""
    _init_table()
    f = get(fid)
    if not f:
        return {"ok": False, "error": "not found"}
    refs = references(fid)
    if refs and not force:
        return {"ok": False, "error": "file is referenced by dataset cells; pass force=true to delete anyway",
                "refs": refs}
    with datastore._lock:
        datastore._db().execute("DELETE FROM files WHERE id=?", (fid,))
        datastore._db().commit()
        # Unlink the physical blob only when no other registration points at it.
        siblings = datastore._db().execute("SELECT 1 FROM files WHERE sha256=? LIMIT 1",
                                           (f["sha256"],)).fetchone()
        if not siblings:
            try:
                Path(f["path"]).unlink(missing_ok=True)
            except OSError:
                pass
    return {"ok": True, "id": fid, "refs": refs}


# ---------------------------------------------------------------- workspace
def copy_to_workspace(fid: str, dst: str | os.PathLike) -> str:
    """Materialise the file at ``dst`` (an absolute or relative path the agent
    will read via its native tools). Returns the absolute path written."""
    f = get(fid)
    if not f:
        raise FileNotFoundError(f"no file {fid}")
    dst = Path(dst).expanduser()
    if dst.is_dir():
        dst = dst / f["name"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(f["path"], dst)
    return str(dst.resolve())


# ---------------------------------------------------------------- expand for workflows
def expand_value(value: Any) -> Any:
    """Resolve a value as it appears in a workflow input row when the column
    type is ``file`` (single id → dict) or ``file_list`` (json array of ids →
    list of dicts). Unknown ids become ``None`` in the resolved place (so a
    workflow can skip them gracefully). Non-id values pass through unchanged."""
    if value is None:
        return None
    if isinstance(value, dict):  # already expanded by caller
        return value
    s = str(value).strip()
    if not s:
        return None
    # file_list: a JSON array of ids
    if s[:1] == "[":
        try:
            ids = json.loads(s)
        except ValueError:
            return value
        if isinstance(ids, list):
            return [get(str(x)) for x in ids if str(x).strip()]
    # single id
    f = get(s)
    return f if f else value


# Match the public shape returned by store ops. A "looks like an id" check —
# used by the orchestrator's auto-capture path on output rows.
_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def looks_like_id(s: Any) -> bool:
    return isinstance(s, str) and bool(_ID_RE.match(s))


def absorb_path_or_id(value: Any, *, source: str | None = None) -> Any:
    """For a workflow's output value in a ``file`` column: if it's already a
    valid registered id, return it unchanged; else if it's a path to an existing
    file, register it and return the new id; else return the value unchanged
    (so the cell stays text and the user sees the raw output)."""
    if value is None or value == "":
        return value
    s = str(value)
    if looks_like_id(s) and get(s):
        return s
    p = Path(s).expanduser()
    if p.is_file():
        try:
            rec = register_from_path(p, name=p.name, source=source)
            return rec["id"]
        except Exception:
            return value
    return value


def absorb_list(value: Any, *, source: str | None = None) -> Any:
    """For a ``file_list`` output value: a JSON array of ids/paths → JSON array
    of ids (after auto-registering any paths). Falsy stays falsy."""
    if value is None or value == "":
        return value
    if isinstance(value, list):
        items = value
    else:
        s = str(value).strip()
        if not s:
            return value
        if s[:1] != "[":
            # single item passed where a list was declared — accept it
            absorbed = absorb_path_or_id(s, source=source)
            return json.dumps([absorbed]) if absorbed else value
        try:
            items = json.loads(s)
        except ValueError:
            return value
    if not isinstance(items, list):
        return value
    out = []
    for it in items:
        a = absorb_path_or_id(it, source=source)
        if a is not None and a != "":
            out.append(a)
    return json.dumps(out)
