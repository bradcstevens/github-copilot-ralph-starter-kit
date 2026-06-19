# Runner

> Everything you need to invoke the AFK runner correctly and understand what it'll do on each iteration.

The AFK loop is the autonomous phase ([Phase 6 in the workflow](workflow.md#phase-6--afk-loop-ralphpython)). The kit ships a single runner — the Python AFK runner at [`ralph/python/`](../ralph/python/), built on the GitHub Copilot Python SDK. [`ralph/afk.sh`](../afk.sh) is an optional one-line convenience launcher that just invokes it with a default model; there is no separate shell runner.

## The runner: `ralph/python/`

The runner enforces the **wrapper contract** — `ready-for-agent` filter, `## Parent` + `## Acceptance criteria` discriminator, `Closes/Fixes/Resolves #N` auto-close backstop, env-var surface, and termination model. It also auto-stashes dirty leftovers after an iteration so a partial commit cannot make the next iteration abort or absorb unrelated tracked changes.

| Surface                          | [`ralph/python/`](../ralph/python/) (Python SDK)                                                                                                                  |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Invocation                       | `uv run --project ralph/python ralph-afk`                                                                                                      |
| Positional arg (iteration cap)   | `uv run --project ralph/python ralph-afk 50` (0 / omitted = unlimited)                                                                         |
| `MODEL`                          | env var (default `claude-opus-4.8`; use a bare base id — see [`ralph/python/README.md`](../ralph/python/README.md))                            |
| `ISSUE_SOURCE`                   | env var; `github` (default) or `prds`                                                                                                          |
| `INCLUDE_PRS`                    | env var; `1`/`true`/`yes` to also collect `ready-for-agent` PRs (GitHub mode). Overrides `docs/agents/issue-tracker.md`; default auto-detects from that file, off unless opted in |
| `MAX_NMT_STRIKES`                | env var (default `3`)                                                                                                                          |
| Exit `0` — clean                 | empty AFK-ready pool **or** iteration cap reached                                                                                              |
| Exit `1` — aborted               | `MAX_NMT_STRIKES` tripped, pre-iteration stale worktree, post-iteration stash failure, **or** preflight/setup failure (gh not authed, prompt file missing, malformed pricing, etc.) |
| Observability artefacts          | `.ralph/logs/<iso>-<run_id>.jsonl` (replay JSONL) + `.ralph/runs/<iso>-<run_id>.json` (per-iteration rollup) + `.ralph/logs/<iso>-<run_id>.log` (stderr mirror) |
| Terminal UX                      | Rich-rendered iteration `Panel`s, per-iteration token + live-catalog estimated-cost signal, run-end summary table                              |
| OpenTelemetry tracing            | opt-in via `uv sync --project ralph/python --extra otel` + `RALPH_OTEL_ENABLED=1` (or `OTEL_EXPORTER_OTLP_ENDPOINT`)                            |
| Prerequisites                    | `gh`, `git`, `copilot`, Python ≥ 3.11, `uv` (or `pip ≥ 24`)                                                                                    |

The runner gives you a richer terminal experience — frozen iteration `Panel`s showing tool calls / tokens / estimated cost, a JSONL replay log under `.ralph/logs/` you can grep through later, a run-summary JSON for post-hoc analysis, and (optionally) OpenTelemetry tracing of the full SDK + wrapper span tree. Its dependencies (Python ≥ 3.11, `uv`) are one-time and stay scoped to `ralph/python/` — they do not touch your project's runtime.

The cost figure surfaced by the runner is an **estimate** based on provider list prices (not Copilot's premium-request billing). See [`ralph/python/README.md`](../ralph/python/README.md) for the full caveat.

## Invocation

```bash
# Unlimited iterations, default model (claude-opus-4.8 at `max` reasoning effort).
uv run --project ralph/python ralph-afk

# Cap at 50 iterations.
uv run --project ralph/python ralph-afk 50

# Pick a different model.
MODEL=gpt-5.4 uv run --project ralph/python ralph-afk

# Tolerate more no-progress iterations before aborting (default: 3).
MAX_NMT_STRIKES=5 uv run --project ralph/python ralph-afk

# Use the legacy local-markdown mode (prds/<feature>/NNN-*.md).
ISSUE_SOURCE=prds uv run --project ralph/python ralph-afk

# Also advance ready-for-agent pull requests (GitHub mode only).
INCLUDE_PRS=1 uv run --project ralph/python ralph-afk
```

## Per-iteration flow

1. **Stale-worktree guard.** Refuses to start if the working tree is dirty (uncommitted changes from a previous iteration would otherwise get absorbed into the next one). When PR support is on, it also restores the base branch first — a prior PR iteration may have left HEAD on a PR branch from `gh pr checkout`.
2. **Collect.** Pulls every open issue labeled `ready-for-agent` via `gh issue list`, then filters to those whose body contains both `## Parent` and `## Acceptance criteria` (skips bare PRDs). When PR support is on, it also pulls every open PR labeled `ready-for-agent` (discriminated by an `## Agent Brief` in the PR body or a comment) and renders them as `=== PR #N: <title> [labels: ...] (branch: <head-branch>) ===` blocks.
3. **Run.** Feeds the filtered set, the last five commits, and [`ralph/PROMPT.md`](../ralph/PROMPT.md) to a fresh `copilot --yolo -p` invocation. Streams the agent's reasoning, tool calls, and tool output to the terminal. Captures Copilot's exit code via `PIPESTATUS` so a crash isn't mistaken for a clean turn.
4. **Auto-close backstop.** Walks new commits for GitHub closing keywords (`Closes/Fixes/Resolves #N`, case-insensitive) **restricted to issue numbers that were in this iteration's AFK-ready pool**. Any referenced issue that's still open gets closed by the wrapper with a comment pointing at the commit SHA(s). The pool whitelist prevents a stale or mis-numbered `Closes #N` from acting on an unrelated issue — and is restricted to issues, so a PR in the pool is never closed by the backstop.
5. **Progress accounting.** An iteration "made progress" if it produced commits or wrapper closures. A PR also counts as progress when its head SHA advances (the agent pushed to the PR branch) — detected by re-fetching each pool PR and comparing its live head SHA. The wrapper never merges or closes PRs; advancement is the only signal it records. Otherwise the iteration counts as a strike.

## Exit conditions

| Exit                  | Code | When                                                                                   |
| --------------------- | ---- | -------------------------------------------------------------------------------------- |
| Clean — queue empty   | `0`  | Start of an iteration finds the AFK-ready pool empty.                                  |
| Clean — iteration cap | `0`  | Optional positional arg `N` reached without natural termination.                       |
| **Aborted — stuck**   | `1`  | `MAX_NMT_STRIKES` (default 3) consecutive iterations made no progress.                 |
| **Aborted — preflight** | `1`  | A required precondition failed before the first iteration: missing `ralph/PROMPT.md`, missing [`docs/agents/issue-tracker.md`](customization.md#auto-bootstrap-behavior) (i.e. `/setup-agent-skills` hasn't run), `gh` not authed, dirty worktree, malformed pricing, or stash failure. |

The legacy `<promise>NO MORE TASKS</promise>` sentinel is now **informational only**: the wrapper counts it as a strike if the iteration made no progress, otherwise ignores it. The next iteration's collection is always the source of truth on whether work remains.

## Commit-message contract

The auto-close backstop relies on commit messages following the GitHub closing-keyword convention:

- **Completion commits:** `Closes #N`, `Fixes #N`, or `Resolves #N` (case-insensitive forms — `close[sd]?`, `fix(es|ed)?`, `resolve[sd]?` — followed by whitespace then `#N`).
- **Partial-progress commits:** use `Refs #N` or `Progress on #N` so the wrapper does **not** auto-close.

[`ralph/PROMPT.md`](../ralph/PROMPT.md) instructs the agent in this contract and also lays out a **FINAL SEQUENCE** for issue closure (re-fetch state → `gh issue close` → verify state is `CLOSED` → retry once → fall through to wrapper backstop). If you customize `PROMPT.md`, keep that contract intact or the backstop will misfire — and update the `CLOSE_KEYWORD_RE` regex used by `extract_close_refs` in [`ralph/python/ralph_afk/wrapper.py`](../ralph/python/ralph_afk/wrapper.py) so it still matches.

## Pull requests as a request surface

By default the loop only works **issues**. A repo can opt into also advancing **pull requests** — useful when `/triage` labels an external or in-flight PR `ready-for-agent` with an `## Agent Brief` for the loop to push forward.

- **Enabling.** Set `PRs as a request surface: yes` in [`docs/agents/issue-tracker.md`](customization.md#auto-bootstrap-behavior) (written by `/setup-agent-skills`), or override per-run with `INCLUDE_PRS=1`. `INCLUDE_PRS=0` force-disables even if the file says yes. With neither present, PR support is **off**.
- **Collection.** When on, each iteration also lists open `ready-for-agent` PRs and keeps those carrying an `## Agent Brief` (in the PR body or any comment) — the PR analogue of the issue body discriminator.
- **Per-iteration PR flow.** The agent runs `gh pr checkout <N>`, implements the brief on the PR branch, commits, and pushes. The wrapper registers progress when the PR's **head SHA advances**; at the start of the next iteration it restores the base branch. The agent is instructed never to merge or close the PR — a human merges in QA.
- **Safety.** The auto-close backstop is restricted to issue numbers, so a PR can never be `gh issue close`d by a `Closes #N` in a commit. PRs are advanced, never closed, by the wrapper.

## Skill routing

[`ralph/PROMPT.md`](../ralph/PROMPT.md) directs each iteration's work to the right **model-invocable** skill:

- `/diagnosing-bugs` for hard bugs
- `/prototype` for sketchy areas
- `/tdd` for slice implementation
- `/codebase-design` for refactors (finding deepening opportunities)

A few related skills are **human-only** (`disable-model-invocation: true`), so the loop can't invoke them; `PROMPT.md` inlines the part the agent needs instead of calling them — plan stress-testing against the domain docs (was `/grill-with-docs`), going up a layer to map an unfamiliar area (was `/zoom-out`), and the deep-module design vocabulary now covered by `/codebase-design` (was `/improve-codebase-architecture`).

Skills the loop **will not invoke** (out of scope for unattended runs): `/setup-agent-skills` (one-shot setup, `disable-model-invocation: true`), `/triage`, `/to-prd`, `/to-issues` (they create or relabel issues — human-driven), `/handoff` (pointless inside a one-shot iteration), `/caveman` (reviewability beats compression while running unattended).

---

**Next:**
- [`docs/workflow.md`](workflow.md) — where the AFK loop fits in the broader Idea → QA workflow.
- [`docs/customization.md`](customization.md) — adjusting `AGENTS.md` feedback loops and `PROMPT.md` skill routing.
- [`ralph/python/README.md`](../ralph/python/README.md) — Python-specific bootstrap, observability artefacts, OpenTelemetry tracing.
- Back to [`README.md`](../README.md).
