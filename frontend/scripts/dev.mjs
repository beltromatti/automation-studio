// Dev launcher. Some environments export ELECTRON_RUN_AS_NODE=1, which makes the
// Electron binary boot as plain Node (so `require('electron').app` is undefined
// and the app crashes). Strip it before launching electron-vite so Electron runs
// as Electron. No-op on machines that don't set it.
import { spawn } from "node:child_process";

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

const p = spawn("electron-vite", ["dev"], {
  stdio: "inherit",
  env,
  shell: process.platform === "win32",
});
p.on("exit", (code) => process.exit(code ?? 0));
