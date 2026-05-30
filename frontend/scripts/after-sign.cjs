/**
 * electron-builder `afterSign` hook for Automation Studio.
 *
 * Two branches:
 *
 *   developer-id / developer-id-no-notary
 *     electron-builder (via @electron/osx-sign) already signed everything,
 *     including Contents/Resources/ where the Python backend and Chromium live.
 *     Re-signing here would clobber the Developer ID signature with an ad-hoc
 *     one. We just verify and log.
 *
 *   ad-hoc (no Developer ID cert in keychain)
 *     macOS Sequoia's mandatory signature check requires every Mach-O that
 *     executes to carry at least an ad-hoc signature. codesign --deep on the
 *     outer .app does NOT automatically recurse into arbitrary Resources/
 *     subdirectories that contain nested .app bundles (Chromium.app). We:
 *       1. sign nested .app bundles in Resources/ first (with --deep so their
 *          own Frameworks / Helpers get signed before the bundle seal),
 *       2. sign loose Mach-O files in Resources/ (Python backend, .so, .dylib),
 *       3. sign the outer .app bundle (handles Electron + Frameworks/ + Helpers/).
 */

'use strict'

const { execFileSync, spawnSync } = require('node:child_process')
const path = require('node:path')
const fs = require('node:fs')

module.exports = async function afterSign(context) {
  const { electronPlatformName, appOutDir, packager } = context
  if (electronPlatformName !== 'darwin') return

  const appName = packager.appInfo.productFilename
  const appPath = path.join(appOutDir, `${appName}.app`)
  if (!fs.existsSync(appPath)) {
    console.warn(`[after-sign] ${appPath} not found; skipping`)
    return
  }

  const mode = process.env.AUTOMATION_STUDIO_MAC_SIGNING_MODE ?? 'ad-hoc'

  if (mode === 'developer-id' || mode === 'developer-id-no-notary') {
    console.log(`[after-sign] mode=${mode} — verifying Developer ID signature`)
    execFileSync('codesign', ['--verify', '--deep', '--strict', appPath], { stdio: 'inherit' })
    console.log(`[after-sign] Developer ID signature verified`)
    return
  }

  // ── ad-hoc ────────────────────────────────────────────────────────────────
  console.log(`[after-sign] mode=ad-hoc — signing ${appPath}`)

  const resourcesDir = path.join(appPath, 'Contents', 'Resources')

  if (fs.existsSync(resourcesDir)) {
    // 1) Sign nested .app bundles (e.g. Chromium.app inside resources/chromium/)
    const nestedApps = findNestedApps(resourcesDir)
    for (const nested of nestedApps) {
      console.log(`[after-sign]   nested bundle: ${path.relative(appPath, nested)}`)
      execFileSync(
        'codesign',
        ['--force', '--deep', '--sign', '-', '--timestamp=none', nested],
        { stdio: 'pipe' },
      )
    }

    // 2) Sign loose Mach-O files in Resources/ (Python backend binary, .so, .dylib)
    signLooseBinaries(resourcesDir)
  }

  // 3) Sign the outer .app (Electron frameworks, helpers, main executable)
  execFileSync(
    'codesign',
    ['--force', '--deep', '--sign', '-', '--timestamp=none', appPath],
    { stdio: 'inherit' },
  )

  execFileSync('codesign', ['--verify', '--deep', '--strict', appPath], { stdio: 'inherit' })
  console.log('[after-sign] ad-hoc signature verified')
}

/** Recursively find all .app bundles inside a directory (not the dir itself). */
function findNestedApps(dir) {
  const found = []
  function walk(d) {
    let entries
    try { entries = fs.readdirSync(d, { withFileTypes: true }) } catch { return }
    for (const e of entries) {
      if (e.isSymbolicLink()) continue
      const full = path.join(d, e.name)
      if (e.isDirectory()) {
        if (e.name.endsWith('.app')) found.push(full)
        else walk(full)
      }
    }
  }
  walk(dir)
  return found
}

/**
 * Walk dir, try to ad-hoc sign every regular file; non-Mach-O files cause
 * codesign to exit non-zero — we swallow those errors silently.
 * Skip .app subdirectories (already handled by findNestedApps).
 */
function signLooseBinaries(dir) {
  function walk(d) {
    let entries
    try { entries = fs.readdirSync(d, { withFileTypes: true }) } catch { return }
    for (const e of entries) {
      if (e.isSymbolicLink()) continue
      const full = path.join(d, e.name)
      if (e.isDirectory()) {
        if (!e.name.endsWith('.app')) walk(full)
      } else if (e.isFile()) {
        spawnSync('codesign', ['--force', '--sign', '-', '--timestamp=none', full], {
          stdio: 'pipe',
        })
        // non-zero exit = not a Mach-O → ignored
      }
    }
  }
  walk(dir)
}
