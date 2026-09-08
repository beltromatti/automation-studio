import { app, BrowserWindow, shell, Menu, Tray, nativeImage } from "electron";
import { spawn, ChildProcess, spawnSync } from "node:child_process";
import { join, resolve } from "node:path";
import { existsSync } from "node:fs";
import net from "node:net";
import http from "node:http";

// ELECTRON_RUN_AS_NODE makes the Electron binary boot as plain Node — `app` is
// then undefined and the app dies before it can say why. Editors and CI shells
// (VS Code's integrated terminal among them) export it, and Electron reads it
// before any of our code runs, so unsetting it here is too late: re-launch
// ourselves once with a clean environment and let this process go. Guarded by a
// marker so a failure can never turn into a spawn loop.
if (process.env.ELECTRON_RUN_AS_NODE && !process.env.AUTOMATION_STUDIO_RELAUNCHED) {
  const env: NodeJS.ProcessEnv = { ...process.env, AUTOMATION_STUDIO_RELAUNCHED: "1" };
  delete env.ELECTRON_RUN_AS_NODE;
  const r = spawnSync(process.execPath, process.argv.slice(1), { stdio: "inherit", env });
  process.exit(r.status ?? 0);
}

const isMac = process.platform === "darwin";

let backend: ChildProcess | null = null;
let backendUrl = "";
let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let isQuitting = false; // true only when we really mean to exit (Quit / Cmd+Q)

function freePort(): Promise<number> {
  return new Promise((res, rej) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", rej);
    srv.listen(0, "127.0.0.1", () => {
      const port = (srv.address() as net.AddressInfo).port;
      srv.close(() => res(port));
    });
  });
}

// Poll the backend until it answers, or give up after `timeoutMs`.
//
// This MUST always settle. An earlier version destroyed a slow request with a
// bare `req.destroy()`, which emits `close` but no `error` on current Node — so
// the retry was never scheduled, the promise hung forever, and the app sat there
// with a started backend and NO WINDOW AT ALL. Every path is now funnelled
// through one settle-once pair, plus a hard overall timer as a backstop.
function waitForHealth(url: string, timeoutMs = 30000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve) => {
    let settled = false;
    const finish = (ok: boolean) => {
      if (settled) return;
      settled = true;
      clearTimeout(hardStop);
      resolve(ok);
    };
    const hardStop = setTimeout(() => finish(false), timeoutMs + 2000);

    const tick = () => {
      if (settled) return;
      let attemptOver = false;
      const again = () => {
        if (attemptOver || settled) return;
        attemptOver = true;
        if (Date.now() > deadline) finish(false);
        else setTimeout(tick, 400);
      };
      let req: http.ClientRequest;
      try {
        req = http.get(url + "/api/health", (r) => {
          r.resume();
          if (r.statusCode === 200) {
            attemptOver = true;
            finish(true);
          } else {
            again();
          }
        });
      } catch {
        again();
        return;
      }
      req.on("error", again);
      req.on("close", again);   // covers a destroyed request that emits no error
      req.setTimeout(1500, () => req.destroy());
    };
    tick();
  });
}

function startBackend(port: number) {
  const isDev = !app.isPackaged;
  const env: NodeJS.ProcessEnv = { ...process.env, AUTOMATION_PORT: String(port), AUTOMATION_VERSION: app.getVersion() };
  let cmd: string;
  let args: string[];
  let cwd: string;

  if (isDev) {
    // dev: run from the backend's own venv (backend/.venv) against the source
    // backend + dev-data. The venv lives inside backend/ (like frontend/node_modules).
    const repoRoot = resolve(__dirname, "../../..");
    env.AUTOMATION_DATA_DIR = join(repoRoot, "dev-data");
    const venvBin = process.platform === "win32" ? join("Scripts", "python.exe") : join("bin", "python");
    cmd = join(repoRoot, "backend", ".venv", venvBin);
    args = ["-m", "orchestrator", "api", "--port", String(port)];
    cwd = join(repoRoot, "backend");
  } else {
    // prod: the frozen backend binary shipped in resources, data in userData,
    // bundled Chromium as the fallback when no system Chrome is present.
    env.AUTOMATION_DATA_DIR = app.getPath("userData");
    env.PLAYWRIGHT_BROWSERS_PATH = join(process.resourcesPath, "chromium");
    const bin = process.platform === "win32" ? "automation-backend.exe" : "automation-backend";
    cmd = join(process.resourcesPath, "backend", bin);
    args = ["api", "--port", String(port)];
    cwd = join(process.resourcesPath, "backend");
  }

  console.log("[main] starting backend:", cmd, args.join(" "));
  backend = spawn(cmd, args, { env, cwd, stdio: ["ignore", "pipe", "pipe"] });
  backend.stdout?.on("data", (d) => process.stdout.write(`[backend] ${d}`));
  backend.stderr?.on("data", (d) => process.stderr.write(`[backend] ${d}`));
  backend.on("exit", (code) => console.log("[main] backend exited", code));
}

function stopBackend() {
  if (backend && !backend.killed) {
    try { backend.kill("SIGTERM"); } catch {}
    // hard fallback so nothing lingers
    setTimeout(() => { try { backend?.kill("SIGKILL"); } catch {} }, 2500);
  }
}

