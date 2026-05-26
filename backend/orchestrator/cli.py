"""Backend entrypoint + self-dispatcher.

One executable, several modes — so the frozen production binary can re-invoke
itself to launch control-servers and workflows (no separate `python`/`hb` needed):

    automation-backend api [--port N]          # the orchestrator HTTP API (the sidecar)
    automation-backend control-server ...      # a humanbrowser control server (one Chrome)
    automation-backend run-workflow MOD ...    # run a workflow module attached to a server
    automation-backend reap                    # kill any orphaned browser trees of ours
    automation-backend hb ...                  # the humanbrowser CLI (for agents)
    automation-backend mcp                     # the stdio MCP tool server (spawned per agent)
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import sys


def _run_api(argv: list[str]) -> int:
    import uvicorn
    from .api import create_app
    p = argparse.ArgumentParser(prog="automation-backend api")
    p.add_argument("--port", type=int, default=int(os.environ.get("AUTOMATION_PORT", "8765")))
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args(argv)
    # record our actual port so in-process components (e.g. the agent manager
    # building the MCP server's backend URL) can address this API.
    os.environ["AUTOMATION_PORT"] = str(args.port)
    # tell the launcher we're about to bind (Electron also health-checks /api/health)
    print(f"[backend] api starting on http://{args.host}:{args.port}", flush=True)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


def _run_control_server(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="automation-backend control-server")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--headless", action="store_true")
    args = p.parse_args(argv)
    os.environ["HB_PORT"] = str(args.port)
    os.environ["HB_PROFILE"] = args.profile
    os.environ["HB_HEADLESS"] = "1" if args.headless else "0"
    from humanbrowser.server import main as server_main
    server_main()
    return 0


def _run_workflow(argv: list[str]) -> int:
    if not argv:
        print("run-workflow: missing module", file=sys.stderr)
        return 2
    target, rest = argv[0], argv[1:]
    # User/agent workflows are loaded from a .py file under the data dir; built-ins
    # are dotted modules in the bundle.
    if target.endswith(".py") or os.path.sep in target or os.path.exists(target):
        spec = importlib.util.spec_from_file_location("_user_workflow", target)
        if not spec or not spec.loader:
            print(f"run-workflow: cannot load {target}", file=sys.stderr)
            return 2
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    else:
        mod = importlib.import_module(target)
    return int(mod.main(rest) or 0)


def _reap() -> int:
    # constructing the manager runs a startup reap of orphaned trees
    from .manager import RunManager
    RunManager()
    print("[backend] reaped orphaned browser trees")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode = args[0] if args else "api"
    rest = args[1:]
    if mode == "api":
        return _run_api(rest)
    if mode == "control-server":
        return _run_control_server(rest)
    if mode == "run-workflow":
        return _run_workflow(rest)
    if mode == "reap":
        return _reap()
    if mode == "hb":
        from humanbrowser.cli import main as hb_main
        return hb_main(rest)
    if mode == "mcp":
        from .mcp_server import main as mcp_main
        return mcp_main(rest)
    # default: treat unknown leading arg as api flags
    return _run_api(args)


if __name__ == "__main__":
    sys.exit(main())
