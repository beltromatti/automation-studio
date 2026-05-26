"""Datasets — the SQLite-backed, persistent, cross-run data layer.

This is the queryable spine that lets data flow *between* workflows and agents.
A **Dataset** is a named, spreadsheet-like table with a typed-column *contract*;
it persists across runs and workflows. CSV is import/export only at the boundary
— inside the app everything lives in SQLite so it can be queried, filtered,
deduped, projected and merged.

Design:
- One SQLite file (``studio.sqlite`` in the data dir), WAL so a run can write
  while the UI reads. Two metadata tables (``datasets``, ``dataset_columns``)
  plus **one physical table per dataset** (``ds_<id>``) — real columns, so an
  agent can run real SQL against it, and the UI gets a true spreadsheet.
- User-facing *display* column names are sanitised to safe SQL identifiers; the
  mapping is kept in ``dataset_columns`` (``display`` ↔ ``name``). Every dataset
  table carries internal ``_rid`` (stable row id) and ``_added_at`` columns.

All functions are synchronous and guarded by one re-entrant lock; on localhost
single-user scale that's simpler and safe. Reads for the SQL endpoint use a
separate read-only connection so an agent's query can never mutate state.
"""
from __future__ import annotations

import csv as csvmod
import io
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import sqlite3

from humanbrowser.config import data_dir

DB_PATH = data_dir() / "studio.sqlite"
LOGICAL_TYPES = {"text", "number", "boolean"}
_AFFINITY = {"text": "TEXT", "number": "NUMERIC", "boolean": "INTEGER"}
_RESERVED = {"_rid", "_added_at", "rowid", "oid"}

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


# ---------------------------------------------------------------------- connection
def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute(
            """CREATE TABLE IF NOT EXISTS datasets (
                   id TEXT PRIMARY KEY,
                   name TEXT NOT NULL,
                   table_name TEXT NOT NULL,
                   dedup_keys TEXT,
                   source TEXT,
                   created_at REAL,
                   updated_at REAL )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS dataset_columns (
                   dataset_id TEXT NOT NULL,
                   ordinal INTEGER NOT NULL,
                   display TEXT NOT NULL,
                   name TEXT NOT NULL,
                   type TEXT NOT NULL,
                   PRIMARY KEY (dataset_id, name),
                   FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE CASCADE )"""
        )
        c.commit()
        _conn = c
    return _conn


def _q(identifier: str) -> str:
    """Quote a SQL identifier (defence-in-depth; names are already sanitised)."""
    return '"' + identifier.replace('"', '""') + '"'


def _new_id() -> str:
    while True:
        i = uuid.uuid4().hex[:8]
        if not _db().execute("SELECT 1 FROM datasets WHERE id=?", (i,)).fetchone():
            return i


def _sanitize(display: str, taken: set[str]) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", (display or "").strip().lower()).strip("_")
    if not s:
        s = "col"
    if s[0].isdigit():
        s = "c_" + s
    base, i = s, 2
    while s in taken or s in _RESERVED:
        s = f"{base}_{i}"
        i += 1
    taken.add(s)
    return s


def _norm_type(t: str | None) -> str:
    t = (t or "text").strip().lower()
    return t if t in LOGICAL_TYPES else "text"


def _normalize_columns(columns: list[dict | str]) -> list[dict]:
    """Accept ['Name', {'name':'Rank','type':'number'}, ...] → canonical column
    specs with sanitised physical names. ``name``/``display`` are user-facing."""
    out: list[dict] = []
    taken: set[str] = set()
    for col in columns:
        if isinstance(col, str):
            display, typ = col, "text"
        else:
            display = col.get("display") or col.get("name") or "col"
            typ = col.get("type")
        out.append({"display": str(display), "name": _sanitize(str(display), taken),
                    "type": _norm_type(typ)})
    return out


# ---------------------------------------------------------------------- introspection
def _columns(ds_id: str) -> list[dict]:
    rows = _db().execute(
        "SELECT display, name, type, ordinal FROM dataset_columns WHERE dataset_id=? ORDER BY ordinal",
        (ds_id,),
    ).fetchall()
    return [{"display": r["display"], "name": r["name"], "type": r["type"]} for r in rows]


