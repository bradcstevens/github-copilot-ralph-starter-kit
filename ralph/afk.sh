#!/usr/bin/env bash
#
# ralph/afk.sh — autonomous ralph loop running entirely on the local host.
#
# Each iteration runs `copilot ...` against ralph/prompt.md plus every open
# issue under issues/. Streams Copilot's text output to the terminal and
# exits when the terminal assistant message of an iteration contains the
# sentinel <promise>NO MORE TASKS</promise>.
#
# Usage:
#   bash ralph/afk.sh                       # unlimited iterations
#   bash ralph/afk.sh 50                    # cap at 50 iterations
#   MODEL=gpt-5.4 EFFORT=high bash ralph/afk.sh
#
# Prereqs (one-time):
#   - copilot, jq, git on PATH.
#   - GitHub Copilot CLI signed in (run `copilot` once interactively, or
#     follow the auth flow at https://docs.github.com/copilot/github-copilot-in-the-cli).

set -euo pipefail

on_err() {
  local rc=$?
  local line=${BASH_LINENO[0]:-?}
  printf '\nralph/afk.sh aborted at line %s (exit %s): %s\n' \
    "$line" "$rc" "${BASH_COMMAND}" >&2
}
trap on_err ERR
trap 'echo "interrupted" >&2; exit 130' INT TERM

# Optional positional: max iterations (0 / omitted = unlimited).
MAX_ITERATIONS="${1:-0}"
if ! [[ "$MAX_ITERATIONS" =~ ^[0-9]+$ ]]; then
  echo "Usage: bash ralph/afk.sh [<iterations>]   (default: unlimited)" >&2
  exit 2
fi

MODEL="${MODEL:-claude-opus-4.7-1m-internal}"
EFFORT="${EFFORT:-xhigh}"

for cmd in copilot jq git; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: '$cmd' not found on PATH." >&2
    case "$cmd" in
      copilot) echo "  Install: npm install -g @github/copilot" >&2 ;;
      jq)      echo "  Install: brew install jq" >&2 ;;
      git)     echo "  Install: brew install git" >&2 ;;
    esac
    exit 1
  fi
done

if [ ! -f ralph/prompt.md ]; then
  echo "Error: ralph/prompt.md not found. Run this script from the repo root." >&2
  exit 1
fi

# jq filters tuned to the Copilot CLI --output-format json event shape.
# stream_text emits each delta's text + a newline at end of each assistant.message.
# final_result extracts the last terminal assistant.message content for the sentinel.
stream_text='if .type == "assistant.message_delta" then (.data.deltaContent // "") elif .type == "assistant.message" then "\n" else empty end'
final_result='[inputs | select(.type == "assistant.message") | .data.content] | last // empty'

i=0
while true; do
  i=$((i + 1))
  if [ "$MAX_ITERATIONS" -ne 0 ] && [ "$i" -gt "$MAX_ITERATIONS" ]; then
    echo "=== Reached iteration limit ($MAX_ITERATIONS) without <promise>NO MORE TASKS</promise>; exiting. ==="
    exit 0
  fi
  echo "=== Iteration $i ==="

  tmpfile="$(mktemp -t afk-iter.XXXXXX)"
  trap 'rm -f "$tmpfile"' EXIT

  commits="$(git log -n 5 --format='%H%n%ad%n%B---' --date=short 2>/dev/null || echo 'No commits found')"
  issues="$(find issues -type f -name '*.md' -not -path '*/done/*' -exec cat {} + 2>/dev/null || true)"
  [ -z "$issues" ] && issues='No issues found'
  prompt="$(cat ralph/prompt.md)"

  copilot \
      --model "$MODEL" \
      --effort "$EFFORT" \
      --yolo \
      --output-format json \
      -p "Previous commits: $commits Issues: $issues $prompt" \
    | grep --line-buffered '^{' \
    | tee "$tmpfile" \
    | jq --unbuffered -rj "$stream_text"
  printf '\n'

  result="$(jq -nr "$final_result" "$tmpfile" 2>/dev/null || true)"
  if [[ "$result" == *"<promise>NO MORE TASKS</promise>"* ]]; then
    echo "=== Iteration $i emitted <promise>NO MORE TASKS</promise> — exiting. ==="
    exit 0
  fi

  rm -f "$tmpfile"
  trap - EXIT
done
