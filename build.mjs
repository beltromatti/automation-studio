#!/usr/bin/env node
/**
 * Root build script for Automation Studio.
 *
 * Usage:
 *   node build.mjs <version>                        # mac-arm64 (default)
 *   node build.mjs <version> --all                  # mac-arm64 + mac-x64
 *   node build.mjs <version> --target=mac-arm64
 *   node build.mjs <version> --target=mac-x64
 *   node build.mjs <version> --target=win-x64       # intended for CI (Windows runner)
 *   node build.mjs <version> --target=linux-x64     # intended for CI (Linux runner)
 *
 * Prerequisites:
 *   All targets  — frozen Python backend at backend/dist/automation-backend/
 *                  (the binary must match the target platform; CI freezes it
 *                  on the native runner before calling this script)
 *   mac targets  — patchright Chromium staged at frontend/resources/chromium/
 *                  (run: PLAYWRIGHT_BROWSERS_PATH=frontend/resources/chromium
 *                         python -m patchright install chromium)
 *
 * Apple signing (mac targets):
 *   Detects a "Developer ID Application" cert in the login keychain via
 *   `security find-identity`.  Three modes, picked automatically:
 *     developer-id          — cert + ASC API key → full signing + notarization
 *     developer-id-no-notary — cert only (no APPLE_API_KEY trio) → signed, not notarized
 *     ad-hoc                — no cert → ad-hoc sig via frontend/scripts/after-sign.cjs
 *
 * Windows: unsigned (SmartScreen warning on first launch; acceptable for now).
 */

