
import { Link, useLocation } from "react-router-dom";
import { Icon } from "./Icon";
import { useRuns } from "./RunsProvider";
import iconUrl from "@/assets/icon.png";

const NAV = [
  { href: "/", label: "Workflows", icon: "layers" },
  { href: "/runs", label: "Runs", icon: "terminal" },
  { href: "/profiles", label: "Profiles", icon: "user" },
];

export function Sidebar() {
  const pathname = useLocation().pathname;
  const { runs } = useRuns();
  const active = runs.filter((r) => ["running", "starting", "controlled"].includes(r.status)).length;

  const is = (href: string) => (href === "/" ? pathname === "/" || pathname.startsWith("/workflows") : pathname.startsWith(href));

  return (
    <aside className="w-[248px] shrink-0 border-r flex flex-col bg-panel" style={{ borderColor: "var(--color-line)" }}>
      <div className="app-drag sidebar-titlebar h-[60px] flex items-center gap-2.5 px-4 border-b" style={{ borderColor: "var(--color-line)" }}>
        <img src={iconUrl} alt="" className="brand-logo shrink-0 select-none" style={{ height: 28, width: 28, objectFit: "contain" }} draggable={false} />
        <div className="text-[13px] font-semibold truncate min-w-0">Automation Studio</div>
      </div>

      <nav className="p-3 flex flex-col gap-0.5">
        {NAV.map((n) => (
          <Link
            key={n.href}
            to={n.href}
            className="flex items-center gap-2.5 px-2.5 h-9 rounded-lg text-[13px] transition-colors"
            style={is(n.href) ? { background: "#1a1a1a", color: "#fff" } : { color: "var(--color-muted)" }}
          >
            <Icon name={n.icon} size={16} />
            {n.label}
            {n.href === "/runs" && active > 0 && (
              <span className="ml-auto text-[11px] px-1.5 rounded-full" style={{ background: "#0072f530", color: "#3b9eff" }}>
                {active}
              </span>
            )}
          </Link>
        ))}
      </nav>

      <div className="mt-auto p-4 border-t text-[11px] text-faint" style={{ borderColor: "var(--color-line)" }}>
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full" style={{ background: active > 0 ? "#3b9eff" : "#2bd576" }} />
          {active > 0 ? `${active} active run${active > 1 ? "s" : ""}` : "idle"}
        </div>
        <div className="mt-1">Automation Studio · v{(window as { api?: { version?: string } }).api?.version ?? "0.0.0"}</div>
      </div>
    </aside>
  );
}
