#!/usr/bin/env bash
#
# ralph/once.sh — single-shot copilot run on the local host, raw output.
#
# Same prompt + scope as afk.sh, but without --output-format json / jq /
# sentinel detection. Use for the first try, debugging, or any time you want
# to see Copilot's full output unfiltered.
#
# Usage:
#   bash ralph/once.sh
#   MODEL=gpt-5.4 EFFORT=high bash ralph/once.sh
#
# Prereqs (one-time):
#   - copilot, git on PATH.
#   - GitHub Copilot CLI signed in (run `copilot` once interactively, or
#     follow the auth flow at https://docs.github.com/copilot/github-copilot-in-the-cli).

set -euo pipefail

trap 'rc=$?; printf "\nralph/once.sh aborted at line %s (exit %s): %s\n" "${BASH_LINENO[0]:-?}" "$rc" "${BASH_COMMAND}" >&2' ERR

MODEL="${MODEL:-claude-opus-4.7-1m-internal}"
EFFORT="${EFFORT:-xhigh}"

for cmd in copilot git; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: '$cmd' not found on PATH." >&2
    case "$cmd" in
      copilot) echo "  Install: npm install -g @github/copilot" >&2 ;;
      git)     echo "  Install: brew install git" >&2 ;;
    esac
    exit 1
  fi
done

if [ ! -f ralph/prompt.md ]; then
  echo "Error: ralph/prompt.md not found. Run this script from the repo root." >&2
  exit 1
fi

commits="$(git log -n 5 --format='%H%n%ad%n%B---' --date=short 2>/dev/null || echo 'No commits found')"
issues="$(find issues -type f -name '*.md' -not -path '*/done/*' -exec cat {} + 2>/dev/null || true)"
[ -z "$issues" ] && issues='No issues found'
prompt="$(cat ralph/prompt.md)"

exec copilot \
  --model "$MODEL" \
  --effort "$EFFORT" \
  --yolo \
  -p "Previous commits: $commits Issues: $issues $prompt"