def _row_count(table: str) -> int:
    try:
        return _db().execute(f"SELECT COUNT(*) AS n FROM {_q(table)}").fetchone()["n"]
    except sqlite3.Error:
        return 0


def _dataset_dict(row: sqlite3.Row, with_count: bool = True) -> dict:
    cols = _columns(row["id"])
    return {
        "id": row["id"],
        "name": row["name"],
        "table": row["table_name"],
        "columns": cols,
        "dedupKeys": json.loads(row["dedup_keys"] or "[]"),
        "source": json.loads(row["source"] or "null"),
        "rowCount": _row_count(row["table_name"]) if with_count else None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _get_row(ds_id: str) -> sqlite3.Row | None:
    return _db().execute("SELECT * FROM datasets WHERE id=?", (ds_id,)).fetchone()


def _touch(ds_id: str) -> None:
    _db().execute("UPDATE datasets SET updated_at=? WHERE id=?", (time.time(), ds_id))


def _col_map(ds_id: str) -> dict[str, dict]:
    """display(lower) -> column spec, for resilient incoming-row mapping."""
    return {c["display"].strip().lower(): c for c in _columns(ds_id)}


# ---------------------------------------------------------------------- CRUD
def list_datasets() -> list[dict]:
    with _lock:
        rows = _db().execute("SELECT * FROM datasets ORDER BY updated_at DESC").fetchall()
        return [_dataset_dict(r) for r in rows]


def get_dataset(ds_id: str) -> dict | None:
    with _lock:
        r = _get_row(ds_id)
        return _dataset_dict(r) if r else None


def create_dataset(name: str, columns: list[dict | str] | None = None,
                   dedup_keys: list[str] | None = None, source: Any = None,
                   rows: list[dict] | None = None) -> dict:
    with _lock:
        c = _db()
        ds_id = _new_id()
        table = f"ds_{ds_id}"
        cols = _normalize_columns(columns or [])
        defs = ['"_rid" INTEGER PRIMARY KEY AUTOINCREMENT', '"_added_at" REAL']
        defs += [f'{_q(col["name"])} {_AFFINITY[col["type"]]}' for col in cols]
        c.execute(f"CREATE TABLE {_q(table)} ({', '.join(defs)})")
        now = time.time()
        c.execute(
            "INSERT INTO datasets (id, name, table_name, dedup_keys, source, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (ds_id, name or "Untitled", table, json.dumps(dedup_keys or []),
             json.dumps(source) if source is not None else None, now, now),
        )
        for ordinal, col in enumerate(cols):
            c.execute(
                "INSERT INTO dataset_columns (dataset_id, ordinal, display, name, type) VALUES (?,?,?,?,?)",
                (ds_id, ordinal, col["display"], col["name"], col["type"]),
            )
        c.commit()
        if rows:
            append_rows(ds_id, rows, dedup=False)
        return get_dataset(ds_id)  # type: ignore[return-value]


def rename_dataset(ds_id: str, name: str) -> dict | None:
    with _lock:
        if not _get_row(ds_id):
            return None
        _db().execute("UPDATE datasets SET name=?, updated_at=? WHERE id=?", (name, time.time(), ds_id))
        _db().commit()
        return get_dataset(ds_id)


def set_dedup_keys(ds_id: str, keys: list[str]) -> dict | None:
    with _lock:
        if not _get_row(ds_id):
            return None
        valid = {c["display"] for c in _columns(ds_id)}
        keys = [k for k in keys if k in valid]
        _db().execute("UPDATE datasets SET dedup_keys=?, updated_at=? WHERE id=?",
                      (json.dumps(keys), time.time(), ds_id))
        _db().commit()
        return get_dataset(ds_id)


def delete_dataset(ds_id: str) -> bool:
    with _lock:
        row = _get_row(ds_id)
        if not row:
            return False
        _db().execute(f"DROP TABLE IF EXISTS {_q(row['table_name'])}")
        _db().execute("DELETE FROM dataset_columns WHERE dataset_id=?", (ds_id,))
        _db().execute("DELETE FROM datasets WHERE id=?", (ds_id,))
        _db().commit()
        return True


