#!/bin/sh
# The scripted e2e suite as CI runs it: real Codex and Claude Code, signed in with dummy API keys.
#
# In scripted mode nothing reaches a provider: keepmum forwards to the tap, and the tap to the
# scripted model, which never reads a credential. The agents only need one to start, so a dummy
# key in a throwaway folder is enough, and no real secret is needed. They sign in by API key, so
# this covers keepmum's API-key routes; the ChatGPT-login route is covered by running the suite
# on a Mac signed in that way (`python -m e2e run`).
#
# Usage: e2e/ci.sh [case ...]   (the agents must be on PATH; their versions are printed)
set -eu

work="$(mktemp -d "${TMPDIR:-/tmp}/keepmum-e2e-ci.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# Both logins live in $work only: the user's own (~/.codex, the Keychain) are never read or written.
export CODEX_HOME="$work/codex" CLAUDE_CONFIG_DIR="$work/claude"
mkdir -p "$CODEX_HOME" "$CLAUDE_CONFIG_DIR"
printf '%s' "sk-e2e-dummy-not-a-key-0000000000000000" | codex login --with-api-key >/dev/null
export ANTHROPIC_API_KEY="sk-ant-api03-e2e-dummy-not-a-key-$(printf '0%.0s' $(seq 1 60))AA"
export DISABLE_AUTOUPDATER=1

cd "$(dirname "$0")/.."
status=0
uv run --frozen python -m e2e run --check-tools "$@" || status=$?

# The summary (no values, no request bodies) for the CI page.
summary="$(ls -dt e2e/results/scripted-*/summary.md 2>/dev/null | head -1 || true)"
if [ -n "$summary" ] && [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  cat "$summary" >> "$GITHUB_STEP_SUMMARY"
fi
exit "$status"
