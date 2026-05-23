"""Command-line client + server launcher.

Driving the live session from a shell is what lets an agent (or a person, or me
during development) act on the page one step at a time:

    hb serve --headless &        # start the session (background)
    hb goto google.com
    hb observe                   # see the indexed page
    hb type 7 "best espresso machine" --enter
    hb observe                   # see the results
    hb pause                     # hand control to the human window
    hb resume

Every command is a thin HTTP call to the control server, so the exact same
actions are available to deterministic scripts via the Python API.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error

DEFAULT_PORT = int(os.environ.get("HB_PORT", "8787"))


def _base(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _req(port: int, method: str, path: str, body: dict | None = None) -> dict:
    url = _base(port) + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"cannot reach server on {url}: {e.reason}. Is `hb serve` running?"}


def _print(obj) -> None:
    if isinstance(obj, dict) and "text" in obj and len(obj) == 1:
        print(obj["text"])
    else:
        print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_serve(args) -> int:
    if args.headless:
        os.environ["HB_HEADLESS"] = "1"
    if args.no_humanize:
        os.environ["HB_HUMANIZE"] = "0"
    os.environ["HB_PORT"] = str(args.port)
    if args.profile:
        os.environ["HB_PROFILE"] = args.profile
    from .server import ControlServer
    from .config import BrowserConfig
    print(f"[hb] starting control server on 127.0.0.1:{args.port} "
          f"(headless={args.headless}, humanize={not args.no_humanize})", flush=True)
    ControlServer(BrowserConfig.from_env()).run()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hb", description="human-grade browser control")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, default=DEFAULT_PORT, help="control server port")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    sp = add("serve", help="run the control server (browser session)")
    sp.add_argument("--headless", action="store_true")
    sp.add_argument("--no-humanize", action="store_true")
    sp.add_argument("--profile", default=None)
    sp.set_defaults(func=cmd_serve)

    add("status"); add("pause"); add("resume"); add("text")
    add("back"); add("forward"); add("reload"); add("shutdown")

    g = add("goto"); g.add_argument("url")
    o = add("observe"); o.add_argument("--json", action="store_true"); o.add_argument("--max-nodes", type=int, default=1200)
    c = add("click"); c.add_argument("index", type=int)
    t = add("type"); t.add_argument("index", type=int); t.add_argument("text"); t.add_argument("--enter", action="store_true"); t.add_argument("--clear", action="store_true")
    f = add("fill"); f.add_argument("index", type=int); f.add_argument("text")
    s = add("scroll"); s.add_argument("dy", type=int, nargs="?", default=600)
    pr = add("press"); pr.add_argument("key")
    se = add("select"); se.add_argument("index", type=int); se.add_argument("--value"); se.add_argument("--label")
    sh = add("screenshot"); sh.add_argument("--full", action="store_true")
    m = add("mode"); m.add_argument("mode", choices=["headed", "headless"])
    ev = add("eval"); ev.add_argument("script")

    args = p.parse_args(argv)
    port = args.port

    if args.cmd == "serve":
        return cmd_serve(args)

    routes = {
        "status": ("GET", "/status", None),
        "pause": ("POST", "/pause", {}),
        "resume": ("POST", "/resume", {}),
        "text": ("GET", "/text", None),
        "back": ("POST", "/act", {"action": "back"}),
        "forward": ("POST", "/act", {"action": "forward"}),
        "reload": ("POST", "/act", {"action": "reload"}),
        "shutdown": ("POST", "/shutdown", {}),
    }
    if args.cmd in routes:
        method, path, body = routes[args.cmd]
        _print(_req(port, method, path, body)); return 0

    if args.cmd == "goto":
        _print(_req(port, "POST", "/goto", {"url": args.url})); return 0
    if args.cmd == "observe":
        q = f"?format={'json' if args.json else 'text'}&max_nodes={args.max_nodes}"
        _print(_req(port, "GET", "/observe" + q)); return 0
    if args.cmd == "click":
        _print(_req(port, "POST", "/act", {"action": "click", "index": args.index})); return 0
    if args.cmd == "type":
        _print(_req(port, "POST", "/act", {"action": "type", "index": args.index, "text": args.text, "enter": args.enter, "clear": args.clear})); return 0
    if args.cmd == "fill":
        _print(_req(port, "POST", "/act", {"action": "fill", "index": args.index, "text": args.text})); return 0
    if args.cmd == "scroll":
        _print(_req(port, "POST", "/act", {"action": "scroll", "dy": args.dy})); return 0
    if args.cmd == "press":
        _print(_req(port, "POST", "/act", {"action": "press", "key": args.key})); return 0
    if args.cmd == "select":
        _print(_req(port, "POST", "/act", {"action": "select", "index": args.index, "value": args.value, "label": args.label})); return 0
    if args.cmd == "screenshot":
        _print(_req(port, "GET", "/screenshot" + ("?full=1" if args.full else ""))); return 0
    if args.cmd == "mode":
        _print(_req(port, "POST", "/switch_mode", {"headless": args.mode == "headless"})); return 0
    if args.cmd == "eval":
        _print(_req(port, "POST", "/eval", {"script": args.script})); return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
