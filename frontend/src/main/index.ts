import { app, BrowserWindow, shell, Menu, Tray, nativeImage } from "electron";
import { spawn, ChildProcess } from "node:child_process";
import { join, resolve } from "node:path";
import { existsSync } from "node:fs";
import net from "node:net";
import http from "node:http";

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

function waitForHealth(url: string, timeoutMs = 30000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve) => {
    const tick = () => {
      const req = http.get(url + "/api/health", (r) => {
        r.resume();
        if (r.statusCode === 200) return resolve(true);
        retry();
      });
      req.on("error", retry);
      req.setTimeout(1000, () => req.destroy());
    };
    const retry = () => (Date.now() > deadline ? resolve(false) : setTimeout(tick, 400));
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
    // dev: run from the repo's venv against the source backend + dev-data
    const repoRoot = resolve(__dirname, "../../..");
    env.AUTOMATION_DATA_DIR = join(repoRoot, "dev-data");
    cmd = join(repoRoot, ".venv", "bin", process.platform === "win32" ? "python.exe" : "python");
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
  // window hides on close. Template image on mac (adapts to light/dark menu bar),
  // colored elsewhere. Skip silently if the asset is missing.
  const iconPath = isMac ? resourcePath("tray", "trayTemplate.png") : resourcePath("tray", "tray.png");
  if (!existsSync(iconPath)) return;
  const img = nativeImage.createFromPath(iconPath);
  if (isMac) img.setTemplateImage(true);
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
      ? { trafficLightPosition: { x: 18, y: 22 } }
      : { titleBarOverlay: { color: "#0a0a0a", symbolColor: "#ededed", height: 60 } }),
    webPreferences: {
      preload: join(__dirname, "../preload/index.js"),
      sandbox: false,
      additionalArguments: [`--backend-url=${backendUrl}`, `--app-version=${app.getVersion()}`],
    },
  });

  mainWindow.once("ready-to-show", () => mainWindow?.show());

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