# ---------------------------------------------------------------------- schema edits
def add_column(ds_id: str, display: str, type: str = "text") -> dict | None:
    with _lock:
        row = _get_row(ds_id)
        if not row:
            return None
        taken = {c["name"] for c in _columns(ds_id)}
        name = _sanitize(display, taken)
        typ = _norm_type(type)
        ordinal = (_db().execute("SELECT COALESCE(MAX(ordinal),-1)+1 AS n FROM dataset_columns WHERE dataset_id=?",
                                 (ds_id,)).fetchone()["n"])
        _db().execute(f"ALTER TABLE {_q(row['table_name'])} ADD COLUMN {_q(name)} {_AFFINITY[typ]}")
        _db().execute("INSERT INTO dataset_columns (dataset_id, ordinal, display, name, type) VALUES (?,?,?,?,?)",
                      (ds_id, ordinal, display, name, typ))
        _touch(ds_id)
        _db().commit()
        return get_dataset(ds_id)


def drop_column(ds_id: str, display: str) -> dict | None:
    with _lock:
        row = _get_row(ds_id)
        if not row:
            return None
        col = next((c for c in _columns(ds_id) if c["display"] == display), None)
        if not col:
            return get_dataset(ds_id)
        _db().execute(f"ALTER TABLE {_q(row['table_name'])} DROP COLUMN {_q(col['name'])}")
        _db().execute("DELETE FROM dataset_columns WHERE dataset_id=? AND name=?", (ds_id, col["name"]))
        keys = [k for k in json.loads(row["dedup_keys"] or "[]") if k != display]
        _db().execute("UPDATE datasets SET dedup_keys=? WHERE id=?", (json.dumps(keys), ds_id))
        _touch(ds_id)
        _db().commit()
        return get_dataset(ds_id)


def rename_column(ds_id: str, old_display: str, new_display: str) -> dict | None:
    """Rename the *display* name only (physical column unchanged — no data move)."""
    with _lock:
        row = _get_row(ds_id)
        if not row:
            return None
        _db().execute("UPDATE dataset_columns SET display=? WHERE dataset_id=? AND display=?",
                      (new_display, ds_id, old_display))
        keys = [new_display if k == old_display else k for k in json.loads(row["dedup_keys"] or "[]")]
        _db().execute("UPDATE datasets SET dedup_keys=? WHERE id=?", (json.dumps(keys), ds_id))
        _touch(ds_id)
        _db().commit()
        return get_dataset(ds_id)


# ---------------------------------------------------------------------- rows
def _coerce(value: Any, typ: str) -> Any:
    if value is None:
        return None
    if typ == "number":
        if isinstance(value, (int, float)):
            return value
        s = str(value).strip().replace(",", "")
        try:
            f = float(s)
            return int(f) if f.is_integer() else f
        except ValueError:
            return str(value)  # keep as text rather than lose data
    if typ == "boolean":
        if isinstance(value, bool):
            return 1 if value else 0
        return 1 if str(value).strip().lower() in {"1", "true", "yes", "y", "on"} else 0
    return str(value)


def get_rows(ds_id: str, limit: int = 200, offset: int = 0, search: str = "",
             sort: str | None = None, direction: str = "asc") -> dict:
    with _lock:
        row = _get_row(ds_id)
        if not row:
            return {"columns": [], "rows": [], "count": 0}
        cols = _columns(ds_id)
        table = row["table_name"]
        where, params = "", []
        if search:
            clauses = [f"CAST({_q(c['name'])} AS TEXT) LIKE ?" for c in cols]
            if clauses:
                where = "WHERE " + " OR ".join(clauses)
                params = [f"%{search}%"] * len(clauses)
        total = _db().execute(f"SELECT COUNT(*) AS n FROM {_q(table)} {where}", params).fetchone()["n"]
        order = "ORDER BY \"_rid\" ASC"
        sort_col = next((c for c in cols if c["display"] == sort or c["name"] == sort), None)
        if sort_col:
            order = f"ORDER BY {_q(sort_col['name'])} {'DESC' if str(direction).lower() == 'desc' else 'ASC'}"
        sel = ", ".join([_q("_rid")] + [_q(c["name"]) for c in cols])
        q = f"SELECT {sel} FROM {_q(table)} {where} {order} LIMIT ? OFFSET ?"
        recs = _db().execute(q, params + [max(1, min(int(limit), 5000)), max(0, int(offset))]).fetchall()
        out = []
        for r in recs:
            d = {"_rid": r["_rid"]}
            for c in cols:
                d[c["display"]] = r[c["name"]]
            out.append(d)
        return {"columns": cols, "rows": out, "count": total}


