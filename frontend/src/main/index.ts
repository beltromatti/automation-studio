import { app, BrowserWindow, shell } from "electron";
import { spawn, ChildProcess } from "node:child_process";
import { join, resolve } from "node:path";
import { existsSync } from "node:fs";
import net from "node:net";
import http from "node:http";

let backend: ChildProcess | null = null;
let backendUrl = "";

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
  const env = { ...process.env, AUTOMATION_PORT: String(port), AUTOMATION_VERSION: app.getVersion() };
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

async function createWindow() {
  const port = await freePort();
  backendUrl = `http://127.0.0.1:${port}`;
  startBackend(port);
  const ok = await waitForHealth(backendUrl);
  if (!ok) console.error("[main] backend failed health check");

  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 960,
    minHeight: 600,
    backgroundColor: "#000000",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    webPreferences: {
      preload: join(__dirname, "../preload/index.js"),
      sandbox: false,
      additionalArguments: [`--backend-url=${backendUrl}`, `--app-version=${app.getVersion()}`],
    },
  });

  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  if (!app.isPackaged && process.env["ELECTRON_RENDERER_URL"]) {
    win.loadURL(process.env["ELECTRON_RENDERER_URL"]);
  } else {
    win.loadFile(join(__dirname, "../renderer/index.html"));
  }
}

app.whenReady().then(createWindow);
app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
app.on("window-all-closed", () => app.quit());
app.on("before-quit", stopBackend);
app.on("will-quit", stopBackend);
process.on("exit", stopBackend);
