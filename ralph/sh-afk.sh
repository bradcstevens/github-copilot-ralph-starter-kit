#!/usr/bin/env bash
#
# ralph/sh-afk.sh — autonomous ralph loop running entirely on the local host.
#
# Each iteration runs `copilot ...` against ralph/PROMPT.md plus every AFK-ready
# open issue from the configured issue source. Streams Copilot's text output to
# the terminal. After every iteration, the wrapper walks new commits for
# `Closes|Fixes|Resolves #N` references whose N was in the iteration's
# AFK-ready pool and auto-closes any issue the agent forgot to close itself.
#
# Peer variant: a Python implementation of the same wrapper contract lives at ralph/python/ — see ralph/python/README.md.
#
# Termination:
#   - Clean exit (0): the AFK-ready pool is empty at the start of an iteration.
#   - Aborted exit (1): MAX_NMT_STRIKES (default 3) consecutive iterations made
#     no progress (no commits, no wrapper closures) while AFK-ready work
#     remained — the agent is stuck and the human should investigate. The
#     legacy "<promise>NO MORE TASKS</promise>" sentinel is now informational
#     only: it counts toward strikes when the iteration produced no progress,
#     otherwise it is ignored (the next iteration's pool decides what's next).
#
# Usage:
#   bash ralph/sh-afk.sh                    # unlimited iterations
#   bash ralph/sh-afk.sh 50                 # cap at 50 iterations
#   MODEL=claude-opus-4.7-1m-internal bash ralph/sh-afk.sh
#   ISSUE_SOURCE=prds bash ralph/sh-afk.sh  # legacy local-markdown mode
#   MAX_NMT_STRIKES=5 bash ralph/sh-afk.sh  # tolerate more no-progress iters
#
# Env:
#   MODEL             Copilot CLI model id (default: claude-opus-4.7-xhigh)
#   ISSUE_SOURCE      'github' (default) or 'prds' (legacy local-markdown layout)
#   MAX_NMT_STRIKES   Consecutive no-progress iterations before aborting (default: 3)
#
# Prereqs (one-time):
#   - copilot, jq, git on PATH.
#   - For ISSUE_SOURCE=github (default): `gh` on PATH and signed in
#     (`gh auth login`); current working tree must resolve to a GitHub
#     repository via `gh repo view`.
#   - GitHub Copilot CLI signed in (run `copilot` once interactively, or
#     follow the auth flow at https://docs.github.com/copilot/github-copilot-in-the-cli).
#
# Skills:
#   This loop is designed to cooperate with the skills installed under
#   ~/.agents/skills (matt pocock's engineering + productivity skills plus
#   vercel-labs/find-skills). The companion prompt at ralph/PROMPT.md routes
#   work to /diagnose, /prototype, /tdd, /improve-codebase-architecture,
#   /zoom-out, and /grill-with-docs at the appropriate phase.

set -euo pipefail

on_err() {
  local rc=$?
  local line=${BASH_LINENO[0]:-?}
  printf '\nralph/sh-afk.sh aborted at line %s (exit %s): %s\n' \
    "$line" "$rc" "${BASH_COMMAND}" >&2
}
trap on_err ERR
trap 'echo "interrupted" >&2; exit 130' INT TERM

# Optional positional: max iterations (0 / omitted = unlimited).
MAX_ITERATIONS="${1:-0}"
if ! [[ "$MAX_ITERATIONS" =~ ^[0-9]+$ ]]; then
  echo "Usage: bash ralph/sh-afk.sh [<iterations>]   (default: unlimited)" >&2
  exit 2
fi

MODEL="${MODEL:-claude-opus-4.7-xhigh}"
ISSUE_SOURCE="${ISSUE_SOURCE:-github}"
case "$ISSUE_SOURCE" in
  github|prds) ;;
  *)
    echo "Error: ISSUE_SOURCE must be 'github' or 'prds' (got '$ISSUE_SOURCE')." >&2
    exit 2
    ;;
esac

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