def append_rows(ds_id: str, rows: list[dict], dedup: bool = True, extend: bool = False) -> dict:
    """Append incoming rows (keyed by *display* name, case-insensitive). When the
    dataset declares dedup keys and ``dedup`` is on, rows whose key already exists
    (or repeats within this batch) are skipped. With ``extend`` new incoming
    columns are added to the dataset schema (text)."""
    with _lock:
        row = _get_row(ds_id)
        if not row:
            return {"inserted": 0, "skipped": 0, "error": "dataset not found"}
        table = row["table_name"]
        if extend:
            known = {c["display"].strip().lower() for c in _columns(ds_id)}
            seen_new: set[str] = set()
            for r in rows:
                for k in r.keys():
                    kl = str(k).strip().lower()
                    if kl and kl not in known and kl not in seen_new:
                        add_column(ds_id, str(k), "text")
                        seen_new.add(kl)
        cmap = _col_map(ds_id)
        cols = _columns(ds_id)
        keys = json.loads(row["dedup_keys"] or "[]") if dedup else []
        existing: set = set()
        if keys:
            keyphys = [cmap[k.strip().lower()]["name"] for k in keys if k.strip().lower() in cmap]
            if keyphys:
                sel = ", ".join(_q(p) for p in keyphys)
                for er in _db().execute(f"SELECT {sel} FROM {_q(table)}").fetchall():
                    existing.add(tuple("" if er[p] is None else str(er[p]) for p in keyphys))
            else:
                keys = []
        inserted = skipped = 0
        now = time.time()
        for r in rows:
            mapped: dict[str, Any] = {}
            for k, v in r.items():
                col = cmap.get(str(k).strip().lower())
                if col:
                    mapped[col["name"]] = _coerce(v, col["type"])
            if keys:
                keyphys = [cmap[k.strip().lower()]["name"] for k in keys if k.strip().lower() in cmap]
                ktuple = tuple("" if mapped.get(p) is None else str(mapped.get(p)) for p in keyphys)
                if ktuple in existing:
                    skipped += 1
                    continue
                existing.add(ktuple)
            phys = ["_added_at"] + list(mapped.keys())
            vals = [now] + list(mapped.values())
            placeholders = ", ".join("?" for _ in phys)
            _db().execute(
                f"INSERT INTO {_q(table)} ({', '.join(_q(p) for p in phys)}) VALUES ({placeholders})", vals)
            inserted += 1
        _touch(ds_id)
        _db().commit()
        return {"inserted": inserted, "skipped": skipped}


def insert_row(ds_id: str, values: dict) -> dict:
    return append_rows(ds_id, [values], dedup=False)


def update_cell(ds_id: str, rid: int, display: str, value: Any) -> bool:
    with _lock:
        row = _get_row(ds_id)
        if not row:
            return False
        col = next((c for c in _columns(ds_id) if c["display"] == display), None)
        if not col:
            return False
        _db().execute(f"UPDATE {_q(row['table_name'])} SET {_q(col['name'])}=? WHERE \"_rid\"=?",
                      (_coerce(value, col["type"]), int(rid)))
        _touch(ds_id)
        _db().commit()
        return True


def delete_rows(ds_id: str, rids: list[int]) -> int:
    with _lock:
        row = _get_row(ds_id)
        if not row or not rids:
            return 0
        marks = ", ".join("?" for _ in rids)
        cur = _db().execute(f"DELETE FROM {_q(row['table_name'])} WHERE \"_rid\" IN ({marks})",
                            [int(x) for x in rids])
        _touch(ds_id)
        _db().commit()
        return cur.rowcount


