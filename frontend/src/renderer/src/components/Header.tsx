export function Header({ title, sub, actions }: { title: React.ReactNode; sub?: string; actions?: React.ReactNode }) {
  return (
    <div className="app-drag main-titlebar h-[60px] border-b flex items-center justify-between px-7 sticky top-0 z-10 bg-bg/90 backdrop-blur" style={{ borderColor: "var(--color-line)" }}>
      <div className="min-w-0">
        <div className="text-[15px] font-semibold leading-tight truncate">{title}</div>
        {sub && <div className="text-[12px] text-faint truncate">{sub}</div>}
      </div>
      <div className="flex items-center gap-2 shrink-0">{actions}</div>
    </div>
  );
}
