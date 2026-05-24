import { useCallback, useEffect, useRef, useState } from "react";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { jget, jpost, jdel, timeAgo, formatBytes } from "@/lib/client";
import type { Profile } from "@/lib/types";

export default function ProfilesPage() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState<string>(""); // id currently performing an action
  const [error, setError] = useState("");
  const anyOpen = profiles.some((p) => p.open);
  const stopped = useRef(false);

  const load = useCallback(async () => {
    try {
      const d = await jget<{ profiles: Profile[] }>("/api/profiles");
      if (!stopped.current) setProfiles(d.profiles);
    } catch {}
  }, []);

  // Poll faster while a login window is open so the "open" badge reflects the
  // user closing the window directly; idle otherwise.
  useEffect(() => {
    stopped.current = false;
    load();
    const t = setInterval(load, anyOpen ? 2000 : 6000);
    return () => {
      stopped.current = true;
      clearInterval(t);
    };
  }, [load, anyOpen]);

  const create = async () => {
    const name = newName.trim();
    if (!name) return;
    setError("");
    try {
      await jpost("/api/profiles", { name });
      setNewName("");
      setCreating(false);
      load();
    } catch (e) {
      setError(String((e as Error).message));
    }
  };

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    setError("");
    try {
      await fn();
      await load();
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy("");
    }
  };

  return (
    <>
      <Header
        title="Profiles"
        sub="Self-contained browser environments — cookies, logins, history & storage"
        actions={
          <button className="btn btn-primary btn-sm" onClick={() => setCreating((c) => !c)}>
            <Icon name="plus" size={14} /> New profile
          </button>
        }
      />
      <div className="px-7 py-6 max-w-[860px]">
        <div className="card p-4 mb-3 flex items-start gap-3 text-[12.5px] text-muted leading-relaxed">
          <Icon name="user" size={16} />
          <div>
            Each profile keeps its own logins, cookies, history and storage, fully isolated from the others.
            Pick a profile when you run a workflow. <span className="text-fg">Open</span> a profile to launch a
            browser window where you can log in or set things up — your session is saved and the profile
            <span className="text-fg"> ages genuinely over time</span> as runs use it. Runs on the same profile
            run <span className="text-fg">one at a time</span> (others queue); different profiles run in parallel.
          </div>
        </div>

        {/* The built-in Ephemeral profile — not a managed entity, always available in the run form. */}
        <div className="card p-4 mb-5 flex items-center gap-4 opacity-90">
          <span className="w-9 h-9 rounded-full flex items-center justify-center shrink-0" style={{ background: "#3a3a3a22", border: "1px dashed #3a3a3a" }}>
            <Icon name="globe" size={16} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-[13.5px] font-medium">Ephemeral</span>
              <span className="text-[10px] px-1.5 py-0.5 rounded text-faint" style={{ background: "#161616" }}>built-in</span>
            </div>
            <div className="text-[11.5px] text-faint mt-0.5">
              A fresh throwaway profile is spawned per run and deleted after — no login kept. Always selectable;
              ephemeral runs all go in parallel. Nothing to manage here.
            </div>
          </div>
        </div>

        {creating && (
          <div className="card p-4 mb-4 flex items-center gap-2">
            <input
              autoFocus
              className="input"
              placeholder="Profile name — e.g. LinkedIn (work)"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") create();
                if (e.key === "Escape") {
                  setCreating(false);
                  setNewName("");
                }
              }}
            />
            <button className="btn btn-primary" onClick={create} disabled={!newName.trim()}>
              Create
            </button>
            <button className="btn btn-secondary" onClick={() => { setCreating(false); setNewName(""); }}>
              Cancel
            </button>
          </div>
        )}

        {error && <div className="text-[12px] text-danger mb-4">{error}</div>}

        <div className="flex flex-col gap-2.5">
          {profiles.map((p) => (
            <ProfileCard
              key={p.id}
              profile={p}
              busy={busy === p.id}
              onChange={load}
              onError={setError}
              act={act}
            />
          ))}
          {profiles.length === 0 && (
            <div className="card p-10 text-center text-[13px] text-faint flex flex-col items-center gap-2">
              <Icon name="user" size={22} />
              No profiles yet.
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function ProfileCard({
  profile: p,
  busy,
  onChange,
  onError,
  act,
}: {
  profile: Profile;
  busy: boolean;
  onChange: () => void;
  onError: (s: string) => void;
  act: (id: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(p.name);
  const [confirmDel, setConfirmDel] = useState(false);
  const isDefault = p.id === "default";

  const saveName = async () => {
    const v = name.trim();
    setEditing(false);
    if (!v || v === p.name) {
      setName(p.name);
      return;
    }
    try {
      await jpost(`/api/profiles/${p.id}/rename`, { name: v });
      onChange();
    } catch (e) {
      onError(String((e as Error).message));
      setName(p.name);
    }
  };

  return (
    <div className="card p-4 flex items-center gap-4">
      <span
        className="w-9 h-9 rounded-full flex items-center justify-center shrink-0"
        style={{ background: `${p.color}22`, border: `1px solid ${p.color}55` }}
      >
        <span className="w-3 h-3 rounded-full" style={{ background: p.color }} />
      </span>

      <div className="min-w-0 flex-1">
        {editing ? (
          <input
            autoFocus
            className="input max-w-[280px]"
            style={{ height: 30 }}
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={saveName}
            onKeyDown={(e) => {
              if (e.key === "Enter") saveName();
              if (e.key === "Escape") { setEditing(false); setName(p.name); }
            }}
          />
        ) : (
          <div className="flex items-center gap-2">
            <span className="text-[13.5px] font-medium truncate">{p.name}</span>
            {isDefault && (
              <span className="text-[10px] px-1.5 py-0.5 rounded text-faint" style={{ background: "#161616" }}>
                default
              </span>
            )}
            {p.open && (
              <span className="text-[10px] px-1.5 py-0.5 rounded inline-flex items-center gap-1" style={{ background: "#2bd57618", color: "#2bd576" }}>
                <span className="w-1.5 h-1.5 rounded-full" style={{ background: "#2bd576" }} /> window open
              </span>
            )}
          </div>
        )}
        <div className="text-[11px] text-faint mono mt-0.5 truncate">
          {formatBytes(p.sizeBytes)} · {p.lastUsedAt ? `used ${timeAgo(p.lastUsedAt)}` : "never used"}
        </div>
      </div>

      <div className="flex items-center gap-1.5 shrink-0">
        {p.open ? (
          <button
            className="btn btn-secondary btn-sm"
            disabled={busy}
            onClick={() => act(p.id, () => jpost(`/api/profiles/${p.id}/close`))}
          >
            <Icon name="power" size={13} /> {busy ? "…" : "Close window"}
          </button>
        ) : (
          <button
            className="btn btn-secondary btn-sm"
            disabled={busy}
            onClick={() => act(p.id, () => jpost(`/api/profiles/${p.id}/open`))}
            title="Open a browser window to log in / set things up"
          >
            <Icon name="globe" size={13} /> {busy ? "Opening…" : "Open to log in"}
          </button>
        )}

        {!editing && (
          <button className="btn btn-secondary btn-sm" onClick={() => setEditing(true)} title="Rename">
            <Icon name="pencil" size={13} />
          </button>
        )}

        {!isDefault &&
          (confirmDel ? (
            <button
              className="btn btn-danger btn-sm"
              disabled={busy}
              onClick={() => act(p.id, () => jdel(`/api/profiles/${p.id}`))}
              onMouseLeave={() => setConfirmDel(false)}
            >
              <Icon name="trash" size={13} /> {busy ? "…" : "Confirm delete"}
            </button>
          ) : (
            <button className="btn btn-secondary btn-sm" onClick={() => setConfirmDel(true)} title="Delete profile">
              <Icon name="trash" size={13} />
            </button>
          ))}
      </div>
    </div>
  );
}
