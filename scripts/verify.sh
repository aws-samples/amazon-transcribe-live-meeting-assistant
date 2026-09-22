#!/usr/bin/env bash
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
#
# Run the repository's no-AWS checks locally, in parallel, and print one table.
#
# Why: run one after another these checks take about three minutes, nearly all of
# it waiting. They fall into four groups that share no files, so the wall time can
# be the slowest group instead of the sum.
#
# Parallelism is BETWEEN groups only, never within one: everything inside a group
# shares a node_modules directory, and two npm processes in one directory corrupt
# each other.
#
#   python     cfn-lint, the pytest suites, the static template tests
#   node-vp    Virtual Participant backend + WebSocket transcriber (tsc, tests, smoke)
#   node-ui    React UI (eslint + vitest)
#   node-ext   browser extension (tests + production build)
#
# Each group is capped by `timeout`, so a hung check is reported as TIMEOUT rather
# than blocking the run forever. Group work is dispatched with `xargs -P` and the
# script re-invokes itself per group, rather than hand-rolled shell job control.
#
# DELIBERATE DIFFERENCE FROM CI: the make targets for the Node components run
# `npm ci`, which deletes and reinstalls node_modules and is most of their
# runtime. Locally that is usually redundant, so this installs only when
# node_modules is absent or older than the lockfile. Use --clean-install before
# trusting a result that depends on dependency resolution.
#
# Usage:
#   scripts/verify.sh                        # all groups, parallel
#   scripts/verify.sh python node-ui         # only these
#   scripts/verify.sh --clean-install        # reinstall node deps as CI does
#   scripts/verify.sh --timeout 600          # per-group cap, default 420s
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

ALL_GROUPS=(python node-vp node-ui node-ext)
GROUP_TIMEOUT="${LMA_VERIFY_TIMEOUT:-420}"
CLEAN_INSTALL="${LMA_VERIFY_CLEAN_INSTALL:-0}"

use_pinned_node() {
  if [ -s "$HOME/.nvm/nvm.sh" ]; then
    # shellcheck disable=SC1091
    . "$HOME/.nvm/nvm.sh" >/dev/null 2>&1
    nvm use "$(tr -d '[:space:]' < .nvmrc)" >/dev/null 2>&1 || true
  fi
}

ensure_node_deps() {
  local dir="$1"
  if [ "$CLEAN_INSTALL" = "1" ]; then
    (cd "$dir" && npm ci --prefer-offline --no-audit --no-fund --loglevel=error)
    return $?
  fi
  if [ ! -d "$dir/node_modules" ] || [ "$dir/package-lock.json" -nt "$dir/node_modules" ]; then
    (cd "$dir" && npm install --prefer-offline --no-audit --no-fund --loglevel=error)
    return $?
  fi
  return 0
}

# ── one group, run in a child invocation of this script ─────────────────────

if [ "${1:-}" = "--run-group" ]; then
  group="$2"
  use_pinned_node
  case "$group" in
    python)
      # These share only a read-only virtualenv, so they can run concurrently
      # with each other; cfn-lint alone is most of the group's wall time.
      printf '%s\n' lint-cfn test-sdk test-cli test-lambdas test-ai-stack \
                    test-integ-plumbing test-vp-template test-asr \
        | xargs -P 8 -I{} make {}
      ;;
    node-vp)
      ensure_node_deps lma-virtual-participant-stack/backend || exit 1
      ensure_node_deps lma-websocket-transcriber-stack/source/app || exit 1
      (cd lma-virtual-participant-stack/backend && npm test) || exit 1
      (cd lma-websocket-transcriber-stack/source/app && npm test && npm run smoke) || exit 1
      ;;
    node-ui)
      ensure_node_deps lma-ai-stack/source/ui || exit 1
      # Use the package's own scripts. `npm run lint` is scoped to src/; a bare
      # `eslint .` here walks node_modules and build/ and effectively never ends.
      (cd lma-ai-stack/source/ui && npm run lint) || exit 1
      # --run is required: without it vitest waits for input and never exits.
      (cd lma-ai-stack/source/ui && CI=true npx vitest run) || exit 1
      ;;
    node-ext)
      ensure_node_deps lma-browser-extension-stack || exit 1
      # CI=true keeps react-scripts out of watch mode. CI=false for the build
      # matches CodeBuild, which does not treat lint warnings as errors.
      (cd lma-browser-extension-stack && CI=true npm test) || exit 1
      (cd lma-browser-extension-stack && CI=false npm run build) || exit 1
      ;;
    *) echo "unknown group: $group" >&2; exit 2 ;;
  esac
  exit $?
fi

# ── parent: parse args, dispatch, report ───────────────────────────────────

SELECTED=()
while [ $# -gt 0 ]; do
  case "$1" in
    --clean-install) CLEAN_INSTALL=1 ;;
    --timeout) GROUP_TIMEOUT="$2"; shift ;;
    -h|--help) sed -n '6,34p' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) SELECTED+=("$1") ;;
  esac
  shift
done
[ ${#SELECTED[@]} -eq 0 ] && SELECTED=("${ALL_GROUPS[@]}")

LOGS="$(mktemp -d -t lma-verify-XXXXXX)"
export LMA_VERIFY_CLEAN_INSTALL="$CLEAN_INSTALL"

start_all=$(date +%s)
# One child per group, at most four at once. Each child records its own exit
# code and duration, so the parent needs no job bookkeeping.
printf '%s\n' "${SELECTED[@]}" | xargs -P 4 -I{} bash -c '
  g="{}"
  s=$(date +%s)
  timeout --kill-after=30s "'"$GROUP_TIMEOUT"'" "'"$REPO"'/scripts/verify.sh" --run-group "$g" \
    > "'"$LOGS"'/$g.log" 2>&1
  rc=$?
  echo "$rc $(( $(date +%s) - s ))" > "'"$LOGS"'/$g.rc"
'
end_all=$(date +%s)

printf '\n%-10s %7s  %s\n' "GROUP" "TIME" "RESULT"
printf '%-10s %7s  %s\n' "---------" "-------" "-------"
failed=0
summed=0
for g in "${SELECTED[@]}"; do
  if [ -f "$LOGS/$g.rc" ]; then read -r rc secs < "$LOGS/$g.rc"; else rc=1; secs=0; fi
  summed=$((summed + secs))
  case "$rc" in
    0)   printf '%-10s %6ss  PASS\n' "$g" "$secs" ;;
    124) printf '%-10s %6ss  TIMEOUT (>%ss)\n' "$g" "$secs" "$GROUP_TIMEOUT"; failed=1 ;;
    *)   printf '%-10s %6ss  FAIL (exit %s)\n' "$g" "$secs" "$rc"; failed=1 ;;
  esac
done
printf '%-10s %7s  %s\n' "---------" "-------" "-------"
printf '%-10s %6ss  groups summed: %ss\n' "WALL" "$((end_all - start_all))" "$summed"

if [ "$failed" != "0" ]; then
  echo
  for g in "${SELECTED[@]}"; do
    [ -f "$LOGS/$g.rc" ] || continue
    read -r rc _ < "$LOGS/$g.rc"
    [ "$rc" = "0" ] && continue
    echo "=== $g (exit $rc) ==="
    tail -40 "$LOGS/$g.log"
  done
  echo
  echo "Full logs: $LOGS"
  exit 1
fi

rm -rf "$LOGS"
echo
echo "Node deps reused where already current; --clean-install reinstalls as CI does."
