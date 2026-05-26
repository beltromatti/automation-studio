# Automation Studio

A cross-platform **desktop app** for running human-grade browser **automations**
locally — so they run from your own machine and IP (safest for account-bound
sites like LinkedIn), watchable and controllable, with one painless install.

One bundle, two parts that are each runnable on their own:

```
┌───────────────────────────── Automation Studio.app ─────────────────────────────┐
│  frontend/  Electron + Vite + React + Tailwind   →  dumb console UI (HTTP only)   │
│  backend/   Python (frozen):                                                      │
│      orchestrator/   FastAPI API + RunManager (spawn, events, concurrency, reap)  │
│      humanbrowser/   the stealth browser engine (patchright + real Chrome)        │
│      automations/    the workflows (google_search, linkedin_people, …)            │
│  resources/chromium  bundled Chromium (used when no system Chrome is present)     │
└──────────────────────────────────────────────────────────────────────────────────┘
```

The Electron app launches the backend as a child process, health-checks it, and
kills it on quit (which reaps every browser — no orphans). The **backend is fully
autonomous**: agents/scripts can drive it with no UI at all (see below).

## Repository layout

```
backend/        Python — the autonomous local engine + orchestrator + workflows
  humanbrowser/   browser engine (config, browser, humanize, context, session, server, cli)
  automations/    workflows (google_search.py, linkedin_people.py, _events.py)
  orchestrator/   api.py (FastAPI), manager.py (RunManager), registry.py, cli.py (dispatcher)
  automation-backend.spec   PyInstaller (onedir) build spec
frontend/       Electron + Vite + React (the console UI)
  src/main       Electron main (spawns/health-checks/cleans the backend sidecar)
  src/preload    injects the backend URL into the renderer
  src/renderer   the React app (pages, components, design system)
  electron-builder.yml / scripts/build-mac.mjs
dev-data/       runtime data in DEV (profiles, artifacts, runs)  ·  gitignored
```

Runtime data (browser profiles, run logs, CSVs) is **never** in the repo or app
bundle. In dev it lives in `dev-data/`; in the packaged app it lives in the OS
per-user data dir (`~/Library/Application Support/automation-studio`, `%APPDATA%`,
`~/.local/share`). One location the engine, orchestrator and app all agree on.

## Develop

The repo is one app: the root `package.json` is the entry point; the **backend
engine** lives in `backend/` with its own `.venv` + `requirements.txt`, and the
**Electron app shell** lives in `frontend/` with its own `node_modules`.

```bash
# one-time — the backend engine (its own virtualenv, like frontend/node_modules)
cd backend && python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e .
cd .. && npm run frontend:install          # the app shell deps

# run the app in dev FROM THE REPO ROOT (opens Electron, spawns the Python backend, hot-reloads UI)
npm run dev
```

## Build the desktop app (this machine = macOS arm64)

```bash
cd backend && .venv/bin/pyinstaller --noconfirm automation-backend.spec   # freeze backend
# (one-time) bundle Chromium for the target:
PLAYWRIGHT_BROWSERS_PATH=frontend/resources/chromium backend/.venv/bin/patchright install chromium
npm run build:mac     # from the repo root -> frontend/release/Automation Studio-*.dmg
```

Windows/Linux: the same flow run **on that OS** (`build:win` / `build:linux`) —
the build freezes a native backend and bundles that platform's Chromium. The code
is cross-platform (psutil process management, platformdirs, pathlib, Chromium
fallback). Real distribution needs OS code-signing/notarization.

## Drive the backend without the UI (agents & scripts)

The backend is just a process. An AI agent or a script can run it directly:

```bash
cd backend
../.venv/bin/python -m orchestrator api          # the HTTP API (what the UI uses)
../.venv/bin/python -m automations.google_search "best espresso machine" -n 25
../.venv/bin/hb serve                             # a live, controllable browser session
```

To add a workflow: drop a module in `backend/automations/` (driving the browser
via `humanbrowser.session`, emitting run-events via `automations/_events.py`,
writing a CSV) and register it in `backend/orchestrator/registry.py`. The UI then
shows it automatically.

## Browser & stealth

Prefers the **real system Google Chrome** (best stealth, validated against
Google/LinkedIn); falls back to the **bundled Chromium** so the app works on a
machine with nothing installed. The UA/fingerprint is normalised either way, and
headed ≡ headless. See `backend/humanbrowser/` for the engine details.

## Safety

Each run is a full Chrome owned by the backend. Process trees are killed as a
whole (psutil) and orphans reaped, so browsers can never accumulate and overwhelm
the machine. The Electron app terminates the backend on quit.
