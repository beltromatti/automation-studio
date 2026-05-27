import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import { jget } from "@/lib/client";
import type { ChromeInfo, InstallInfo, OSPlatform } from "@/lib/types";

function osPlatform(): OSPlatform {
  const p = (window as { api?: { platform?: string } }).api?.platform;
  return p === "win32" ? "win" : p === "linux" ? "linux" : "mac";
}

const OS_LABEL: Record<OSPlatform, string> = { mac: "macOS", win: "Windows", linux: "Linux" };

function CommandBlock({ lines }: { lines: string[] }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(lines.filter((l) => !l.trim().startsWith("#")).join("\n"))
      .then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }).catch(() => {});
  };
  return (
    <div className="relative card" style={{ background: "#0a0a0a" }}>
      <button className="absolute right-2 top-2 text-faint hover:text-fg text-[11px] flex items-center gap-1" onClick={copy}>
        <Icon name="copy" size={12} /> {copied ? "copied" : "copy"}
      </button>
      <pre className="mono text-[12px] p-3 pr-16 overflow-auto whitespace-pre-wrap" style={{ lineHeight: 1.6 }}>
        {lines.map((l, i) => (
          <div key={i} style={{ color: l.trim().startsWith("#") ? "var(--color-faint)" : "var(--color-fg)" }}>{l}</div>
        ))}
      </pre>
    </div>
  );
}

// Standardized "external dependency" modal — used when an agent engine isn't
// installed, or when system Chrome is missing before a browser action.
export function DependencyModal({ kind, install, onClose, onProceed }: {
  kind: "engine" | "chrome";
  install: InstallInfo;
  onClose: () => void;
  onProceed?: (rememberBundled: boolean) => void; // chrome only
}) {
  const os = osPlatform();
  const steps = install[os] ?? install.mac;
  const [remember, setRemember] = useState(false);
  const isChrome = kind === "chrome";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6" style={{ background: "#000000aa" }} onClick={onClose}>
      <div className="card p-5 overflow-auto" style={{ width: 560, maxWidth: "92vw", maxHeight: "90vh" }} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-1">
          <div className="text-[14px] font-semibold flex items-center gap-2">
            <span style={{ color: isChrome ? "#f5a623" : "#3b9eff" }}><Icon name={isChrome ? "globe" : "sparkles"} size={16} /></span>
            {isChrome ? `${install.name} not found` : `${install.name} isn't installed`}
          </div>
          <button className="text-faint hover:text-fg" onClick={onClose}><Icon name="x" size={16} /></button>
        </div>

        <p className="text-[12.5px] text-muted leading-relaxed mb-3">
          {isChrome
            ? "Automation Studio works best with Google Chrome (most human-like, best stealth). You can install it, or continue right now using the browser bundled with the app."
            : `Install ${install.name} and sign in to launch agents on this engine.`}
        </p>

        <div className="text-[11px] text-faint mb-1.5 flex items-center gap-1.5">
          <Icon name="download2" size={12} /> Install on {OS_LABEL[os]}
        </div>
        <CommandBlock lines={steps} />
        {install.note && <p className="text-[11px] text-faint mt-2">{install.note}</p>}
        <a href={install.url} target="_blank" rel="noreferrer" className="text-[12px] text-running hover:underline inline-flex items-center gap-1 mt-2">
          <Icon name="external" size={12} /> Official install guide
        </a>

        {isChrome && (
          <label className="flex items-center gap-2 mt-4 text-[12px] cursor-pointer select-none" onClick={() => setRemember((r) => !r)}>
            <span className="w-4 h-4 rounded flex items-center justify-center" style={{ border: `1px solid ${remember ? "#0072f5" : "var(--color-line-strong)"}`, background: remember ? "#0072f5" : "transparent" }}>
              {remember && <Icon name="check" size={11} />}
            </span>
            Don't ask again — always use the bundled browser
          </label>
        )}

        <div className="flex items-center gap-2 mt-4">
          {isChrome && onProceed && (
            <button className="btn btn-primary btn-sm" onClick={() => onProceed(remember)}>
              <Icon name="globe" size={13} /> Use the bundled browser & continue
            </button>
          )}
          <button className="btn btn-secondary btn-sm" onClick={onClose}>{isChrome ? "Cancel" : "Close"}</button>
        </div>
      </div>
    </div>
  );
}

const BUNDLED_OK_KEY = "as.browser.bundledOk";

// Gate any browser-using action behind a Chrome check. If system Chrome is missing
// (and the user hasn't opted into the bundled browser), show the modal first;
// otherwise run the action immediately. Used by runs, agent launches and manual opens.
export function useChromeGate() {
  const [chrome, setChrome] = useState<ChromeInfo | null>(null);
  const [show, setShow] = useState(false);
  const pending = useRef<null | (() => void)>(null);

  useEffect(() => {
    jget<{ chrome: ChromeInfo }>("/api/system/deps").then((d) => setChrome(d.chrome)).catch(() => {});
  }, []);

  const dismissed = () => { try { return localStorage.getItem(BUNDLED_OK_KEY) === "1"; } catch { return false; } };

  const guard = (action: () => void) => {
    if (!chrome || chrome.available || dismissed()) { action(); return; } // chrome present / unknown / opted-in → just go
    pending.current = action;
    setShow(true);
  };

  const modal = show && chrome ? (
    <DependencyModal
      kind="chrome"
      install={chrome.install}
      onClose={() => { setShow(false); pending.current = null; }}
      onProceed={(remember) => {
        if (remember) { try { localStorage.setItem(BUNDLED_OK_KEY, "1"); } catch { /* ignore */ } }
        setShow(false);
        const a = pending.current; pending.current = null; a?.();
      }}
    />
  ) : null;

  return { guard, modal, chrome };
}
