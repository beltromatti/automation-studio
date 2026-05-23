export function Header({ title, sub, actions }: { title: React.ReactNode; sub?: string; actions?: React.ReactNode }) {
  return (
    <div className="h-[60px] border-b flex items-center justify-between px-7 sticky top-0 z-10 bg-bg/90 backdrop-blur" style={{ borderColor: "var(--color-line)" }}>
      <div>
        <div className="text-[15px] font-semibold leading-tight">{title}</div>
        {sub && <div className="text-[12px] text-faint">{sub}</div>}
      </div>
      <div className="flex items-center gap-2">{actions}</div>
    </div>
  );
}