// Path to a bundled resource, in dev (source tree) or prod (process.resourcesPath).
function resourcePath(...parts: string[]): string {
  return app.isPackaged
    ? join(process.resourcesPath, ...parts)
    : join(__dirname, "../../resources", ...parts);
}

function appIconPath(): string {
  // electron-builder bakes the icon into the bundle on mac; on win/linux we set
  // the window/tray icon explicitly from the build asset.
  return app.isPackaged ? join(process.resourcesPath, "icon.png") : join(__dirname, "../../build/icon.png");
}

// Bring the single window to the foreground (create it if it was never made).
function showMainWindow() {
  if (!mainWindow) {
    createWindow();
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  if (!mainWindow.isVisible()) mainWindow.show();
  mainWindow.focus();
}

function createTray() {
  if (tray) return;
  // Background-presence indicator + the only guaranteed Quit affordance once the
  // window hides on close. Uses the real app icon (the "A STUDIO" mark), sized
  // for the menu bar/tray — same brand on every platform. Skip if asset missing.
  const iconPath = resourcePath("tray", "tray.png");
  if (!existsSync(iconPath)) return;
  const img = nativeImage.createFromPath(iconPath);
  tray = new Tray(img);
  tray.setToolTip("Automation Studio");
  const menu = Menu.buildFromTemplate([
    { label: "Show Automation Studio", click: () => showMainWindow() },
    { type: "separator" },
    { label: "Quit", click: () => { isQuitting = true; app.quit(); } },
  ]);
  tray.setContextMenu(menu);
  tray.on("click", () => showMainWindow()); // single-click toggles to foreground (win/linux)
}

async function createWindow() {
  const port = await freePort();
  backendUrl = `http://127.0.0.1:${port}`;
  startBackend(port);
  const ok = await waitForHealth(backendUrl);
  if (!ok) console.error("[main] backend failed health check");

  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 960,
    minHeight: 600,
    show: false,
    backgroundColor: "#000000",
    icon: isMac ? undefined : appIconPath(),
    // Unified custom title bar across platforms: on macOS the traffic lights sit
    // inset on the left; on Windows/Linux the native window controls are drawn as
    // an overlay on the right. The app paints the bar itself (and makes it
    // draggable) — see the renderer's title-bar styles.
    titleBarStyle: isMac ? "hiddenInset" : "hidden",
    ...(isMac
      ? { trafficLightPosition: { x: 14, y: 14 } }
      : { titleBarOverlay: { color: "#0a0a0a", symbolColor: "#ededed", height: 60 } }),
    webPreferences: {
      preload: join(__dirname, "../preload/index.js"),
      sandbox: false,
      additionalArguments: [`--backend-url=${backendUrl}`, `--app-version=${app.getVersion()}`],
    },
  });

  // Show on the first paint, but never depend on it alone: if the renderer fails
  // to paint (a bad asset, a CSP refusal, a renderer crash) `ready-to-show` never
  // fires and a `show: false` window would leave the user staring at nothing.
  // did-finish-load and a last-resort timer both reveal it.
  const reveal = () => {
    if (mainWindow && !mainWindow.isDestroyed() && !mainWindow.isVisible()) mainWindow.show();
  };
  mainWindow.once("ready-to-show", reveal);
  mainWindow.webContents.once("did-finish-load", reveal);
  mainWindow.webContents.on("did-fail-load", (_e, code, desc, validated) =>
    console.error("[main] renderer failed to load:", code, desc, validated));
  mainWindow.webContents.on("render-process-gone", (_e, details) =>
    console.error("[main] renderer process gone:", details.reason));
  setTimeout(reveal, 8000);

  // Closing the window does NOT quit: hide to the background (the backend and any
  // runs keep going) and leave the tray/dock as the running indicator. The app
  // exits only on an explicit Quit (tray, app menu, Cmd+Q) which sets isQuitting.
  mainWindow.on("close", (e) => {
    if (!isQuitting) {
      e.preventDefault();
      mainWindow?.hide();
    }
  });
  mainWindow.on("closed", () => { mainWindow = null; });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  if (!app.isPackaged && process.env["ELECTRON_RENDERER_URL"]) {
    mainWindow.loadURL(process.env["ELECTRON_RENDERER_URL"]);
  } else {
    mainWindow.loadFile(join(__dirname, "../renderer/index.html"));
  }
}

// ---- single instance: one backend + one frontend, ever ----------------------
// A second launch must not spawn a parallel app; it just refocuses this one.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => showMainWindow());

  app.whenReady().then(() => {
    // No default menu bar on Windows/Linux (controls live in the title bar +
    // tray); keep the standard menu on macOS so Cmd+Q and friends work.
    if (!isMac) Menu.setApplicationMenu(null);
    createTray();
    createWindow();
  });

  // Re-show on dock/taskbar click instead of making a second window.
  app.on("activate", () => showMainWindow());

  // We hide rather than close, so this normally won't fire; if it ever does,
  // keep running (the tray is still there) — don't auto-quit.
  app.on("window-all-closed", () => {});

  app.on("before-quit", () => { isQuitting = true; stopBackend(); });
  app.on("will-quit", stopBackend);
  process.on("exit", stopBackend);
}
