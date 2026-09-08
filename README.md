<div align="center">

# ⚡ Automation Studio

### The desktop workbench for **human-grade automations** — on *your* machine.

Build browser & data workflows, shape the results in a built-in data layer, and let **AI agents** run the whole thing for you. Local-first, account-safe, no cloud middleman.

<br/>

[![Latest release](https://img.shields.io/github/v/release/beltromatti/automation-studio?label=download&color=3b9eff&logo=github)](https://github.com/beltromatti/automation-studio/releases/latest)
[![Build](https://img.shields.io/github/actions/workflow/status/beltromatti/automation-studio/release.yml?label=build&color=00d4ff)](https://github.com/beltromatti/automation-studio/actions/workflows/release.yml)
[![Downloads](https://img.shields.io/github/downloads/beltromatti/automation-studio/total?color=3b9eff)](https://github.com/beltromatti/automation-studio/releases)
![Platforms](https://img.shields.io/badge/platform-macOS%20%C2%B7%20Windows%20%C2%B7%20Linux-555)

![Electron](https://img.shields.io/badge/Electron-47848F?logo=electron&logoColor=white)
![React](https://img.shields.io/badge/React-20232A?logo=react&logoColor=61DAFB)
![Python](https://img.shields.io/badge/Python%203.13-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)

**[⬇️ Download](https://github.com/beltromatti/automation-studio/releases/latest)** · **[✨ Features](#-what-it-does)** · **[🤖 Agents](#-ai-agents--your-automation-team)** · **[🛠️ Build from source](#️-build-from-source)**

</div>

---

## Why Automation Studio

Most automation tools run your bots on *someone else's* servers — a foreign IP, a foreign session, a fast track to getting your accounts flagged. **Automation Studio runs everything on your own computer**, from your own browser session and IP. That makes it the safe way to automate account-bound sites (LinkedIn, dashboards, internal tools) — the work looks like *you*, because it runs *as you*.

One painless install gives you three things that compose into something much bigger than the sum of its parts:

> 🌐 **a human-grade browser** &nbsp;·&nbsp; 🗄️ **a built-in data layer** &nbsp;·&nbsp; 🤖 **AI agents that operate both**

---

## ✨ What it does

| | |
|---|---|
| 🌐 **Human-grade browser engine** | Drives a *real* Chrome — human pacing, stealthed fingerprint, headed≡headless. Uses your installed Google Chrome for maximum realism, or a **bundled Chromium** so it works on a fresh machine with nothing installed. |
| ⚙️ **Workflows** | Ready-to-run automations with typed inputs and a clean output table. Use the built-ins or write your own in a few lines of Python — the app picks them up automatically. |
| 🔗 **Chainable pipelines** | Any workflow's output becomes the next one's input. Search → enrich → act, all stitched together through datasets. |
| 🗄️ **Built-in data layer** | Every result lands in a real **SQLite** workbench you can query, filter, dedupe, merge, reshape and export — full SQL with regex helpers, right inside the app. |
| 🤖 **AI agents** | Point an agent at a goal and it operates the studio for you: runs workflows, shapes the data, drives the browser like a careful human, schedules work, and reports back. |
| 🗓️ **Detached runs & scheduling** | Launch and walk away. Runs continue in the background, can be scheduled or repeated, and the app notifies you when they finish. |
| 🧹 **Zero-orphan safety** | Every run is a fully-owned browser tree that's reaped on completion or quit — browsers can never pile up and choke your machine. |
| 💻 **Truly cross-platform** | One codebase, native installers for **macOS (Apple Silicon & Intel), Windows, and Linux**. |

---

## 📦 Built-in workflows

Out of the box, with the **LinkedIn growth suite** as the flagship — three workflows that chain into one pipeline: **search → connect → message**.

| Workflow | What it does |
|---|---|
| 🔎 **Google Search** | Autonomous, human-grade Google search — handles the consent wall, types like a person, paginates, returns ranked results. |
| 👥 **LinkedIn People** | Turns a people search into an enriched lead dataset with the *exact* targeting of LinkedIn's own filters (title, company, school, location, industry, connection degree, language). |
| 🤝 **LinkedIn Connections** | Sends connection requests to a list of profiles — human-paced, resilient to LinkedIn's interstitials, skips already-connected / pending, never spams. |
| ✉️ **LinkedIn Messages** | Messages your 1st-degree connections, with **per-recipient personalization** — one message, several to alternate, or a custom line for every lead. |
| 🌍 **Page Titles** | Feed it a list of URLs, get each page's title back — the simplest example of a list-consuming, chainable workflow. |

> Build your own just as easily: drop a Python module in `automations/`, register it, and it shows up in the app — list-consuming and chainable by default.

---

## 🤖 AI agents — your automation team

Agents are the headline act. They run on **your own [Claude Code](https://claude.com/claude-code) or [Codex](https://openai.com/codex/) CLI** — your existing subscription, **no API keys, nothing extra to pay**. They act through the same tools you do (workflows, the data layer, and the live browser), so they can genuinely *operate* the studio, not just chat about it.

Four specialists ship built-in:

- 🛠️ **Studio Agent** — the complete operator. Builds & runs workflows, shapes the data layer, drives the browser, schedules work. Point it at anything.
- 💼 **LinkedIn Specialist** — deep LinkedIn mastery: the search→connect→message pipeline plus live navigation, fluent in all of LinkedIn's quirks.
- 📈 **Client Acquisition Lead** — a B2B business-development partner. It sharpens your ideal customer, finds prospects across LinkedIn, Google/Maps, websites, directories and niche sources, enriches them with verified contact paths, runs approved multi-channel outreach and follow-up, and advises on sales strategy.
- 📣 **Social Growth Lead** — a social media growth operator. It plans organic or paid distribution across LinkedIn, Reddit, X, Instagram, TikTok, Facebook and niche communities, publishes approved channel-native content, monitors real traffic and engagement metrics, and keeps iterating over hours or days.

Roll your own in seconds: give an agent a name, a personality/skill prompt, and a toolset — it's ready to launch.

**Pick the model, mid-conversation.** Every session has a model and a reasoning-effort dropdown, and the choices are read live from the CLI you have installed — `codex app-server model/list` and Claude Code's own `/model`, so a new model shows up the day your CLI learns about it, with nothing to update here. Switch either one mid-chat and the conversation carries on in the same native thread, exactly like typing `/model` in the CLI itself.

---

## ⬇️ Download

Grab the installer for your OS from the **[latest release](https://github.com/beltromatti/automation-studio/releases/latest)**:

| OS | File |
|---|---|
| 🍎 macOS (Apple Silicon) | `Automation Studio-<version>-arm64.dmg` |
| 🍎 macOS (Intel) | `Automation Studio-<version>-x64.dmg` |
| 🪟 Windows | `Automation Studio-<version>.exe` |
| 🐧 Linux | `Automation Studio-<version>.AppImage` |

<sub>macOS builds are Developer ID signed and notarized by Apple — they open normally. Windows builds are unsigned, so on first launch click **More info → Run anyway**.</sub>

---

## 🧠 How it works

```
┌──────────────────────────── Automation Studio.app ─────────────────────────────┐
│                                                                                 │
│   Electron + React UI  ──HTTP──▶  Python backend (frozen, fully autonomous)     │
│                                     ├── orchestrator/  FastAPI · runs · agents   │
│                                     ├── humanbrowser/  the stealth Chrome engine │
│                                     ├── automations/   the workflows             │
│                                     └── studio.sqlite  the data layer            │
│                                                                                 │
│   AI agents ◀──MCP tools──▶ workflows · data · live browser                     │
└─────────────────────────────────────────────────────────────────────────────────┘
```

- **Local-first by design.** The UI is a thin shell over a self-contained Python engine. Everything — browser profiles, datasets, runs — lives in your OS user-data folder, never in the cloud and never in the app bundle.
- **Bring your own model.** Agents spawn your local Claude/Codex CLI and inherit your login. The only thing that leaves your machine is the model traffic you already pay for.
- **Autonomous backend.** The engine runs with or without the UI — script it, or let an agent drive it headless.

---

## 🛠️ Build from source

```bash
# backend engine (its own virtualenv)
cd backend && python3.13 -m venv .venv && .venv/bin/pip install -e .
cd .. && npm run frontend:install        # app shell deps

# run in dev (opens Electron, spawns the Python backend, hot-reloads the UI)
npm run dev

# package a native desktop app for this OS (run from the repo root)
( cd backend && .venv/bin/pyinstaller --noconfirm automation-backend.spec )   # freeze the backend
PLAYWRIGHT_BROWSERS_PATH=frontend/resources/chromium backend/.venv/bin/patchright install chromium  # bundle Chromium
npm run build:mac      # or build:win / build:linux, run on that OS
```

Tagging a `vX.Y.Z` release on `main` triggers CI to build & publish installers for **all four targets** automatically.

---

## 🧩 Extend it

Add a workflow by dropping a module in `backend/automations/` — drive the browser via `humanbrowser.session`, emit progress through `automations/_events.py`, write a CSV — and register it in `orchestrator/registry.py`. Make it list-consuming with an input contract and it instantly chains with everything else. The UI and the agents discover it automatically.

---

## 🔐 Privacy & safety

- Runs on **your** machine, **your** IP, **your** browser session — nothing is proxied through a third party.
- Browser process trees are owned and reaped as a whole (via `psutil`), so automations can never orphan or accumulate.
- You're automating real accounts: Automation Studio paces like a human and respects platform limits — **use it responsibly**.

---

<div align="center">

**Automation Studio** — built with Electron, React, Python, FastAPI & patchright.

[⬇️ Download](https://github.com/beltromatti/automation-studio/releases/latest) · [🐙 Source](https://github.com/beltromatti/automation-studio) · [🚀 Releases](https://github.com/beltromatti/automation-studio/releases)

</div>