def dedup(ds_id: str, keys: list[str] | None = None) -> dict:
    """Remove duplicate rows, keeping the first (lowest _rid) per key. Keys default
    to the dataset's dedup keys, else every column."""
    with _lock:
        row = _get_row(ds_id)
        if not row:
            return {"removed": 0}
        cmap = _col_map(ds_id)
        keys = keys or json.loads(row["dedup_keys"] or "[]") or [c["display"] for c in _columns(ds_id)]
        keyphys = [cmap[k.strip().lower()]["name"] for k in keys if k.strip().lower() in cmap]
        if not keyphys:
            return {"removed": 0}
        table = row["table_name"]
        grp = ", ".join(_q(p) for p in keyphys)
        before = _row_count(table)
        _db().execute(
            f"DELETE FROM {_q(table)} WHERE \"_rid\" NOT IN "
            f"(SELECT MIN(\"_rid\") FROM {_q(table)} GROUP BY {grp})")
        _touch(ds_id)
        _db().commit()
        return {"removed": before - _row_count(table)}


# ---------------------------------------------------------------------- project / merge
def project(src_id: str, columns: list[Any], new_name: str,
            dedup_keys: list[str] | None = None) -> dict | None:
    """Create a NEW dataset from selected (optionally renamed) columns of another,
    copying all rows. ``columns`` items are display names or {'from','to'} maps.
    The canonical way to prep a tidy input for the next workflow."""
    with _lock:
        src = _get_row(src_id)
        if not src:
            return None
        srccols = {c["display"]: c for c in _columns(src_id)}
        picks: list[tuple[dict, str]] = []  # (source col, target display)
        for item in columns:
            if isinstance(item, str):
                frm, to = item, item
            else:
                frm, to = item.get("from"), item.get("to") or item.get("from")
            if frm in srccols:
                picks.append((srccols[frm], to))
        if not picks:
            return None
        new = create_dataset(new_name, [{"display": to, "type": col["type"]} for col, to in picks],
                             dedup_keys=dedup_keys, source={"kind": "project", "from": src_id})
        # copy rows
        sel = ", ".join(_q(col["name"]) for col, _ in picks)
        tgtcols = _columns(new["id"])
        tgt_table = new["table"]
        rows = _db().execute(f"SELECT {sel} FROM {_q(src['table_name'])} ORDER BY \"_rid\"").fetchall()
        now = time.time()
        for r in rows:
            phys = ["_added_at"] + [tc["name"] for tc in tgtcols]
            vals = [now] + [r[col["name"]] for col, _ in picks]
            ph = ", ".join("?" for _ in phys)
            _db().execute(f"INSERT INTO {_q(tgt_table)} ({', '.join(_q(p) for p in phys)}) VALUES ({ph})", vals)
        _touch(new["id"])
        _db().commit()
        if dedup_keys:
            dedup(new["id"])
        return get_dataset(new["id"])


def merge(ids: list[str], new_name: str, dedup_keys: list[str] | None = None) -> dict | None:
    """Union several datasets into a new one, reconciling columns by display name
    (type → 'text' on conflict), null-filling missing columns; optional dedup."""
    with _lock:
        sources = [_get_row(i) for i in ids]
        sources = [s for s in sources if s]
        if not sources:
            return None
        union: dict[str, str] = {}  # display -> type
        order: list[str] = []
        for s in sources:
            for c in _columns(s["id"]):
                if c["display"] not in union:
                    union[c["display"]] = c["type"]
                    order.append(c["display"])
                elif union[c["display"]] != c["type"]:
                    union[c["display"]] = "text"
        new = create_dataset(new_name, [{"display": d, "type": union[d]} for d in order],
                             dedup_keys=dedup_keys, source={"kind": "merge", "from": ids})
        for s in sources:
            srcrows = get_rows(s["id"], limit=5000, offset=0)
            # paginate large sources
            fetched = srcrows["rows"]
            page = 1
            while len(fetched) < srcrows["count"]:
                more = get_rows(s["id"], limit=5000, offset=page * 5000)["rows"]
                if not more:
                    break
                fetched += more
                page += 1
            append_rows(new["id"], [{k: v for k, v in r.items() if k != "_rid"} for r in fetched], dedup=False)
        if dedup_keys:
            dedup(new["id"])
        return get_dataset(new["id"])


