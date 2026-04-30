#!/bin/bash

# Optional positional args: <prd-folder...> — scope work to one or more
# issues/<folder> subfolders. Pass any folder name created by the
# `prd-to-issues` skill (or any other subfolder under issues/). Omit to include
# every open issue under issues/.
#   bash ralph/ralph-once.sh                              # all open issues
#   bash ralph/ralph-once.sh <prd-folder>                 # only that PRD's issues
#   bash ralph/ralph-once.sh <prd-folder-a> <prd-folder-b>  # multiple PRDs
scope_args=("$@")

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

if [ ${#issue_paths[@]} -gt 0 ]; then
  issues=$(find "${issue_paths[@]}" -type f -name '*.md' -not -path '*/done/*' -exec cat {} + 2>/dev/null || true)
else
  issues=""
fi
[ -z "$issues" ] && issues="No issues found"
commits=$(git log -n 5 --format="%H%n%ad%n%B---" --date=short 2>/dev/null || echo "No commits found")
prompt=$(cat ralph/prompt.md)

copilot \
  --model claude-opus-4.7-xhigh \
  --yolo \
  -p "Previous commits: $commits Issues: $issues $prompt"