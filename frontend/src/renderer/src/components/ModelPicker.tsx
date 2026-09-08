import { useCallback, useEffect, useMemo, useState } from "react";
import { Icon } from "@/components/Icon";
import { jget } from "@/lib/client";
import type { AgentEngine, EngineCatalog, EngineModel } from "@/lib/types";

// The catalogue is whatever the INSTALLED CLI advertises right now — the backend
// asks `codex app-server model/list` and Claude Code's own `/model`. Cached here
// per engine for the lifetime of the page so switching engines is instant.
const cache = new Map<AgentEngine, EngineCatalog>();

export function useEngineCatalog(engine: AgentEngine | null) {
  const [catalog, setCatalog] = useState<EngineCatalog | null>(
    engine ? cache.get(engine) ?? null : null,
  );
  const [loading, setLoading] = useState(false);

  const load = useCallback(
    async (refresh = false) => {
      if (!engine) return;
      if (!refresh && cache.has(engine)) {
        setCatalog(cache.get(engine)!);
        return;
      }
      setLoading(true);
      try {
        const d = await jget<{ catalog: EngineCatalog }>(
          `/api/agents/models?engine=${engine}${refresh ? "&refresh=1" : ""}`,
        );
        cache.set(engine, d.catalog);
        setCatalog(d.catalog);
      } catch {
        /* keep whatever we had; the picker falls back to showing the raw ids */
      } finally {
        setLoading(false);
      }
    },
    [engine],
  );

  useEffect(() => {
    setCatalog(engine ? cache.get(engine) ?? null : null);
    load();
  }, [engine, load]);

  return { catalog, loading, refresh: () => load(true) };
}

function Select({
  value,
  onChange,
  disabled,
  title,
  compact,
  children,
}: {
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  title?: string;
  compact?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="relative flex items-center" title={title}>
      <select
        className="input appearance-none"
        disabled={disabled}
        style={
          compact
            ? { height: 26, paddingLeft: 8, paddingRight: 22, fontSize: 11.5, minWidth: 0 }
            : { paddingRight: 30 }
        }
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {children}
      </select>
      <Icon
        name="chevronRight"
        size={compact ? 11 : 14}
        className={`absolute ${compact ? "right-1.5" : "right-3"} rotate-90 pointer-events-none text-faint`}
      />
    </div>
  );
}

/**
 * Model + reasoning-effort pickers for one engine. The effort list is per-model:
 * Codex advertises a different set per model (and its own default), so changing
 * the model re-derives the efforts and snaps to a valid one.
 */
export function ModelPicker({
  engine,
  model,
  effort,
  onChange,
  disabled,
  compact,
  syncDefaults = true,
}: {
  engine: AgentEngine;
  model: string;
  effort: string;
  onChange: (model: string, effort: string) => void;
  disabled?: boolean;
  compact?: boolean;
  /** Push the resolved defaults back to the caller as soon as the catalogue
   *  loads. On for a launch form (the values are about to be submitted); OFF for
   *  a live session, where writing back would post a "model changed" note the
   *  user never asked for — the backend resolves the very same defaults per turn,
   *  so what's displayed is what will run. */
  syncDefaults?: boolean;
}) {
  const { catalog, loading, refresh } = useEngineCatalog(engine);
  const models = catalog?.models ?? [];

  const current: EngineModel | undefined = useMemo(
    () => models.find((m) => m.id === model) ?? models.find((m) => m.id === catalog?.defaultModel) ?? models[0],
    [models, model, catalog],
  );
  const efforts = current?.efforts ?? [];

  // Keep the selection valid as the catalogue arrives (or the engine changes):
  // an empty/unknown model becomes the engine's default, and an effort the chosen
  // model doesn't support becomes that model's own default.
  useEffect(() => {
    if (!current || !syncDefaults) return;
    const wantModel = current.id;
    const wantEffort = efforts.includes(effort) ? effort : current.defaultEffort || efforts[efforts.length - 1] || "";
    if (wantModel !== model || wantEffort !== effort) onChange(wantModel, wantEffort);
  }, [current, efforts, model, effort, onChange, syncDefaults]);

  const pickModel = (id: string) => {
    const m = models.find((x) => x.id === id);
    const eff = m && m.efforts.includes(effort) ? effort : m?.defaultEffort || m?.efforts[0] || "";
    onChange(id, eff);
  };

  const label = (m: EngineModel) => (m.isDefault ? `${m.label} — default` : m.label);

  return (
    <div className={`flex items-center ${compact ? "gap-1.5" : "gap-2"}`}>
      <Select
        compact={compact}
        disabled={disabled || loading || models.length === 0}
        value={current?.id ?? ""}
        onChange={pickModel}
        title={current?.description || "Model"}
      >
        {models.length === 0 && <option value="">{loading ? "loading models…" : "no models"}</option>}
        {models.map((m) => (
          <option key={m.id} value={m.id}>
            {label(m)}
          </option>
        ))}
      </Select>
      <Select
        compact={compact}
        disabled={disabled || efforts.length === 0}
        value={efforts.includes(effort) ? effort : current?.defaultEffort ?? ""}
        onChange={(v) => onChange(current?.id ?? model, v)}
        title={current?.effortLabels?.[effort] || "Reasoning effort"}
      >
        {efforts.length === 0 && <option value="">—</option>}
        {efforts.map((e) => (
          <option key={e} value={e}>
            {compact ? e : `effort: ${e}`}
          </option>
        ))}
      </Select>
      <button
        className="text-faint hover:text-fg"
        title={
          catalog?.error
            ? `Couldn't read the list from ${engine}: ${catalog.error}`
            : `Model list read from ${engine} (${catalog?.source ?? "…"}). Click to re-read.`
        }
        onClick={() => refresh()}
        disabled={loading}
      >
        <Icon name="refresh" size={compact ? 11 : 13} className={loading ? "animate-spin" : ""} />
      </button>
      {catalog?.error && (
        <span className="text-[10.5px]" style={{ color: "#f5a623" }} title={catalog.error}>
          fallback list
        </span>
      )}
    </div>
  );
}
