
import { Link, useLocation } from "react-router-dom";
import { Icon } from "./Icon";
import { useRuns } from "./RunsProvider";
import iconUrl from "@/assets/icon.png";

// Conceptually top-down: an agent runs sessions; a workflow spawns runs. So each
// "thing" sits above its executions: Agents → Sessions → Workflows → Runs → Data → Files → Profiles.
const NAV = [
  { href: "/agents", label: "Agents", icon: "sparkles" },
  { href: "/agents/sessions", label: "Sessions", icon: "bot" },
  { href: "/workflows", label: "Workflows", icon: "layers" },
  { href: "/runs", label: "Runs", icon: "terminal" },
  { href: "/data", label: "Data", icon: "database" },
  { href: "/files", label: "Files", icon: "image" },
  { href: "/profiles", label: "Profiles", icon: "user" },
];

function matchesNav(href: string, pathname: string): boolean {
  return pathname === href || pathname.startsWith(href + "/");
}

export function Sidebar() {
  const pathname = useLocation().pathname;
  const { runs } = useRuns();
  const active = runs.filter((r) => ["running", "starting", "controlled"].includes(r.status)).length;

  // The active item is the one whose href is the *longest* matching prefix, so
  // /agents/sessions highlights Sessions (not also Agents).
  const activeHref = NAV.map((n) => n.href).filter((h) => matchesNav(h, pathname)).sort((a, b) => b.length - a.length)[0] ?? "";
  const is = (href: string) => href === activeHref;
  return (
    <aside className="w-[248px] shrink-0 flex flex-col bg-bg">
      <div className="app-drag sidebar-titlebar h-[48px] shrink-0" />

      <div className="pl-[22px] pt-0 pb-3 flex items-center gap-2">
        <img src={iconUrl} alt="" className="brand-logo shrink-0 select-none" style={{ height: 18, width: 18, objectFit: "contain" }} draggable={false} />
        <div className="text-[13px] font-semibold text-fg">Automation Studio</div>
      </div>

      <nav className="p-3 pt-1 flex flex-col gap-0.5">
        {NAV.map((n) => (
          <Link
            key={n.href}
            to={n.href}
            className={`nav-item flex items-center gap-2.5 px-2.5 h-9 rounded-lg text-[13px] ${is(n.href) ? "nav-active" : ""}`}
            style={is(n.href) ? undefined : { color: "var(--color-muted)" }}
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

      <div className="mt-auto p-4 text-[11px] text-faint">
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full" style={{ background: active > 0 ? "#3b9eff" : "#2bd576" }} />
          {active > 0 ? `${active} active run${active > 1 ? "s" : ""}` : "idle"}
        </div>
        <div className="mt-1">Automation Studio · v{(window as { api?: { version?: string } }).api?.version ?? "0.0.0"}</div>
      </div>
    </aside>
  );
}
