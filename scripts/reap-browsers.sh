#!/usr/bin/env bash
# Emergency reaper — kill every browser/server THIS automation project spawned.
#
# Matched strictly by this repo's venv binary and profiles/ path, so your
# personal Google Chrome (which uses a different --user-data-dir) is never
# touched. Run this any time things feel heavy:  bash scripts/reap-browsers.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

pkill -9 -f "$ROOT/.venv/bin/hb serve" 2>/dev/null || true
pkill -9 -f "$ROOT/profiles" 2>/dev/null || true

# Orphaned (ppid 1) Chrome referencing our profiles dir — killing each main makes
# its renderer/GPU helper processes self-terminate.
ps -axo pid,ppid,command | awk -v d="$ROOT/profiles" '$2==1 && index($0,d){print $1}' \
  | while read -r p; do kill -9 "$p" 2>/dev/null || true; done

LEFT=$(ps -axo command | grep -c "$ROOT/profiles")
echo "[reap] done — automation processes referencing $ROOT/profiles now: $((LEFT > 0 ? LEFT - 1 : 0)) (your personal Chrome is untouched)"
