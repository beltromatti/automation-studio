// Cross-platform production build: stage the frozen backend, build the renderer/
// main with electron-vite, then package with electron-builder for one target/arch.
// Signing is conditional on the presence of signing secrets in the environment, so
// the same pipeline produces signed installers when certs are provided and
// ad-hoc/unsigned (still installable) ones otherwise.
//
// Usage:  node scripts/build.mjs <mac|win|linux> [arm64|x64]
import { execSync } from "node:child_process";
import { cpSync, rmSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repo = resolve(root, "..");

const target = (process.argv[2] || { darwin: "mac", win32: "win", linux: "linux" }[process.platform] || "linux").toLowerCase();
const arch = (process.argv[3] || process.arch).toLowerCase(); // arm64 | x64

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

// 1) stage the frozen backend (built per-platform by the CI before this runs)
const src = resolve(repo, "backend", "dist", "automation-backend");
const dst = resolve(root, "resources", "backend");
if (!existsSync(src)) {
  console.error(`Frozen backend missing at ${src}\n  cd backend && pyinstaller --noconfirm automation-backend.spec`);
  process.exit(1);
}
console.log(`[build] target=${target} arch=${arch} — staging backend`);
rmSync(dst, { recursive: true, force: true });
cpSync(src, dst, { recursive: true });

// 2) build renderer + main
execSync("npx electron-vite build", { cwd: root, stdio: "inherit", env });

// 3) package
const flags = [`--${target}`, `--${arch}`, "--publish", "never"];
if (target === "mac" && !env.CSC_LINK) {
  // no Apple cert provided → ad-hoc sign so it still launches locally
  flags.push("-c.mac.identity=null");
  console.log("[build] no CSC_LINK → ad-hoc (unsigned) macOS build");
} else if (target === "mac") {
  if (env.APPLE_ID && env.APPLE_APP_SPECIFIC_PASSWORD && env.APPLE_TEAM_ID) {
    flags.push("-c.mac.notarize=true");
    console.log("[build] signing + notarizing macOS build");
  } else {
    console.log("[build] signing macOS build (no notarization creds → skipping notarize)");
  }
}
if (target === "win" && !env.CSC_LINK) {
  console.log("[build] no CSC_LINK → unsigned Windows build");
}
execSync(`npx electron-builder ${flags.join(" ")}`, { cwd: root, stdio: "inherit", env });
console.log("[build] done -> frontend/release");
