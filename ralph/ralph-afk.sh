#!/bin/bash
set -eo pipefail

if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
  echo "Usage: $0 [prd-folder...]"
  echo ""
  echo "  prd-folder:  optional list of issues/<folder> names to scope work to."
  echo "               Pass one folder per PRD you want to focus on; omit to"
  echo "               include every open issue under issues/."
  echo ""
  echo "  Loops indefinitely until the assistant emits"
  echo "  <promise>NO MORE TASKS</promise>."
  echo ""
  echo "Examples:"
  echo "  $0                                # all open issues"
  echo "  $0 <prd-folder>                   # only that PRD's issues"
  echo "  $0 <prd-folder-a> <prd-folder-b>  # multiple PRDs"
  exit 0
fi

scope_args=("$@")

# Build the list of folders to read issues from. If scope folders are passed,
# only those subfolders under issues/ are searched; otherwise the entire
# issues/ tree is searched (excluding any done/ archives).
if [ ${#scope_args[@]} -gt 0 ]; then
  issue_paths=()
  for folder in "${scope_args[@]}"; do
    if [ ! -d "issues/$folder" ]; then
      echo "Error: issues/$folder does not exist" >&2
      exit 1
    fi
    issue_paths+=("issues/$folder")
  done
  echo "Scoping ralph to: ${issue_paths[*]}"
else
  if [ -d issues ]; then
    issue_paths=(issues)
  else
    issue_paths=()
  fi
fi

# jq filter to stream assistant text deltas live, with a blank-line separator after each completed message
stream_text='if .type == "assistant.message_delta" then (.data.deltaContent // "") | gsub("\n"; "\r\n") elif .type == "assistant.message" then "\r\n\n" else empty end'

# jq filter to extract the last completed assistant message (used to detect the completion sentinel)
final_result='[inputs | select(.type == "assistant.message") | .data.content] | last // empty'

i=0
while true; do
  i=$((i+1))
  tmpfile=$(mktemp)
  trap "rm -f $tmpfile" EXIT

  commits=$(git log -n 5 --format="%H%n%ad%n%B---" --date=short 2>/dev/null || echo "No commits found")
  if [ ${#issue_paths[@]} -gt 0 ]; then
    issues=$(find "${issue_paths[@]}" -type f -name '*.md' -not -path '*/done/*' -exec cat {} + 2>/dev/null || true)
  else
    issues=""
  fi
  [ -z "$issues" ] && issues="No issues found"
  prompt=$(cat ralph/prompt.md)

  docker sandbox run copilot . -- \
    --model claude-opus-4.7-xhigh \
    --yolo \
    --output-format json \
    -p "Previous commits: $commits Issues: $issues $prompt" \
  | grep --line-buffered '^{' \
  | tee "$tmpfile" \
  | jq --unbuffered -rj "$stream_text"

  result=$(jq -nr "$final_result" "$tmpfile")

  if [[ "$result" == *"<promise>NO MORE TASKS</promise>"* ]]; then
    echo "Ralph complete after $i iterations."
    exit 0
  fi
done