# ---------------------------------------------------------------------- CSV in/out
def ingest_csv(csv_path: str | Path, target_id: str | None = None, name: str | None = None,
               dedup_keys: list[str] | None = None, source: Any = None,
               columns: list[dict] | None = None) -> dict:
    """Import a CSV into a new dataset (``target_id`` None) or append into an
    existing one (extending its schema with any new columns). When creating and a
    ``columns`` contract is given (e.g. a workflow's declared output_contract),
    those typed columns are adopted; any extra header columns are added as text."""
    path = Path(csv_path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csvmod.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    rows = [dict(r) for r in reader]
    with _lock:
        if target_id is None:
            if columns:
                typed = {c["name"].strip().lower(): c.get("type", "text") for c in columns}
                schema = [{"display": h, "type": typed.get(h.strip().lower(), "text")} for h in header]
                # include any contract columns not present in the CSV header
                hset = {h.strip().lower() for h in header}
                schema += [{"display": c["name"], "type": c.get("type", "text")}
                           for c in columns if c["name"].strip().lower() not in hset]
            else:
                schema = [{"display": h, "type": "text"} for h in header]
            ds = create_dataset(name or path.stem, schema,
                                dedup_keys=dedup_keys, source=source or {"kind": "import", "file": path.name})
            res = append_rows(ds["id"], rows, dedup=bool(dedup_keys))
            return {"datasetId": ds["id"], "created": True, **res}
        else:
            res = append_rows(target_id, rows, dedup=True, extend=True)
            return {"datasetId": target_id, "created": False, **res}


def to_csv_text(ds_id: str) -> str:
    with _lock:
        row = _get_row(ds_id)
        if not row:
            return ""
        cols = _columns(ds_id)
        buf = io.StringIO()
        w = csvmod.DictWriter(buf, fieldnames=[c["display"] for c in cols])
        w.writeheader()
        offset = 0
        while True:
            page = get_rows(ds_id, limit=5000, offset=offset)["rows"]
            if not page:
                break
            for r in page:
                w.writerow({c["display"]: ("" if r.get(c["display"]) is None else r[c["display"]]) for c in cols})
            offset += len(page)
            if len(page) < 5000:
                break
        return buf.getvalue()


def export_csv(ds_id: str) -> Path | None:
    with _lock:
        row = _get_row(ds_id)
        if not row:
            return None
        out_dir = data_dir() / "artifacts"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"dataset-{ds_id}.csv"
        path.write_text(to_csv_text(ds_id), encoding="utf-8-sig")
        return path


# ---------------------------------------------------------------------- agent SQL
_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
                        r"pragma|vacuum|reindex|begin|commit|rollback)\b", re.IGNORECASE)


def query(sql: str, max_rows: int = 5000) -> dict:
    """Run a single read-only SELECT/WITH against the datasets DB. Used by agents
    and power users. Executes on a read-only connection so it can never mutate."""
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        return {"error": "empty query"}
    if ";" in s:
        return {"error": "only a single statement is allowed"}
    if not re.match(r"^(select|with)\b", s, re.IGNORECASE):
        return {"error": "only SELECT/WITH queries are allowed"}
    if _FORBIDDEN.search(s):
        return {"error": "query contains a forbidden keyword (read-only access)"}
    try:
        ro = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        try:
            ro.execute("PRAGMA query_only=ON")
            cur = ro.execute(s)
            cols = [d[0] for d in cur.description] if cur.description else []
            recs = cur.fetchmany(max_rows + 1)
            truncated = len(recs) > max_rows
            data = [dict(r) for r in recs[:max_rows]]
            return {"columns": cols, "rows": data, "count": len(data), "truncated": truncated}
        finally:
            ro.close()
    except sqlite3.Error as e:
        return {"error": str(e)}


def schema_summary() -> list[dict]:
    """Compact schema for an agent: each dataset's id, name, physical table and
    columns (physical name + type), so it can write SQL against ``query``."""
    out = []
    for ds in list_datasets():
        out.append({
            "id": ds["id"], "name": ds["name"], "table": ds["table"], "rowCount": ds["rowCount"],
            "columns": [{"name": c["name"], "display": c["display"], "type": c["type"]} for c in ds["columns"]],
        })
    return out
