import { useEffect, useState } from "react";
import { Icon } from "./Icon";
import { jget, jpost } from "@/lib/client";
import type { ColumnType, ParamType, PublicWorkflow, WorkflowParam } from "@/lib/types";

const TEMPLATE = `import asyncio
from automations import userkit

async def run(params, sess):
    # sess drives the browser:
    #   await sess.goto(url); await sess.evaluate("() => document.title")
    #   await sess.observe(); await sess.click(i); await sess.type(i, text, enter=True)
    url = params.get("url", "https://example.com")
    await sess.goto(url)
    title = await sess.evaluate("() => document.title")
    userkit.progress(1, 1, message="done")
    return [{"url": url, "title": title}]

def main(argv):
    params, server, output = userkit.parse(argv)
    rows = userkit.run_session(run, params, server)
    userkit.write_csv(output, rows, ["url", "title"])
    return 0
`;

type PEdit = Pick<WorkflowParam, "name" | "label" | "type" | "default" | "help">;

export function WorkflowEditor({ workflow, onClose, onSaved }: {
  workflow: PublicWorkflow | "new"; onClose: () => void; onSaved: (id: string) => void;
}) {
  const isNew = workflow === "new";
  const w = isNew ? null : (workflow as PublicWorkflow);
  const [name, setName] = useState(w?.name ?? "");
  const [description, setDescription] = useState(w?.description ?? "");
  const [profile, setProfile] = useState<"ephemeral" | "shared">((w?.profile as "ephemeral" | "shared") ?? "ephemeral");
  const [needsAuth, setNeedsAuth] = useState(!!w?.needsAuth);
  const [params, setParams] = useState<PEdit[]>(
    (w?.params ?? []).map((p) => ({ name: p.name, label: p.label, type: p.type, default: p.default, help: p.help })));
  const [contract, setContract] = useState<{ name: string; type: ColumnType }[]>(
    (w?.outputContract ?? []).map((c) => ({ name: c.name, type: c.type })));
  const [inputC, setInputC] = useState<{ name: string; type: ColumnType }[]>(
    (w?.inputContract ?? []).map((c) => ({ name: c.name, type: c.type })));
  const [code, setCode] = useState(isNew ? TEMPLATE : "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!isNew && w && !w.builtin) {
      jget<{ source: string }>(`/api/workflows/${w.id}/source`).then((d) => setCode(d.source)).catch(() => setCode(TEMPLATE));
    }
  }, [isNew, w]);

  const setP = (i: number, k: keyof PEdit, v: unknown) => setParams((ps) => ps.map((p, j) => (j === i ? { ...p, [k]: v } : p)));
  const setC = (i: number, k: "name" | "type", v: string) => setContract((cs) => cs.map((c, j) => (j === i ? { ...c, [k]: v } : c)));

  const save = async () => {
    if (!name.trim()) { setError("Name is required."); return; }
    setBusy(true); setError("");
    try {
      const body: Record<string, unknown> = {
        name, description, profile, needsAuth, code, createdBy: "user", icon: w?.icon ?? "wand",
        params: params.filter((p) => p.name.trim()).map((p) => ({ ...p, label: p.label || p.name })),
        outputContract: contract.filter((c) => c.name.trim()),
        inputContract: inputC.filter((c) => c.name.trim()),
      };
      if (!isNew && w) body.id = w.id;
      const r = await jpost<{ workflow: { id: string } }>("/api/workflows", body);
      onSaved(r.workflow.id);
    } catch (e) { setError(String((e as Error).message)); setBusy(false); }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6" style={{ background: "#000000aa" }} onClick={onClose}>
      <div className="card p-5 overflow-auto" style={{ width: 820, maxWidth: "94vw", maxHeight: "90vh" }} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <div className="text-[14px] font-semibold">{isNew ? "New workflow" : `Edit ${w!.name}`}</div>
          <button className="text-faint hover:text-fg" onClick={onClose}><Icon name="x" size={16} /></button>
        </div>
        <div className="grid grid-cols-2 gap-3 mb-3">
          <div><label className="label">Name</label><input className="input mt-1" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. LinkedIn Connect" /></div>
          <div>
            <label className="label">Profile</label>
            <select className="input appearance-none mt-1" value={profile} onChange={(e) => setProfile(e.target.value as "ephemeral" | "shared")}>
              <option value="ephemeral">Ephemeral — throwaway</option>
              <option value="shared">Persistent — uses a saved login</option>
            </select>
          </div>
        </div>
        <label className="label">Description</label>
        <input className="input mt-1 mb-3" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What does it do?" />

        <label className="label">Parameters <span className="text-faint">(the inputs the run form shows)</span></label>
        <div className="flex flex-col gap-1.5 mt-1 mb-2">
          {params.map((p, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <input className="input" style={{ height: 30, width: 130 }} placeholder="name" value={p.name} onChange={(e) => setP(i, "name", e.target.value)} />
              <input className="input" style={{ height: 30, flex: 1 }} placeholder="label" value={p.label ?? ""} onChange={(e) => setP(i, "label", e.target.value)} />
              <select className="input appearance-none" style={{ height: 30, width: 110 }} value={p.type} onChange={(e) => setP(i, "type", e.target.value as ParamType)}>
                <option value="string">string</option><option value="number">number</option><option value="boolean">boolean</option><option value="select">select</option>
              </select>
              <input className="input" style={{ height: 30, width: 120 }} placeholder="default" value={String(p.default ?? "")} onChange={(e) => setP(i, "default", e.target.value)} />
              <button className="btn btn-secondary btn-sm" onClick={() => setParams((ps) => ps.filter((_, j) => j !== i))}><Icon name="x" size={12} /></button>
            </div>
          ))}
          <button className="btn btn-secondary btn-sm self-start" onClick={() => setParams((ps) => [...ps, { name: "", label: "", type: "string", default: "", help: "" }])}><Icon name="plus" size={12} /> Parameter</button>
        </div>

        <label className="label">Output columns <span className="text-faint">(the result CSV / dataset contract)</span></label>
        <div className="flex flex-wrap items-center gap-1.5 mt-1 mb-3">
          {contract.map((c, i) => (
            <span key={i} className="flex items-center gap-1 card px-1.5 py-1">
              <input className="input" style={{ height: 26, width: 110 }} placeholder="name" value={c.name} onChange={(e) => setC(i, "name", e.target.value)} />
              <select className="input appearance-none" style={{ height: 26, width: 86 }} value={c.type} onChange={(e) => setC(i, "type", e.target.value)}>
                <option value="text">text</option><option value="number">number</option><option value="boolean">boolean</option>
              </select>
              <button className="text-faint hover:text-fg" onClick={() => setContract((cs) => cs.filter((_, j) => j !== i))}><Icon name="x" size={11} /></button>
            </span>
          ))}
          <button className="btn btn-secondary btn-sm" onClick={() => setContract((cs) => [...cs, { name: "", type: "text" }])}><Icon name="plus" size={12} /> Column</button>
        </div>

        <label className="label">Input columns <span className="text-faint">(optional — set to consume a dataset as a list; read rows with <span className="mono">userkit.input_rows()</span>)</span></label>
        <div className="flex flex-wrap items-center gap-1.5 mt-1 mb-3">
          {inputC.map((c, i) => (
            <span key={i} className="flex items-center gap-1 card px-1.5 py-1">
              <input className="input" style={{ height: 26, width: 110 }} placeholder="name" value={c.name} onChange={(e) => setInputC((cs) => cs.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
              <button className="text-faint hover:text-fg" onClick={() => setInputC((cs) => cs.filter((_, j) => j !== i))}><Icon name="x" size={11} /></button>
            </span>
          ))}
          <button className="btn btn-secondary btn-sm" onClick={() => setInputC((cs) => [...cs, { name: "", type: "text" }])}><Icon name="plus" size={12} /> Input column</button>
        </div>

        <label className="label flex items-center gap-2">Python code <span className="text-faint">— define <span className="mono">main(argv)</span>; use <span className="mono">automations.userkit</span></span></label>
        <textarea className="input mono mt-1 mb-3" style={{ height: 280, padding: 10, lineHeight: 1.45, fontSize: 12 }} value={code} onChange={(e) => setCode(e.target.value)} spellCheck={false} />

        <label className="flex items-center gap-2 text-[12.5px] mb-3 cursor-pointer select-none" onClick={() => setNeedsAuth((v) => !v)}>
          <span className="w-9 h-5 rounded-full relative" style={{ background: needsAuth ? "#0072f5" : "#2a2a2a" }}>
            <span className="absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all" style={{ left: needsAuth ? 18 : 2 }} />
          </span>
          Uses a login (defaults the run to a persistent profile)
        </label>

        {error && <div className="text-[12px] text-danger mb-2">{error}</div>}
        <button className="btn btn-primary btn-sm" disabled={busy} onClick={save}><Icon name="check" size={13} /> {busy ? "Saving…" : isNew ? "Create workflow" : "Save changes"}</button>
      </div>
    </div>
  );
}
