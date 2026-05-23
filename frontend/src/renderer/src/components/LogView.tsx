
import { useEffect, useRef } from "react";

export function LogView({ lines }: { lines: string[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);

  useEffect(() => {
    const el = ref.current;
    if (el && atBottom.current) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const color = (l: string) => {
    if (l.startsWith("[stderr]") || /error|failed|traceback/i.test(l)) return "#ff7b7b";
    if (l.startsWith("[progress]")) return "#3b9eff";
    if (l.startsWith("[console]")) return "#2bd576";
    if (l.startsWith("[browser]")) return "#6e6e6e";
    return "#c4c4c4";
  };

  return (
    <div
      ref={ref}
      onScroll={(e) => {
        const el = e.currentTarget;
        atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}
      className="card mono text-[12px] leading-[1.6] p-3.5 overflow-auto"
      style={{ height: 320, background: "#070707" }}
    >
      {lines.length === 0 && <div className="text-faint">waiting for output…</div>}
      {lines.map((l, i) => (
        <div key={i} style={{ color: color(l), whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{l}</div>
      ))}
    </div>
  );
}