if [ "$ISSUE_SOURCE" = "github" ]; then
  if ! command -v gh >/dev/null 2>&1; then
    echo "Error: 'gh' not found on PATH (required for ISSUE_SOURCE=github)." >&2
    echo "  Install: brew install gh   then run 'gh auth login'." >&2
    exit 1
  fi
  if ! gh auth status >/dev/null 2>&1; then
    echo "Error: gh is not authenticated. Run 'gh auth login' first." >&2
    exit 1
  fi
  if ! gh repo view --json name >/dev/null 2>&1; then
    echo "Error: current working tree does not resolve to a GitHub repository." >&2
    echo "  Run this script from inside a clone with a GitHub remote, or use ISSUE_SOURCE=prds." >&2
    exit 1
  fi
fi

if [ ! -f ralph/PROMPT.md ]; then
  echo "Error: ralph/PROMPT.md not found. Run this script from the repo root." >&2
  exit 1
fi

# Preflight: the AFK loop cannot safely run /setup-agent-skills itself (the skill
# is interactive — it asks the operator to pick an issue tracker, label vocabulary,
# and context-doc layout, then shows a draft for confirmation before writing).
# Under `copilot --yolo -p` the agent would have to invent those answers, baking
# the wrong defaults into docs/agents/*.md. So we refuse to start until a human
# has run /setup-agent-skills in an interactive copilot session and produced the
# config files. Detection signal: existence of docs/agents/issue-tracker.md
# (the first of the triplet /setup-agent-skills writes).
if [ ! -f docs/agents/issue-tracker.md ]; then
  echo "Error: docs/agents/issue-tracker.md not found." >&2
  echo "       This repo has not been configured with /setup-agent-skills yet." >&2
  echo "       In an interactive 'copilot' session from the repo root, run:" >&2
  echo "         > /setup-agent-skills" >&2
  echo "       Then re-run this script. See docs/customization.md for details." >&2
  exit 1
fi

# jq filters tuned to the Copilot CLI --output-format json event shape.
# stream_text emits each delta's text + a newline at end of each assistant.message.
# final_result extracts the last terminal assistant.message content for the sentinel.
stream_text='if .type == "assistant.message_delta" then (.data.deltaContent // "") elif .type == "assistant.message" then "\n" else empty end'
final_result='[inputs | select(.type == "assistant.message") | .data.content] | last // empty'

