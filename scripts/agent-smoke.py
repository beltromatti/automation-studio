"""End-to-end smoke test for the agent layer, run against a LIVE backend.

Drives real Codex and Claude Code turns, so it costs real tokens and takes a few
minutes — it is deliberately NOT part of CI. Run it by hand after touching
anything in orchestrator/agents.py or orchestrator/engines.py:

    cd backend && AUTOMATION_DATA_DIR=../dev-data python -m orchestrator api --port 8765 &
    python scripts/agent-smoke.py            # or: python scripts/agent-smoke.py http://127.0.0.1:PORT

It checks that the model catalogues come from the installed CLIs (not the built-in
seed), that both engines complete a turn with the Studio MCP tools attached, that
tool results are paired back to their calls, that a model change mid-conversation
keeps the native thread, and that a notification preempts a turn, wakes the agent,
and preserves whatever it had already streamed. Cleans up the agent it creates.
"""
import json, time, urllib.request, sys

B = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765").rstrip("/")
def post(p, b=None):
    r = urllib.request.Request(B+p, data=json.dumps(b or {}).encode(), headers={"content-type":"application/json"})
    return json.load(urllib.request.urlopen(r))
def get(p): return json.load(urllib.request.urlopen(B+p))
def delete(p):
    r = urllib.request.Request(B+p, method="DELETE"); return json.load(urllib.request.urlopen(r))

def wait(sid, limit=400):
    end = time.time() + limit
    while time.time() < end:
        st = get(f"/api/agents/sessions/{sid}")["session"]["status"]
        if st not in ("running", "starting", "queued"):
            return st
        time.sleep(3)
    return "TIMEOUT"

results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  — {detail}" if detail else ""), flush=True)

agent = post("/api/agents", {"name": "QA Gate", "icon": "sparkles",
                             "systemPrompt": "You are a QA probe. Be terse.", "scopes": ["studio"]})["agent"]["id"]
try:
    # 1. catalogues come from the CLIs, not a seed
    cats = get("/api/agents/models")["catalogs"]
    for e, c in cats.items():
        check(f"catalog[{e}] is live", c["source"] != "seed", f"source={c['source']} models={len(c['models'])}")

    # 2. both engines run a turn with studio tools
    sids = {}
    for engine, model in (("codex", "gpt-5.6-sol"), ("claude", "sonnet")):
        sid = post("/api/agents/sessions", {"agentId": agent, "profileId": "ephemeral",
                                            "prompt": "Call studio_list_workflows and reply with just the count.",
                                            "engine": engine, "model": model, "effort": "low"})["session"]["id"]
        sids[engine] = sid
    for engine, sid in sids.items():
        st = wait(sid)
        d = get(f"/api/agents/sessions/{sid}")
        tools = [e.get("tool") for e in d["events"] if e["kind"] == "tool_call"]
        msgs = [e for e in d["events"] if e["kind"] == "message"]
        check(f"{engine}: turn completes", st == "done", f"status={st} err={d['session']['error']}")
        check(f"{engine}: studio MCP tools reachable", "studio_list_workflows" in tools, f"tools={tools}")
        check(f"{engine}: produced a message", bool(msgs))
        check(f"{engine}: tool results carry their tool name",
              all(e.get("tool") for e in d["events"] if e["kind"] == "tool_result"))
        check(f"{engine}: no duplicate final messages",
              len({m.get("text") for m in msgs}) == len(msgs), f"{len(msgs)} messages")
        check(f"{engine}: native thread recorded", bool(d["session"]["threadId"]))

    # 3. model + effort change mid-conversation, history kept
    sid = sids["claude"]
    try:
        post(f"/api/agents/sessions/{sid}/model", {"model": "not-a-model"})
        check("rejects an unknown model", False, "accepted it")
    except urllib.error.HTTPError as e:
        check("rejects an unknown model", e.code == 400)
    r = post(f"/api/agents/sessions/{sid}/model", {"model": "haiku", "effort": "high"})
    check("accepts a valid model change", r.get("ok") and r.get("model") == "haiku", json.dumps(r))
    post(f"/api/agents/sessions/{sid}/steer", {"message": "How many workflows did you just report? Number only."})
    st = wait(sid)
    d = get(f"/api/agents/sessions/{sid}")
    last = [e["text"] for e in d["events"] if e["kind"] == "message"][-1]
    check("history survives the model change", st == "done" and any(ch.isdigit() for ch in last), f"last={last[:60]!r}")
    check("session records the new model", d["session"]["model"] == "haiku" and d["session"]["effort"] == "high")

    # 4. notification preempt + wake
    sid = post("/api/agents/sessions", {"agentId": agent, "profileId": "ephemeral",
                                        "prompt": "Write a 900-word essay about rivers. Start immediately.",
                                        "engine": "claude", "model": "haiku", "effort": "low"})["session"]["id"]
    time.sleep(9)
    post(f"/api/agents/sessions/{sid}/fake-notify", {"runId": "qa-1", "status": "succeeded",
                                                     "workflow": "QA Workflow", "rows": 3})
    st = wait(sid)
    d = get(f"/api/agents/sessions/{sid}")
    texts = [e.get("text") or "" for e in d["events"]]
    check("notification preempts the turn", any("preempting" in t for t in texts), f"status={st}")
    check("agent is woken and finishes", st == "done" and d["session"]["turns"] >= 2, f"turns={d['session']['turns']}")
    check("interrupted text is kept", any(e.get("truncated") for e in d["events"]))
finally:
    try: delete(f"/api/agents/{agent}")
    except Exception: pass

bad = [n for n, ok, _ in results if not ok]
print("\n=== %d checks, %d failed ===" % (len(results), len(bad)))
if bad: print("FAILED:", bad)
sys.exit(1 if bad else 0)
