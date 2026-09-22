#!/usr/bin/env bash
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
#
# Mutation-test a suite: break the code it covers, confirm the suite fails, and
# report any mutation that survived.
#
# A test that cannot fail is worse than no test, and the only way to know is to
# break the code and watch. Doing that by hand is serial — edit, run, restore —
# and slow enough that it gets skipped. This runs each mutation in its own `git
# worktree`, so they genuinely run at once and none can see another's edit or
# leave anything behind in your checkout.
#
# A MUTATION FILE describes the mutations, one per stanza, blank-line separated:
#
#   name: a short label for the report
#   file: path/relative/to/repo/root.py
#   old: the exact text to replace (single line)
#   new: the text to replace it with (single line; may be empty to delete)
#
# For multi-line or awkward edits use `sed:` instead of old/new with a sed script.
#
# Every mutation must ALSO be checked for having applied at all. A pattern that
# no longer matches produces a silent no-op, which reads exactly like a caught
# mutation if you only look at the exit code — so a mutation whose file is
# unchanged is reported as NOT-APPLIED, not as caught.
#
# Usage:
#   scripts/mutate.sh <mutation-file> <test-command>
#
#   scripts/mutate.sh /tmp/muts.txt \
#     'cd lma-ai-stack/source/lambda_functions/foo && pytest -q test_index.py'
#
# The test command runs with the worktree as its working directory. It should
# FAIL when the mutation is applied — that is the point.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MUTATION_FILE="${1:?usage: mutate.sh <mutation-file> <test-command>}"
TEST_CMD="${2:?usage: mutate.sh <mutation-file> <test-command>}"
JOBS="${LMA_MUTATE_JOBS:-6}"
# Per-mutation cap on the test command. A mutation that makes the code loop
# forever is a legitimate result to report, not a reason to hang.
TEST_TIMEOUT="${LMA_MUTATE_TEST_TIMEOUT:-120}"

[ -f "$MUTATION_FILE" ] || { echo "no such mutation file: $MUTATION_FILE" >&2; exit 2; }

WORK="$(mktemp -d -t lma-mutate-XXXXXX)"
TREES="$WORK/trees"
mkdir -p "$TREES"