import { spawnSync } from 'node:child_process'
import {
  cpSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  rmSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs'
import { dirname, extname, isAbsolute, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const projectRoot = fileURLToPath(new URL('.', import.meta.url))
const frontendRoot = join(projectRoot, 'frontend')
const backendDistDir = join(projectRoot, 'backend', 'dist', 'automation-backend')
const frontendPkg = join(frontendRoot, 'package.json')

const npmCmd = process.platform === 'win32' ? 'npm.cmd' : 'npm'
const npxCmd = process.platform === 'win32' ? 'npx.cmd' : 'npx'

const SEMVER_RE = /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/

// ─── target registry ──────────────────────────────────────────────────────────

const TARGETS = {
  'mac-arm64': {
    id: 'mac-arm64',
    label: 'macOS Apple Silicon',
    platform: 'darwin',
    builderArgs: ['--mac', '--arm64'],
    artifactExts: ['.dmg', '.zip', '.blockmap'],
    artifactYmls: ['latest-mac.yml'],
  },
  'mac-x64': {
    id: 'mac-x64',
    label: 'macOS Intel',
    platform: 'darwin',
    builderArgs: ['--mac', '--x64'],
    artifactExts: ['.dmg', '.zip', '.blockmap'],
    artifactYmls: ['latest-mac.yml'],
  },
  'win-x64': {
    id: 'win-x64',
    label: 'Windows x64',
    platform: 'win32',
    builderArgs: ['--win', '--x64'],
    artifactExts: ['.exe', '.blockmap'],
    artifactYmls: ['latest.yml'],
  },
  'linux-x64': {
    id: 'linux-x64',
    label: 'Linux x64',
    platform: 'linux',
    builderArgs: ['--linux', '--x64'],
    artifactExts: ['.appimage', '.blockmap'],
    artifactYmls: ['latest-linux.yml'],
  },
}

const DEFAULT_TARGET_IDS = ['mac-arm64']
const ALL_LOCAL_TARGET_IDS = ['mac-arm64', 'mac-x64']
const ALL_TARGET_IDS = ['mac-arm64', 'mac-x64', 'win-x64', 'linux-x64']

// ─── helpers ──────────────────────────────────────────────────────────────────

function parseArgs(argv) {
  const positional = []
  const flags = new Set()
  const options = {}
  for (const arg of argv) {
    if (arg.startsWith('--')) {
      const body = arg.slice(2)
      const eq = body.indexOf('=')
      if (eq >= 0) options[body.slice(0, eq)] = body.slice(eq + 1)
      else flags.add(body)
    } else {
      positional.push(arg)
    }
  }
  return { positional, flags, options }
}

function normalizeVersion(raw) {
  if (!raw) throw new Error('Usage: node build.mjs <version> [--all] [--target=<id>]')
  const v = raw.trim().replace(/^v/, '')
  const padded =
    v.split('.').length === 3 ? v : v.split('.').length === 2 ? `${v}.0` : `${v}.0.0`
  if (!SEMVER_RE.test(padded)) throw new Error(`Invalid version "${raw}"`)
  return padded
}

function run(cmd, args, label, opts = {}) {
  const result = spawnSync(cmd, args, {
    stdio: 'inherit',
    cwd: frontendRoot,
    shell: process.platform === 'win32',
    ...opts,
  })
  if (result.error) throw result.error
  if (result.status !== 0) throw new Error(`${label} exited with code ${result.status}`)
}

function ensureDir(p) {
  mkdirSync(p, { recursive: true })
}

// ─── Apple signing detection ──────────────────────────────────────────────────

function resolveMacSigningMode() {
  if (process.platform !== 'darwin') return { mode: 'not-mac', extraArgs: [], extraEnv: {} }

  const explicitAdHoc =
    String(process.env.CSC_IDENTITY_AUTO_DISCOVERY ?? '').toLowerCase() === 'false'

  // In CI (GitHub Actions) use Developer ID when the cert is available.
  // On a local Mac, Developer ID without notarization is blocked by Gatekeeper
  // on macOS 13+ — always use ad-hoc locally so the app runs out of the box.
  const isCI = !!(process.env.CI || process.env.GITHUB_ACTIONS)

  if (!isCI || explicitAdHoc) {
    return {
      mode: 'ad-hoc',
      extraArgs: [],
      extraEnv: { CSC_IDENTITY_AUTO_DISCOVERY: 'false' },
    }
  }

  const probe = spawnSync('security', ['find-identity', '-v', '-p', 'codesigning'], {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const out = `${probe.stdout ?? ''}${probe.stderr ?? ''}`
  const hasCert = /Developer ID Application/.test(out)

  if (!hasCert) {
    return {
      mode: 'ad-hoc',
      extraArgs: [],
      extraEnv: { CSC_IDENTITY_AUTO_DISCOVERY: 'false' },
    }
  }

  const apiKeyPath = (process.env.APPLE_API_KEY ?? '').trim()
  const apiKeyId = (process.env.APPLE_API_KEY_ID ?? '').trim()
  const apiIssuer = (process.env.APPLE_API_ISSUER ?? '').trim()
  const hasAscKey = apiKeyPath && apiKeyId && apiIssuer

  const appleId = (process.env.APPLE_ID ?? '').trim()
  const appPwd = (process.env.APPLE_APP_SPECIFIC_PASSWORD ?? '').trim()
  const teamId = (process.env.APPLE_TEAM_ID ?? '').trim()
  const hasPasswordTrio = appleId && appPwd && teamId

  if (hasAscKey || hasPasswordTrio) {
    return {
      mode: 'developer-id',
      extraArgs: ['--config.mac.hardenedRuntime=true', '--config.mac.notarize=true'],
      extraEnv: {},
    }
  }

  return {
    mode: 'developer-id-no-notary',
    extraArgs: ['--config.mac.hardenedRuntime=true', '--config.mac.notarize=false'],
    extraEnv: {},
  }
}

function describeSigningMode(mode) {
  return (
    {
      'developer-id': 'Developer ID + notarization',
      'developer-id-no-notary': 'Developer ID (no notarization creds)',
      'ad-hoc': 'ad-hoc (right-click → Open required on first launch)',
    }[mode] ?? mode
  )
}

// ─── backend staging ──────────────────────────────────────────────────────────

function stageBackend() {
  if (!existsSync(backendDistDir)) {
    throw new Error(
      `Frozen backend not found at ${backendDistDir}\n` +
        `  Run: cd backend && pyinstaller --noconfirm automation-backend.spec`,
    )
  }
  const dst = join(frontendRoot, 'resources', 'backend')
  console.log(`==> Staging backend → resources/backend/`)
  rmSync(dst, { recursive: true, force: true })
  cpSync(backendDistDir, dst, { recursive: true })
  fixAbsoluteSymlinks(dst, backendDistDir, dst)
}

/**
 * cpSync resolves relative symlinks to absolute paths pointing back at the
 * source tree. codesign rejects these ("invalid destination for symbolic link
 * in bundle" / "unsealed contents") because the targets exit the bundle.
 * Walk the entire dest tree and rewrite every absolute symlink to the
 * equivalent relative path within the dest tree.
 */
function fixAbsoluteSymlinks(dir, sourceBase, destBase) {
  const entries = readdirSync(dir, { withFileTypes: true })
  for (const e of entries) {
    const full = join(dir, e.name)
    if (e.isSymbolicLink()) {
      const absTarget = readlinkSync(full)
      if (!isAbsolute(absTarget)) continue
      // Map the absolute source target to its equivalent path in the dest tree,
      // then express it as relative to the symlink's containing directory.
      const relFromSource = relative(sourceBase, absTarget)
      const destTarget = join(destBase, relFromSource)
      const relFromLink = relative(dirname(full), destTarget)
      unlinkSync(full)
      symlinkSync(relFromLink, full)
    } else if (e.isDirectory()) {
      fixAbsoluteSymlinks(full, sourceBase, destBase)
    }
  }
}

function checkChromium() {
  const dir = join(frontendRoot, 'resources', 'chromium')
  if (!existsSync(dir) || readdirSync(dir).length === 0) {
    throw new Error(
      `Chromium not staged at ${dir}\n` +
        `  Run: PLAYWRIGHT_BROWSERS_PATH=${dir} python -m patchright install chromium`,
    )
  }
}

// ─── version management ───────────────────────────────────────────────────────

function withTransientVersion(version, fn) {
  const original = readFileSync(frontendPkg, 'utf8')
  const parsed = JSON.parse(original)
  if (parsed.version === version) return fn()
  const was = parsed.version
  parsed.version = version
  writeFileSync(frontendPkg, `${JSON.stringify(parsed, null, 2)}\n`)
  console.log(`==> frontend/package.json: ${was} → ${version} (transient)`)
  try {
    return fn()
  } finally {
    writeFileSync(frontendPkg, original)
    console.log(`==> frontend/package.json version restored`)
  }
}

// ─── artifact collection ──────────────────────────────────────────────────────

function collectArtifacts(outputDir, target, releaseDir) {
  const collected = []
  for (const entry of readdirSync(outputDir, { withFileTypes: true })) {
    if (entry.isDirectory()) continue
    const ext = extname(entry.name).toLowerCase()
    const isYml = entry.name.endsWith('.yml') && target.artifactYmls.some(y => entry.name === y)
    if (target.artifactExts.includes(ext) || isYml) {
      const src = join(outputDir, entry.name)
      const dst = join(releaseDir, entry.name)
      // cpSync single file: use readFileSync + writeFileSync for compatibility
      const buf = readFileSync(src)
      writeFileSync(dst, buf)
      collected.push(dst)
    }
  }
  return collected
}

// ─── single-target build ──────────────────────────────────────────────────────

function buildTarget(target, releaseDir) {
  const outputDir = join(projectRoot, 'dist', '.release-build', target.id)
  rmSync(outputDir, { recursive: true, force: true })
  ensureDir(outputDir)

  stageBackend()
  if (target.platform === 'darwin') checkChromium()

  const isMac = target.platform === 'darwin'
  const signing = isMac ? resolveMacSigningMode() : { mode: 'not-mac', extraArgs: [], extraEnv: {} }

  if (isMac) {
    console.log(`    signing: ${describeSigningMode(signing.mode)}`)
  }

  run(
    npxCmd,
    [
      'electron-builder',
      ...target.builderArgs,
      '--publish', 'never',
      `--config.directories.output=${outputDir}`,
      ...signing.extraArgs,
    ],
    `package ${target.label}`,
    { env: { ...process.env, AUTOMATION_STUDIO_MAC_SIGNING_MODE: signing.mode, ...signing.extraEnv } },
  )

  const artifacts = collectArtifacts(outputDir, target, releaseDir)
  if (artifacts.length === 0) {
    throw new Error(`No artifacts found in ${outputDir} for target ${target.id}`)
  }
  return artifacts
}

// ─── main ─────────────────────────────────────────────────────────────────────

export function runBuild({ rawVersion, buildAll = false, explicitTarget = null } = {}) {
  const version = normalizeVersion(rawVersion)

  let targetIds
  if (explicitTarget) {
    if (!TARGETS[explicitTarget]) {
      throw new Error(`Unknown target "${explicitTarget}". Valid: ${ALL_TARGET_IDS.join(', ')}`)
    }
    targetIds = [explicitTarget]
  } else if (buildAll) {
    targetIds = ALL_LOCAL_TARGET_IDS
  } else {
    targetIds = DEFAULT_TARGET_IDS
  }

  const releaseDir = join(projectRoot, 'release', `v${version}`)
  ensureDir(releaseDir)

  console.log(`\nAutomation Studio ${version} — targets: ${targetIds.join(', ')}`)
  console.log(`Artifacts → ${releaseDir}\n`)

  const allArtifacts = []

  withTransientVersion(version, () => {
    // Build renderer/main once (platform-independent JS)
    console.log('==> Building frontend (electron-vite)')
    run(npmCmd, ['run', 'build'], 'electron-vite build')

    for (const id of targetIds) {
      console.log(`\n==> Building ${TARGETS[id].label}`)
      const artifacts = buildTarget(TARGETS[id], releaseDir)
      allArtifacts.push(...artifacts)
    }
  })

  console.log('\nArtifacts:')
  for (const p of allArtifacts) console.log(`  ${p}`)

  return allArtifacts
}

// Allow `node build.mjs --target=win-x64 <version>` from CI (root cwd)
const isMain = process.argv[1] === fileURLToPath(import.meta.url)
if (isMain) {
  const { positional, flags, options } = parseArgs(process.argv.slice(2))
  try {
    runBuild({
      rawVersion: positional[0],
      buildAll: flags.has('all'),
      explicitTarget: options.target ?? null,
    })
  } catch (e) {
    console.error(`\n${e instanceof Error ? e.message : e}`)
    process.exit(1)
  }
}
