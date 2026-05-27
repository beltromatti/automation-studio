import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { jget, jpost, jdel, downloadUrl } from "@/lib/client";
import type { ColumnType, Dataset, DatasetColumn, DatasetRows } from "@/lib/types";

const PAGE = 100;
const isUrl = (s: unknown) => typeof s === "string" && /^https?:\/\//.test(s);

export default function DatasetPage() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [ds, setDs] = useState<Dataset | null>(null);
  const [data, setData] = useState<DatasetRows | null>(null);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<string | null>(null);
  const [dir, setDir] = useState<"asc" | "desc">("asc");
  const [offset, setOffset] = useState(0);
  const [edit, setEdit] = useState<{ rid: number; col: string } | null>(null);
  const [draft, setDraft] = useState("");
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [menuCol, setMenuCol] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [showSql, setShowSql] = useState(false);
  const [showProject, setShowProject] = useState(false);
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");

  const loadMeta = useCallback(async () => {
    try { setDs((await jget<{ dataset: Dataset }>(`/api/datasets/${id}`)).dataset); } catch {}
  }, [id]);

  const loadRows = useCallback(async () => {
    try {
      const qs = new URLSearchParams({ limit: String(PAGE), offset: String(offset), search });
      if (sort) { qs.set("sort", sort); qs.set("direction", dir); }
      setData(await jget<DatasetRows>(`/api/datasets/${id}/rows?${qs}`));
    } catch (e) { setError(String((e as Error).message)); }
  }, [id, offset, search, sort, dir]);

  useEffect(() => { loadMeta(); }, [loadMeta]);
  useEffect(() => { loadRows(); }, [loadRows]);
  // reset to first page when the filter/sort changes
  useEffect(() => { setOffset(0); }, [search, sort, dir]);

  const reload = async () => { await Promise.all([loadMeta(), loadRows()]); };

  const cols = ds?.columns ?? [];
  const dedupKeys = new Set(ds?.dedupKeys ?? []);

  const commitCell = async () => {
    if (!edit) return;
    const { rid, col } = edit;
    setEdit(null);
    try {
      await jpost(`/api/datasets/${id}/cell`, { rid, column: col, value: draft });
      setData((d) => d && { ...d, rows: d.rows.map((r) => (r._rid === rid ? { ...r, [col]: draft } : r)) });
    } catch (e) { setError(String((e as Error).message)); }
  };

  const toggleSort = (c: string) => {
    if (sort === c) setDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSort(c); setDir("asc"); }
  };

  const addColumn = async () => {
    const name = prompt("New column name:");
    if (!name) return;
    const type = (prompt("Type — text | number | boolean", "text") || "text").toLowerCase() as ColumnType;
    await jpost(`/api/datasets/${id}/add-column`, { display: name, type }).catch((e) => setError(String(e.message)));
    reload();
  };

  const addRow = async () => {
    await jpost(`/api/datasets/${id}/rows`, { values: {} }).catch((e) => setError(String(e.message)));
    reload();
  };

  const dedup = async () => {
    const res = await jpost<{ removed: number }>(`/api/datasets/${id}/dedup`, {}).catch((e) => { setError(String(e.message)); return null; });
    if (res) setError(res.removed > 0 ? `Removed ${res.removed} duplicate row(s).` : "No duplicates found.");
    reload();
  };

  const toggleDedupKey = async (display: string) => {
    const next = new Set(dedupKeys);
    next.has(display) ? next.delete(display) : next.add(display);
    await jpost(`/api/datasets/${id}/dedup-keys`, { keys: [...next] }).catch((e) => setError(String(e.message)));
    setMenuCol(null); loadMeta();
  };

  const renameColumn = async (from: string) => {
    const to = prompt("Rename column:", from);
    setMenuCol(null);
    if (!to || to === from) return;
    await jpost(`/api/datasets/${id}/rename-column`, { from, to }).catch((e) => setError(String(e.message)));
    reload();
  };

  const dropColumn = async (display: string) => {
    setMenuCol(null);
    if (!confirm(`Drop column "${display}" and its data?`)) return;
    await jpost(`/api/datasets/${id}/drop-column`, { display }).catch((e) => setError(String(e.message)));
    reload();
  };

  const deleteSelected = async () => {
    if (sel.size === 0) return;
    await jpost(`/api/datasets/${id}/delete-rows`, { rids: [...sel] }).catch((e) => setError(String(e.message)));
    setSel(new Set()); reload();
  };

  const saveName = async () => {
    setEditingName(false);
    const v = nameDraft.trim();
    if (!v || v === ds?.name) return;
    await jpost(`/api/datasets/${id}/rename`, { name: v }).catch((e) => setError(String(e.message)));
    loadMeta();
  };

  const del = async () => {
    if (!confirm(`Delete dataset "${ds?.name}"? This cannot be undone.`)) return;
    await jdel(`/api/datasets/${id}`).catch(() => {});
    navigate("/data");
  };

  if (!ds) return (<><Header title="…" /><div className="px-7 py-6 text-faint text-[13px]">Loading dataset…</div></>);

  const count = data?.count ?? 0;
  const from = count === 0 ? 0 : offset + 1;
  const to = Math.min(offset + PAGE, count);

  return (
    <>
      <Header
        title={
          <span className="flex items-center gap-2">
            <Link to="/data" className="text-muted hover:text-fg">Data</Link>
            <Icon name="chevronRight" size={14} />
            {editingName ? (
              <input autoFocus className="input" style={{ height: 28, width: 220 }} value={nameDraft}
                     onChange={(e) => setNameDraft(e.target.value)} onBlur={saveName}
                     onKeyDown={(e) => { if (e.key === "Enter") saveName(); if (e.key === "Escape") setEditingName(false); }} />
            ) : (
              <span className="font-semibold cursor-pointer hover:text-fg" onClick={() => { setNameDraft(ds.name); setEditingName(true); }}>{ds.name}</span>
            )}
          </span>
        }
        actions={
          <>
            <button className="btn btn-secondary btn-sm" onClick={addColumn}><Icon name="columns" size={13} /> Column</button>
            <button className="btn btn-secondary btn-sm" onClick={addRow}><Icon name="plus" size={13} /> Row</button>
            <button className="btn btn-secondary btn-sm" onClick={dedup} title="Remove duplicate rows by the dedup key(s)"><Icon name="filter" size={13} /> Dedup</button>
            <button className="btn btn-secondary btn-sm" onClick={() => setShowProject(true)} title="Create a new dataset from selected columns (prep an input)"><Icon name="arrowRight" size={13} /> Project</button>
            <button className="btn btn-secondary btn-sm" onClick={() => setShowSql(true)}><Icon name="code" size={13} /> SQL</button>
            <a className="btn btn-secondary btn-sm" href={downloadUrl(`/api/datasets/${id}/export`)}><Icon name="download" size={13} /> CSV</a>
            <button className="btn btn-secondary btn-sm" onClick={del}><Icon name="trash" size={13} /></button>
          </>
        }
      />
      <div className="px-7 py-6 max-w-[1300px]">
        <div className="flex items-center gap-3 mb-3 flex-wrap">
          <div className="relative flex-1 max-w-[340px]">
            <span className="absolute left-3 top-1/2 -translate-y-1/2 text-faint"><Icon name="search" size={14} /></span>
            <input className="input pl-9" placeholder="Filter rows…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <span className="text-[12px] text-faint mono">{from}–{to} of {count} rows</span>
          <div className="flex items-center gap-1.5">
            <button className="btn btn-secondary btn-sm" disabled={offset === 0} onClick={() => setOffset((o) => Math.max(0, o - PAGE))}>Prev</button>
            <button className="btn btn-secondary btn-sm" disabled={to >= count} onClick={() => setOffset((o) => o + PAGE)}>Next</button>
          </div>
          {sel.size > 0 && (
            <button className="btn btn-danger btn-sm ml-auto" onClick={deleteSelected}><Icon name="trash" size={13} /> Delete {sel.size}</button>
          )}
          <span className="text-[11px] text-faint mono ml-auto" style={{ marginLeft: sel.size > 0 ? 8 : "auto" }}>{ds.table}</span>
        </div>

        {error && <div className="text-[12px] text-warning mb-3">{error}</div>}
        {dedupKeys.size > 0 && (
          <div className="flex items-center gap-1.5 mb-3 text-[11.5px] text-faint">
            <Icon name="key" size={13} /> dedup key:
            {[...dedupKeys].map((k) => (
              <span key={k} className="px-1.5 py-0.5 rounded mono" style={{ background: "#0072f518", color: "#3b9eff" }}>{k}</span>
            ))}
          </div>
        )}

        {cols.length === 0 ? (
          <div className="card p-10 text-center text-[13px] text-faint flex flex-col items-center gap-2">
            <Icon name="columns" size={22} />
            No columns yet. Add a column, import a CSV, or capture a run's results into this dataset.
          </div>
        ) : (
          <div className="card overflow-auto" style={{ maxHeight: "calc(100vh - 230px)" }}>
            <table className="text-[12.5px] border-collapse" style={{ minWidth: "100%" }}>
              <thead className="sticky top-0 z-10" style={{ background: "#0f0f0f" }}>
                <tr>
                  <th className="px-2.5 py-2.5 border-b w-8" style={{ borderColor: "var(--color-line)" }}>
                    <input type="checkbox" checked={sel.size > 0 && sel.size === (data?.rows.length ?? 0)}
                           onChange={(e) => setSel(e.target.checked ? new Set((data?.rows ?? []).map((r) => r._rid)) : new Set())} />
                  </th>
                  {cols.map((c) => (
                    <ColumnHeader key={c.name} col={c} sort={sort} dir={dir} isKey={dedupKeys.has(c.display)}
                                  open={menuCol === c.display} onToggleMenu={() => setMenuCol((m) => (m === c.display ? null : c.display))}
                                  onSort={() => toggleSort(c.display)} onRename={() => renameColumn(c.display)}
                                  onDrop={() => dropColumn(c.display)} onToggleKey={() => toggleDedupKey(c.display)} />
                  ))}
                </tr>
              </thead>
              <tbody>
                {(data?.rows ?? []).map((row) => (
                  <tr key={row._rid} className="border-b hover:bg-elevated/40" style={{ borderColor: "#171717" }}>
                    <td className="px-2.5 py-2 align-top">
                      <input type="checkbox" checked={sel.has(row._rid)}
                             onChange={() => setSel((s) => { const n = new Set(s); n.has(row._rid) ? n.delete(row._rid) : n.add(row._rid); return n; })} />
                    </td>
                    {cols.map((c) => {
                      const editing = edit?.rid === row._rid && edit?.col === c.display;
                      const val = row[c.display];
                      return (
                        <td key={c.name} className="px-3 py-2 align-top" style={{ maxWidth: 360 }}
                            onClick={() => { if (!editing) { setEdit({ rid: row._rid, col: c.display }); setDraft(val == null ? "" : String(val)); } }}>
                          {editing ? (
                            <input autoFocus className="input" style={{ height: 26, minWidth: 80 }} value={draft}
                                   onChange={(e) => setDraft(e.target.value)} onBlur={commitCell}
                                   onClick={(e) => e.stopPropagation()}
                                   onKeyDown={(e) => { if (e.key === "Enter") commitCell(); if (e.key === "Escape") setEdit(null); }} />
                          ) : isUrl(val) ? (
                            <a href={String(val)} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} className="text-running hover:underline break-all">{String(val)}</a>
                          ) : (
                            <span className="text-fg/90 break-words">{val == null ? "" : String(val)}</span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
            {(data?.rows.length ?? 0) === 0 && <div className="p-6 text-center text-[13px] text-faint">No rows{search ? " match the filter" : " yet"}.</div>}
          </div>
        )}
      </div>

      {showSql && <SqlModal table={ds.table} onClose={() => setShowSql(false)}
                            onSaved={(nid) => navigate(`/data/${nid}`)} onChanged={reload} />}
      {showProject && <ProjectModal dataset={ds} onClose={() => setShowProject(false)} onDone={(nid) => navigate(`/data/${nid}`)} />}
    </>
  );
}

function ColumnHeader({ col, sort, dir, isKey, open, onToggleMenu, onSort, onRename, onDrop, onToggleKey }: {
  col: DatasetColumn; sort: string | null; dir: "asc" | "desc"; isKey: boolean; open: boolean;
  onToggleMenu: () => void; onSort: () => void; onRename: () => void; onDrop: () => void; onToggleKey: () => void;
}) {
  return (
    <th className="text-left font-medium text-muted px-3 py-2.5 whitespace-nowrap border-b select-none relative" style={{ borderColor: "var(--color-line)" }}>
      <div className="flex items-center gap-1.5">
        <span className="cursor-pointer hover:text-fg" onClick={onSort}>
          {col.display} {sort === col.display && (dir === "asc" ? "↑" : "↓")}
        </span>
        {isKey && <Icon name="key" size={11} />}
        <span className="text-[9px] text-faint mono">{col.type[0]}</span>
        <button className="ml-auto text-faint hover:text-fg" onClick={onToggleMenu}>⋯</button>
      </div>
      {open && (
        <div className="absolute right-2 top-9 z-30 card p-1 text-[12px] font-normal" style={{ background: "#161616", minWidth: 150 }}>
          <button className="w-full text-left px-2.5 py-1.5 rounded hover:bg-elevated flex items-center gap-2" onClick={onToggleKey}>
            <Icon name="key" size={13} /> {isKey ? "Remove dedup key" : "Use as dedup key"}
          </button>
          <button className="w-full text-left px-2.5 py-1.5 rounded hover:bg-elevated flex items-center gap-2" onClick={onRename}>
            <Icon name="pencil" size={13} /> Rename
          </button>
          <button className="w-full text-left px-2.5 py-1.5 rounded hover:bg-elevated text-danger flex items-center gap-2" onClick={onDrop}>
            <Icon name="trash" size={13} /> Drop column
          </button>
        </div>
      )}
    </th>
  );
}

function SqlModal({ table, onClose, onSaved, onChanged }:
                  { table: string; onClose: () => void; onSaved: (id: string) => void; onChanged: () => void }) {
  const [sql, setSql] = useState(`SELECT * FROM "${table}" LIMIT 50`);
  const [res, setRes] = useState<{ columns: string[]; rows: Record<string, unknown>[]; truncated?: boolean } | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const isRead = /^\s*(select|with)\b/i.test(sql);
  const run = async () => {
    setError(""); setRes(null); setNote("");
    try {
      if (isRead) {
        const r = await jpost<{ columns: string[]; rows: Record<string, unknown>[]; error?: string; truncated?: boolean }>("/api/datasets/query", { sql });
        if (r.error) setError(r.error); else setRes(r);
      } else {
        const r = await jpost<{ ok?: boolean; rowsAffected?: number; error?: string }>("/api/datasets/exec", { sql });
        if (r.error) setError(r.error);
        else { setNote(`${r.rowsAffected ?? 0} row${r.rowsAffected === 1 ? "" : "s"} affected.`); onChanged(); }
      }
    } catch (e) { setError(String((e as Error).message)); }
  };
  const saveAsDataset = async () => {
    setError("");
    const name = prompt("New dataset name for this query's result:");
    if (!name) return;
    try {
      const r = await jpost<{ id?: string; error?: string }>("/api/datasets/query-to-dataset", { sql, name });
      if (r.error || !r.id) setError(r.error || "could not create dataset");
      else onSaved(r.id);
    } catch (e) { setError(String((e as Error).message)); }
  };
  return (
    <Modal onClose={onClose} title="SQL console" wide>
      <p className="text-[11.5px] text-faint mb-2">
        <b>SELECT/WITH</b> to read; <b>INSERT/UPDATE/DELETE</b> to edit data in place. Join, aggregate, filter across all
        dataset tables, with <span className="mono">REGEXP</span>, <span className="mono">regexp_extract</span> and
        <span className="mono"> regexp_replace</span>. Save a SELECT as its own dataset. (Same power agents have.)
      </p>
      <textarea className="input mono" style={{ height: 90, padding: 10, lineHeight: 1.5 }} value={sql} onChange={(e) => setSql(e.target.value)} />
      <div className="flex gap-2 mt-2">
        <button className="btn btn-primary btn-sm" onClick={run}>
          <Icon name="play" size={13} /> {isRead ? "Run" : "Run (writes data)"}
        </button>
        <button className="btn btn-sm" onClick={saveAsDataset} disabled={!isRead} title={isRead ? "" : "Only SELECT/WITH can be saved as a dataset"}>
          <Icon name="database" size={13} /> Save as dataset…
        </button>
      </div>
      {note && <div className="text-[12px] text-success mt-3">{note}</div>}
      {error && <div className="text-[12px] text-danger mt-3">{error}</div>}
      {res && (
        <div className="card overflow-auto mt-3" style={{ maxHeight: 360 }}>
          <table className="w-full text-[12px] border-collapse">
            <thead className="sticky top-0" style={{ background: "#0f0f0f" }}>
              <tr>{res.columns.map((c) => <th key={c} className="text-left text-muted px-3 py-2 border-b whitespace-nowrap" style={{ borderColor: "var(--color-line)" }}>{c}</th>)}</tr>
            </thead>
            <tbody>
              {res.rows.map((r, i) => (
                <tr key={i} className="border-b" style={{ borderColor: "#171717" }}>
                  {res.columns.map((c) => <td key={c} className="px-3 py-1.5 align-top" style={{ maxWidth: 320 }}><span className="break-words">{r[c] == null ? "" : String(r[c])}</span></td>)}
                </tr>
              ))}
            </tbody>
          </table>
          <div className="px-3 py-1.5 text-[11px] text-faint">{res.rows.length} rows{res.truncated ? " (truncated)" : ""}</div>
        </div>
      )}
    </Modal>
  );
}

function ProjectModal({ dataset, onClose, onDone }: { dataset: Dataset; onClose: () => void; onDone: (id: string) => void }) {
  const [picked, setPicked] = useState<Record<string, { on: boolean; to: string }>>(
    () => Object.fromEntries(dataset.columns.map((c) => [c.display, { on: true, to: c.display }])));
  const [name, setName] = useState(`${dataset.name} (projection)`);
  const [keyCol, setKeyCol] = useState<string>("");
  const [error, setError] = useState("");
  const go = async () => {
    const columns = dataset.columns.filter((c) => picked[c.display]?.on).map((c) => ({ from: c.display, to: picked[c.display].to || c.display }));
    if (columns.length === 0) { setError("Pick at least one column."); return; }
    const dedupKeys = keyCol ? [picked[keyCol]?.to || keyCol] : undefined;
    try {
      const { dataset: nd } = await jpost<{ dataset: Dataset }>("/api/datasets/project", { srcId: dataset.id, columns, name, dedupKeys });
      onDone(nd.id);
    } catch (e) { setError(String((e as Error).message)); }
  };
  return (
    <Modal onClose={onClose} title="Project into a new dataset">
      <p className="text-[11.5px] text-faint mb-3">Pick (and optionally rename) columns to copy into a new dataset — the tidy way to prep an input for the next workflow.</p>
      <label className="label">New dataset name</label>
      <input className="input mb-3 mt-1" value={name} onChange={(e) => setName(e.target.value)} />
      <div className="flex flex-col gap-1.5 mb-3 max-h-[260px] overflow-auto">
        {dataset.columns.map((c) => (
          <div key={c.display} className="flex items-center gap-2">
            <input type="checkbox" checked={picked[c.display]?.on ?? false}
                   onChange={(e) => setPicked((p) => ({ ...p, [c.display]: { ...p[c.display], on: e.target.checked } }))} />
            <span className="text-[12px] w-[140px] truncate">{c.display}</span>
            <Icon name="arrowRight" size={12} />
            <input className="input" style={{ height: 28 }} value={picked[c.display]?.to ?? ""}
                   onChange={(e) => setPicked((p) => ({ ...p, [c.display]: { ...p[c.display], to: e.target.value } }))} />
          </div>
        ))}
      </div>
      <label className="label">Dedup key (optional)</label>
      <select className="input appearance-none mt-1 mb-3" value={keyCol} onChange={(e) => setKeyCol(e.target.value)}>
        <option value="">— none —</option>
        {dataset.columns.filter((c) => picked[c.display]?.on).map((c) => <option key={c.display} value={c.display}>{picked[c.display]?.to || c.display}</option>)}
      </select>
      {error && <div className="text-[12px] text-danger mb-2">{error}</div>}
      <button className="btn btn-primary btn-sm" onClick={go}><Icon name="arrowRight" size={13} /> Create projection</button>
    </Modal>
  );
}

function Modal({ title, children, onClose, wide }: { title: string; children: React.ReactNode; onClose: () => void; wide?: boolean }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "#000000aa" }} onClick={onClose}>
      <div className="card p-5" style={{ width: wide ? 720 : 520, maxWidth: "90vw" }} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <div className="text-[14px] font-semibold">{title}</div>
          <button className="text-faint hover:text-fg" onClick={onClose}><Icon name="x" size={16} /></button>
        </div>
        {children}
      </div>
    </div>
  );
}