cleanup() {
  # Remove the worktrees before the directory, or git keeps stale registrations.
  if [ -d "$TREES" ]; then
    for d in "$TREES"/*; do
      [ -d "$d" ] && git -C "$REPO" worktree remove --force "$d" >/dev/null 2>&1
    done
  fi
  git -C "$REPO" worktree prune >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

# ── parse the mutation file into one directory per mutation ─────────────────

python3 "$REPO/scripts/_mutate_helper.py" parse "$MUTATION_FILE" "$WORK" >/dev/null || exit 2
COUNT=$(ls "$WORK/specs" | wc -l)
[ "$COUNT" -gt 0 ] || { echo "no mutations parsed" >&2; exit 2; }
echo "Running $COUNT mutations, $JOBS at a time, each in its own worktree."

# ── run one mutation in an isolated worktree ────────────────────────────────

run_one() {
  local spec="$1"
  local id name tree
  id="$(basename "$spec" .json)"
  name="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["name"])' "$spec")"
  tree="$TREES/$id"

  if ! git -C "$REPO" worktree add --detach --quiet "$tree" HEAD 2>"$WORK/$id.wt"; then
    echo "$id|$name|WORKTREE-FAILED|0" >> "$WORK/results"
    return
  fi

  # Replay the parent's uncommitted state so the mutation sees the code as it
  # currently stands, not as it was last committed.
  if [ -s "$PATCH" ] && ! git -C "$tree" apply "$PATCH" 2>>"$WORK/$id.wt"; then
    echo "$id|$name|PATCH-FAILED|0" >> "$WORK/results"
    git -C "$REPO" worktree remove --force "$tree" >/dev/null 2>&1
    return
  fi
  while IFS= read -r u; do
    [ -n "$u" ] || continue
    mkdir -p "$tree/$(dirname "$u")"
    cp "$REPO/$u" "$tree/$u"
  done < "$UNTRACKED"

  # A worktree is a clean checkout, so every gitignored dependency directory is
  # absent -- and a suite that cannot find its dependencies fails for that reason,
  # which reads as a caught mutation. Link them in from the main checkout. Safe to
  # share: a mutation only ever edits tracked source.
  while IFS= read -r nm; do
    rel="${nm#"$REPO"/}"
    mkdir -p "$tree/$(dirname "$rel")"
    ln -sfn "$nm" "$tree/$rel"
  done < <(find "$REPO" -maxdepth 5 -name node_modules -type d \
             -not -path '*/node_modules/*' -not -path "$REPO/.claude/*" 2>/dev/null)
  for venv in "$REPO/.venv" "$REPO/lma-asr-microvm-stack/source/.venv"; do
    [ -d "$venv" ] && ln -sfn "$venv" "$tree/${venv#"$REPO"/}"
  done

  # Apply the mutation inside the worktree only.
  if ! python3 "$REPO/scripts/_mutate_helper.py" apply "$spec" "$tree"
  then
    echo "$id|$name|NOT-APPLIED|0" >> "$WORK/results"
    git -C "$REPO" worktree remove --force "$tree" >/dev/null 2>&1
    return
  fi

  # Run the suite. It is expected to FAIL; a pass means the mutation survived.
  local out rc
  out="$WORK/$id.out"
  # Bounded: a mutation can turn a loop into an infinite one, and an unbounded
  # run would then hang the whole harness rather than reporting that mutation.
  ( cd "$tree" && timeout --kill-after=15s "$TEST_TIMEOUT" bash -c "$TEST_CMD" ) >"$out" 2>&1
  rc=$?

  if [ "$rc" -eq 0 ]; then
    echo "$id|$name|SURVIVED|$rc" >> "$WORK/results"
  elif [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    echo "$id|$name|HUNG|$rc" >> "$WORK/results"
  elif grep -qE 'SyntaxError|IndentationError|ImportError|ModuleNotFoundError|error TS[0-9]|Cannot find module|Failed to compile|Unexpected token|command not found|not recognized|Cannot find package' "$out"; then
    # The suite failed, but because the code stopped loading rather than because
    # a test noticed. That is a bad mutation, not a caught one.
    echo "$id|$name|BROKEN-MUTATION|$rc" >> "$WORK/results"
  else
    echo "$id|$name|caught|$rc" >> "$WORK/results"
  fi
  git -C "$REPO" worktree remove --force "$tree" >/dev/null 2>&1
}
# A worktree is checked out from HEAD, so uncommitted work would be invisible and
# every mutation pattern touching it would report NOT-APPLIED. Carry the working
# tree across: tracked edits as a patch, plus any untracked file. This is what
# makes it possible to mutation-test a change *before* committing it, which is
# when you actually want to know.
PATCH="$WORK/uncommitted.patch"
git -C "$REPO" diff HEAD > "$PATCH"
UNTRACKED="$WORK/untracked.list"
git -C "$REPO" ls-files --others --exclude-standard > "$UNTRACKED"

export -f run_one
export REPO WORK TREES TEST_CMD PATCH UNTRACKED TEST_TIMEOUT

ls "$WORK/specs"/*.json | xargs -P "$JOBS" -I{} bash -c 'run_one "$@"' _ {}

# ── report ──────────────────────────────────────────────────────────────────

printf '\n%-4s %-52s %s\n' "ID" "MUTATION" "RESULT"
printf '%-4s %-52s %s\n' "----" "----------------------------------------------------" "------"
problems=0
while IFS='|' read -r id name status rc; do
  case "$status" in
    caught) printf '%-4s %-52s caught\n' "$id" "${name:0:52}" ;;
    *)      printf '%-4s %-52s *** %s ***\n' "$id" "${name:0:52}" "$status"; problems=1 ;;
  esac
done < <(sort "$WORK/results")

total=$(wc -l < "$WORK/results" 2>/dev/null || echo 0)
if [ "$total" -ne "$COUNT" ]; then
  echo
  echo "Recorded $total results for $COUNT mutations — the harness itself failed."
  echo "Refusing to report a pass. Logs: $WORK"
  exit 1
fi
caught=$(grep -c '|caught|' "$WORK/results" || true)
printf '\n%s of %s caught.\n' "$caught" "$total"

if [ "$problems" != "0" ]; then
  echo
  echo "Anything not 'caught' needs attention:"
  echo "  SURVIVED         the suite passed with broken code — the test is blind."
  echo "  NOT-APPLIED      the pattern did not match, so nothing was tested."
  echo "  BROKEN-MUTATION  the code stopped loading; rewrite the mutation."
  echo "  HUNG             the mutated code did not terminate within"
  echo "                   ${TEST_TIMEOUT}s. Often a real finding: the code under"
  echo "                   test has an unbounded loop the mutation exposed."
  exit 1
fi
echo "Every mutation was caught by a failing assertion."
