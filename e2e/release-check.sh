#!/bin/sh
# Before a release: the live suite on real models, at the cheapest prices that pass it.
#
#   e2e/release-check.sh              every live case, once
#   e2e/release-check.sh --repeat 2   twice each (a flaky case shows as 1 of 2)
#   e2e/release-check.sh val-         some cases (by id prefix)
#
# Codex, opencode and pi run on gpt-6-luna and Claude Code on Claude Haiku 5.5 (`--auth keys
# --cheap`), on the API keys e2e/keys.env names in 1Password: about $0.15 a pass (Nov 2026), the
# cost printed at the end. The Antigravity CLI runs on the user's Google login (no key, no cost), if
# it is signed in. Results and costs: e2e/results/live-*/summary.md.
#
# As fast as it goes: the four agents run side by side (-j 4; each agent one run at a time, since a
# login or session list is its own, and runs that use a model on this Mac one at a time across both
# runners), and agy beside them. About 8 minutes, set by agy's 17 runs; one after another it was 20.
# E2E_JOBS=1 runs them one at a time.
set -eu
cd "$(dirname "$0")/.."

if [ ! -f e2e/keys.env ]; then
  echo "e2e/keys.env is missing: cp e2e/keys.env.example e2e/keys.env, and point it at your 1Password items" >&2
  exit 2
fi
command -v op >/dev/null || { echo "the 1Password CLI (op) is not installed" >&2; exit 2; }

jobs=${E2E_JOBS:-4}
agy_log=$(mktemp "${TMPDIR:-/tmp}/keepmum-agy.XXXXXX")
trap 'rm -f "$agy_log"' EXIT
agy_pid=
if command -v agy >/dev/null; then
  # Its lines are shown after the others', so the two runners' summaries don't mix.
  uv run --frozen python -m e2e run --mode live --agent agy -j "$jobs" "$@" >"$agy_log" 2>&1 &
  agy_pid=$!
else
  echo "agy is not installed: the Antigravity CLI was not checked live" >&2
fi

status=0
op run --env-file e2e/keys.env -- uv run --frozen python -m e2e run --mode live --auth keys --cheap \
  --agent codex --agent claude --agent pi --agent opencode -j "$jobs" "$@" || status=$?

if [ -n "$agy_pid" ]; then
  agy_status=0
  wait "$agy_pid" || agy_status=$?
  cat "$agy_log"
  [ "$agy_status" -eq 0 ] || status=$agy_status
fi
exit "$status"
