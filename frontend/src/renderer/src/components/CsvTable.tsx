
import { useEffect, useMemo, useState } from "react";
import { Icon } from "./Icon";
import { FileChip, FileChipList, parseFileList } from "./FilePreview";
import { jget, downloadUrl } from "@/lib/client";
import type { ColumnType } from "@/lib/types";

interface Result { columns: string[]; rows: Record<string, string>[]; count: number }

// `columnTypes` (optional): per-column type from the workflow's output_contract,
// used to render `file` / `file_list` cells as thumbnails instead of raw ids.
// Missing entries default to plain-text rendering — fully backward-compatible.
export function CsvTable({ runId, columnTypes }: { runId: string; columnTypes?: Record<string, ColumnType> }) {
  const [data, setData] = useState<Result | null>(null);
  const [error, setError] = useState("");
  const [q, setQ] = useState("");
  const [sortCol, setSortCol] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<1 | -1>(1);

  useEffect(() => {
    jget<Result>(`/api/runs/${runId}/result`).then(setData).catch((e) => setError(String(e.message)));
  }, [runId]);

  const rows = useMemo(() => {
    if (!data) return [];
    let r = data.rows;
    if (q.trim()) {
      const needle = q.toLowerCase();
      r = r.filter((row) => data.columns.some((c) => (row[c] ?? "").toLowerCase().includes(needle)));
    }
    if (sortCol) {
      r = [...r].sort((a, b) => {
        const av = a[sortCol] ?? "", bv = b[sortCol] ?? "";
        const an = Number(av), bn = Number(bv);
        const cmp = !isNaN(an) && !isNaN(bn) && av !== "" && bv !== "" ? an - bn : av.localeCompare(bv);
        return cmp * sortDir;
      });
    }
    return r;
  }, [data, q, sortCol, sortDir]);

  if (error) return <div className="card p-6 text-[13px] text-faint">No results to display ({error}).</div>;
  if (!data) return <div className="card p-6 text-[13px] text-faint">Loading results…</div>;

  const isUrl = (s: string) => /^https?:\/\//.test(s);
  const toggleSort = (c: string) => {
    if (sortCol === c) setSortDir((d) => (d === 1 ? -1 : 1));
    else { setSortCol(c); setSortDir(1); }
  };

  return (
    <div>
      <div className="flex items-center gap-3 mb-3">
        <div className="relative flex-1 max-w-[360px]">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-faint"><Icon name="search" size={14} /></span>
          <input className="input pl-9" placeholder="Filter results…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <span className="text-[12px] text-faint mono">
          {rows.length}{rows.length !== data.count ? ` / ${data.count}` : ""} rows
        </span>
        <a href={downloadUrl(`/api/runs/${runId}/download`)} className="btn btn-secondary btn-sm ml-auto">
          <Icon name="download" size={13} /> CSV
        </a>
      </div>

      <div className="card overflow-auto" style={{ maxHeight: 540 }}>
        <table className="w-full text-[12.5px] border-collapse">
          <thead className="sticky top-0 z-10" style={{ background: "#0f0f0f" }}>
            <tr>
              {data.columns.map((c) => (
                <th
                  key={c}
                  onClick={() => toggleSort(c)}
                  className="text-left font-medium text-muted px-3 py-2.5 cursor-pointer whitespace-nowrap border-b select-none hover:text-fg"
                  style={{ borderColor: "var(--color-line)" }}
                >
                  {c} {sortCol === c && (sortDir === 1 ? "↑" : "↓")}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} className="border-b hover:bg-elevated/40" style={{ borderColor: "#171717" }}>
                {data.columns.map((c) => {
                  const v = row[c];
                  const t = columnTypes?.[c];
                  return (
                    <td key={c} className="px-3 py-2 align-top" style={{ maxWidth: 360 }}>
                      {t === "file" ? (
                        v ? <FileChip id={String(v)} /> : <span className="text-faint">—</span>
                      ) : t === "file_list" ? (
                        <FileChipList ids={parseFileList(v)} />
                      ) : isUrl(v) ? (
                        <a href={v} target="_blank" rel="noreferrer" className="text-running hover:underline break-all">{v}</a>
                      ) : (
                        <span className="text-fg/90 break-words">{v}</span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <div className="p-6 text-center text-[13px] text-faint">No matching rows.</div>}
      </div>
    </div>
  );
}
