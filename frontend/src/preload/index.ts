import { contextBridge } from "electron";

// The main process passes runtime info via additionalArguments; expose it to the
// (sandboxed) renderer so the dumb frontend knows where to call and which version.
function arg(name: string, fallback = ""): string {
  const a = process.argv.find((x) => x.startsWith(`--${name}=`));
  return a ? a.slice(name.length + 3) : fallback;
}

contextBridge.exposeInMainWorld("api", {
  backendUrl: arg("backend-url", "http://127.0.0.1:8765"),
  version: arg("app-version", "0.0.0"),
  platform: process.platform, // "darwin" | "win32" | "linux" — drives the title bar layout
});