# collect_github_issues — render an Issues blob from GitHub issues.
# Filter: open + has `ready-for-agent` + body contains BOTH `## Parent` and
# `## Acceptance criteria`. The double-section discriminator skips PRD-style
# issues (which carry `ready-for-agent` for label-discipline reasons but
# don't have AC and aren't themselves units of work).
# Sets globals: $issues, $issue_count, $afk_ready_numbers (newline-separated
# whitelist of issue numbers the wrapper is allowed to auto-close this iter).
collect_github_issues() {
  local raw n_ready=0
  raw="$(gh issue list \
            --state open \
            --label ready-for-agent \
            --limit 100 \
            --json number,title \
            --jq '.[] | "\(.number)\t\(.title)"' 2>/dev/null || true)"
  if [ -z "$raw" ]; then
    issue_count=0
    issues='No issues found'
    afk_ready_numbers=''
    return 0
  fi

  local detail_jq='
    "=== Issue #\(.number): \(.title) [labels: \([.labels[].name] | join(", "))] ==="
    + "\n" + (.body // "")
    + (if (.comments | length) > 0 then
        "\n\n--- Recent comments (newest first, up to 5) ---\n"
        + ([.comments | sort_by(.createdAt) | reverse | .[0:5][]
            | "[\(.createdAt) @\(.author.login)] \(.body)"] | join("\n\n"))
      else "" end)'

  local out=""
  local numbers=""
  while IFS=$'\t' read -r num title; do
    [ -z "$num" ] && continue
    local body
    body="$(gh issue view "$num" \
              --json number,title,body,labels,comments \
              --jq "$detail_jq" 2>/dev/null || true)"
    if printf '%s' "$body" | grep -q '^## Parent' \
       && printf '%s' "$body" | grep -q '^## Acceptance criteria'; then
      out+="$body"$'\n\n'
      numbers+="$num"$'\n'
      n_ready=$((n_ready + 1))
      echo "    - #$num $title"
    else
      echo "    - (skipped) #$num $title (missing ## Parent or ## Acceptance criteria — likely a PRD)"
    fi
  done <<< "$raw"

  issue_count=$n_ready
  afk_ready_numbers="$numbers"
  if [ "$n_ready" -eq 0 ]; then
    issues='No issues found'
  else
    issues="$out"
  fi
}

# extract_close_refs — read commit messages between two SHAs and emit unique
# issue numbers referenced via GitHub closing keywords (close[sd]?, fix(es|ed)?,
# resolve[sd]?) immediately followed by whitespace then `#N`.
#
# The whitespace-then-`#` form intentionally does NOT match qualified refs like
# `Closes org/other-repo#42` (the `#` would not be directly preceded by the
# keyword+space) so we won't accidentally close issues in this repo with the
# same number as a referenced one elsewhere.
#
# Args:
#   $1 — exclusive start SHA (e.g., pre_sha)
#   $2 — inclusive end SHA (typically HEAD)
# Stdout: one issue number per line, sorted & deduped.
extract_close_refs() {
  local from="$1" to="$2"
  if [ "$from" = "$to" ]; then
    return 0
  fi
  git log --format='%B%n---COMMIT-BOUNDARY---' "$from..$to" 2>/dev/null \
    | grep -iEo '(close[sd]?|fix(es|ed)?|resolve[sd]?)[[:space:]]+#[0-9]+' \
    | grep -oE '#[0-9]+' \
    | tr -d '#' \
    | sort -un
}

# enforce_issue_closures — backstop the agent's `gh issue close` step.
#
# For every new commit since $pre_sha, find `Closes/Fixes/Resolves #N`
# references where N was in this iteration's AFK-ready pool. For each such N
# that is still OPEN on GitHub, close it from the wrapper with a wrap-up
# comment referencing the commit SHA(s) that touched it. Verify each closure.
#
# Args:
#   $1 — pre_sha (HEAD before the copilot call)
# Sets global: $auto_closed_count (number of issues the wrapper closed itself).
enforce_issue_closures() {
  local pre_sha="$1"
  auto_closed_count=0

  local head_sha
  head_sha="$(git rev-parse HEAD 2>/dev/null || echo "$pre_sha")"
  if [ "$head_sha" = "$pre_sha" ]; then
    return 0
  fi

  # All issue numbers referenced via closing keywords in the new commits.
  local refs
  refs="$(extract_close_refs "$pre_sha" "$head_sha" || true)"
  if [ -z "$refs" ]; then
    return 0
  fi

  # Restrict to numbers that were in the AFK-ready pool this iteration. This
  # prevents a stale or wrong-numbered `Closes #N` in a commit from acting on
  # an unrelated current-repo issue.
  local n state shas comment_body
  while IFS= read -r n; do
    [ -z "$n" ] && continue
    if ! printf '%s\n' "$afk_ready_numbers" | grep -qx "$n"; then
      echo "  (skip) commit references #$n which was not in this iteration's AFK-ready pool — not auto-closing."
      continue
    fi
    state="$(gh issue view "$n" --json state -q .state 2>/dev/null || echo '')"
    if [ "$state" = "CLOSED" ]; then
      continue
    fi
    if [ "$state" != "OPEN" ]; then
      echo "  warning: could not read state for issue #$n (got '$state'); skipping wrapper close."
      continue
    fi
    # Collect the new-commit SHAs whose message references this issue.
    shas="$(git log --format='%H' --grep="#$n" "$pre_sha..$head_sha" 2>/dev/null \
              | tr '\n' ' ' | sed 's/ *$//')"
    [ -z "$shas" ] && shas="$head_sha"
    comment_body="Implemented in $shas.

Closed by ralph/sh-afk.sh wrapper because the agent did not run \`gh issue close\` itself
this iteration (commit messages did reference \`Closes #$n\`).

If this closure looks wrong, reopen with \`gh issue reopen $n\` — the wrapper
will not re-close it without a new commit that references it."
    if gh issue close "$n" --comment "$comment_body" >/dev/null 2>&1; then
      # Verify the closure actually landed.
      state="$(gh issue view "$n" --json state -q .state 2>/dev/null || echo '')"
      if [ "$state" = "CLOSED" ]; then
        echo "  → wrapper closed #$n (referenced by $shas)"
        auto_closed_count=$((auto_closed_count + 1))
      else
        echo "  warning: gh issue close $n returned success but state is '$state' — verify manually."
      fi
    else
      echo "  warning: gh issue close $n failed; issue remains OPEN."
    fi
  done <<< "$refs"
}

# collect_prds_issues — legacy local-markdown mode.
# Reads NNN-*.md files under prds/<feature>/, skipping prd.md and done/.
# Sets globals: $issues, $issue_count.
collect_prds_issues() {
  local issue_paths
  issue_paths="$(find prds -mindepth 2 -type f -name '*.md' \
                   -not -name 'prd.md' -not -path '*/done/*' \
                   2>/dev/null | sort || true)"
  if [ -z "$issue_paths" ]; then
    issue_count=0
    issues='No issues found'
    return 0
  fi
  issue_count="$(printf '%s\n' "$issue_paths" | wc -l | tr -d ' ')"
  printf '%s\n' "$issue_paths" | sed 's/^/    - /'
  issues="$(printf '%s\n' "$issue_paths" | while IFS= read -r f; do
              printf '=== %s ===\n' "$f"
              cat "$f"
              printf '\n'
            done)"
}

# NMT-strikes counter — protects against the agent emitting
# <promise>NO MORE TASKS</promise> while AFK-ready work still exists and the
# iteration produced no progress (no commits, no wrapper closures). After
# MAX_NMT_STRIKES consecutive such iterations, abort non-zero so the human can
# investigate instead of leaving issues silently open.
MAX_NMT_STRIKES="${MAX_NMT_STRIKES:-3}"
nmt_strikes=0

# Issue-source-specific globals are populated by collect_*_issues.
# GitHub mode also populates afk_ready_numbers (whitelist for auto-close).
afk_ready_numbers=''

i=0
while true; do
  i=$((i + 1))
  if [ "$MAX_ITERATIONS" -ne 0 ] && [ "$i" -gt "$MAX_ITERATIONS" ]; then
    echo "=== Reached iteration limit ($MAX_ITERATIONS) without natural termination; exiting. ==="
    exit 0
  fi
  echo "=== Iteration $i (source=$ISSUE_SOURCE) ==="

  # Stale-worktree guard: a dirty tree would let the next iteration absorb
  # uncommitted work from a previous one. Refuse to start in that state.
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: working tree is dirty before iteration $i. Commit, stash, or reset before re-running." >&2
    git status --short >&2
    exit 1
  fi

  tmpfile="$(mktemp -t afk-iter.XXXXXX)"
  trap 'rm -f "$tmpfile"' EXIT

  commits="$(git log -n 5 --format='%H%n%ad%n%B---' --date=short 2>/dev/null || echo 'No commits found')"

  issue_count=0
  issues='No issues found'
  afk_ready_numbers=''
  echo "  Collecting AFK-ready issues..."
  if [ "$ISSUE_SOURCE" = "github" ]; then
    collect_github_issues
  else
    collect_prds_issues
  fi

  # Natural termination: no AFK-ready work in the queue means we're done.
  if [ "$issue_count" -eq 0 ]; then
    echo "=== No AFK-ready issues found — nothing to do; exiting. ==="
    rm -f "$tmpfile"
    trap - EXIT
    exit 0
  fi
  echo "  Passing $issue_count AFK-ready issue(s) to the agent."

  prompt="$(cat ralph/PROMPT.md)"

  pre_sha="$(git rev-parse HEAD 2>/dev/null || echo '')"

  # Run copilot; capture the pipe's exit code so we can distinguish a clean
  # agent exit from a copilot crash or jq parse failure. PIPESTATUS captures
  # the exit codes of all stages — element 0 is copilot itself.
  set +e
  copilot \
      --model "$MODEL" \
      --yolo \
      --output-format json \
      -p "Previous commits: $commits Issues: $issues $prompt" \
    | grep --line-buffered '^{' \
    | tee "$tmpfile" \
    | jq --unbuffered -rj "$stream_text"
  copilot_rc="${PIPESTATUS[0]}"
  set -e
  printf '\n'

  if [ "$copilot_rc" -ne 0 ]; then
    echo "  warning: copilot exited with code $copilot_rc (treating iteration as no-progress)."
  fi

  result="$(jq -nr "$final_result" "$tmpfile" 2>/dev/null || true)"
  saw_nmt=0
  if [[ "$result" == *"<promise>NO MORE TASKS</promise>"* ]]; then
    saw_nmt=1
  fi
  if [ -n "$result" ]; then
    tail_lines="$(printf '%s\n' "$result" | tail -n 12)"
    echo "--- Iteration $i final message (tail) ---"
    printf '%s\n' "$tail_lines" | sed 's/^/  /'
    echo "------------------------------------------"
  else
    echo "--- Iteration $i produced no terminal assistant.message ---"
  fi

  # Backstop the agent's `gh issue close` step (GitHub mode only). Only acts
  # on issues whose numbers were in this iteration's AFK-ready pool.
  auto_closed_count=0
  new_commit_count=0
  if [ "$ISSUE_SOURCE" = "github" ] && [ -n "$pre_sha" ]; then
    new_commit_count="$(git rev-list --count "$pre_sha..HEAD" 2>/dev/null || echo 0)"
    if [ "$new_commit_count" -gt 0 ]; then
      echo "  $new_commit_count new commit(s) since $pre_sha; checking for closures..."
      enforce_issue_closures "$pre_sha"
      # If commits exist but reference no closing keywords, surface that so
      # the human can spot agent noncompliance (work made it in, but the
      # issue convention wasn't followed — wrapper won't auto-close).
      if [ -z "$(extract_close_refs "$pre_sha" HEAD || true)" ]; then
        echo "  notice: new commits contain no GitHub closing keywords (close[sd]?/fix(es|ed)?/resolve[sd]?)."
        echo "          New commits:"
        git log --format='            %h %s' "$pre_sha..HEAD" | sed 's/^/  /'
      fi
    fi
  fi

  did_work=0
  if [ "$new_commit_count" -gt 0 ] || [ "$auto_closed_count" -gt 0 ]; then
    did_work=1
  fi

  # NMT/strikes decision tree.
  if [ "$did_work" -eq 1 ]; then
    nmt_strikes=0
    if [ "$saw_nmt" -eq 1 ]; then
      echo "  (agent emitted NO MORE TASKS but did work this iteration — ignoring sentinel; letting next iteration's collection decide.)"
    fi
  else
    nmt_strikes=$((nmt_strikes + 1))
    if [ "$saw_nmt" -eq 1 ]; then
      echo "  warning: agent emitted NO MORE TASKS without doing work (strike $nmt_strikes/$MAX_NMT_STRIKES)."
    else
      echo "  warning: iteration produced no commits and no closures (strike $nmt_strikes/$MAX_NMT_STRIKES)."
    fi
    if [ "$nmt_strikes" -ge "$MAX_NMT_STRIKES" ]; then
      echo "=== Aborted: $nmt_strikes consecutive iterations made no progress while $issue_count AFK-ready issue(s) remained open. ===" >&2
      echo "    Remaining AFK-ready issues:" >&2
      printf '%s' "$afk_ready_numbers" | sed 's/^/      #/' >&2
      rm -f "$tmpfile"
      trap - EXIT
      exit 1
    fi
  fi

  rm -f "$tmpfile"
  trap - EXIT
done
