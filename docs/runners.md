# Runners

> Everything you need to pick a runner, invoke it correctly, and understand what it'll do on each iteration.

The AFK loop is the autonomous phase ([Phase 6 in the workflow](workflow.md#phase-6--afk-loop-ralphsh-afksh-or-ralphpython)). Two interchangeable runners ship in this kit; both share the same wrapper contract.

## Pick a Runner: `ralph/sh-afk.sh` vs `ralph/python/`

Both runners implement the **same core wrapper contract** — same `ready-for-agent` filter, same `## Parent` + `## Acceptance criteria` discriminator, same `Closes/Fixes/Resolves #N` auto-close backstop, same env-var surface, same termination model. The Python runner additionally auto-stashes dirty leftovers after an iteration so a partial commit cannot make the next iteration abort or absorb unrelated tracked changes.

| Surface                          | [`ralph/sh-afk.sh`](../ralph/sh-afk.sh) (bash)                       | [`ralph/python/`](../ralph/python/) (Python SDK)                                                                                                                  |
| -------------------------------- | -------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Invocation                       | `bash ralph/sh-afk.sh`                                               | `uv run --project ralph/python ralph-afk`                                                                                                      |
| Positional arg (iteration cap)   | `bash ralph/sh-afk.sh 50` (0 / omitted = unlimited)                  | `uv run --project ralph/python ralph-afk 50` (0 / omitted = unlimited)                                                                         |
| `MODEL`                          | env var (default `claude-opus-4.7-xhigh`)                            | env var (same default)                                                                                                                         |
| `ISSUE_SOURCE`                   | env var; `github` (default) or `prds`                                | env var; same                                                                                                                                  |
| `MAX_NMT_STRIKES`                | env var (default `3`)                                                | env var (same default)                                                                                                                         |
| Exit `0` — clean                 | empty AFK-ready pool **or** iteration cap reached                    | empty AFK-ready pool **or** iteration cap reached                                                                                              |
| Exit `1` — aborted               | `MAX_NMT_STRIKES` tripped **or** stale worktree                      | `MAX_NMT_STRIKES` tripped, pre-iteration stale worktree, post-iteration stash failure, **or** preflight/setup failure (gh not authed, prompt file missing, malformed pricing, etc.) |
| Observability artefacts          | stdout/stderr only                                                   | `.ralph/logs/<iso>-<run_id>.jsonl` (replay JSONL) + `.ralph/runs/<iso>-<run_id>.json` (per-iteration rollup) + `.ralph/logs/<iso>-<run_id>.log` (stderr mirror) |
| Terminal UX                      | streamed text                                                        | Rich-rendered iteration `Panel`s, per-iteration token + live-catalog estimated-cost signal, run-end summary table                              |
| OpenTelemetry tracing            | n/a                                                                  | opt-in via `uv sync --project ralph/python --extra otel` + `RALPH_OTEL_ENABLED=1` (or `OTEL_EXPORTER_OTLP_ENDPOINT`)                            |
| Extra prerequisites              | `jq`                                                                 | Python ≥ 3.11, `uv` (or `pip ≥ 24`)                                                                                                            |

### When to use which

**Use [`ralph/sh-afk.sh`](../ralph/sh-afk.sh) when** you want the smallest possible dependency footprint — `gh`, `jq`, `git`, `copilot` and nothing else. The bash runner is stack-agnostic (it doesn't care that your project happens to be Python, Node, Rust, or something else) and is the right default for repos that deliberately chose a zero-Python, zero-npm toolchain.

**Use [`ralph/python/`](../ralph/python/) when** you want the richer terminal experience — frozen iteration `Panel`s showing tool calls / tokens / estimated cost, a JSONL replay log under `.ralph/logs/` you can grep through later, a run-summary JSON for post-hoc analysis, and (optionally) OpenTelemetry tracing of the full SDK + wrapper span tree. The extra dependencies (Python ≥ 3.11, `uv`) are one-time and stay scoped to `ralph/python/` — they do not touch your project's runtime.

The cost figure surfaced by the Python runner is an **estimate** based on provider list prices (not Copilot's premium-request billing). See [`ralph/python/README.md`](../ralph/python/README.md) for the full caveat.

## Invocation

The examples below use the bash runner; substitute `uv run --project ralph/python ralph-afk` for the Python variant. All env vars and positional args are identical across runners.

```bash
# Unlimited iterations, default model (claude-opus-4.7-xhigh).
bash ralph/sh-afk.sh

# Cap at 50 iterations.
bash ralph/sh-afk.sh 50

# Pick a different model.
MODEL=gpt-5.4 bash ralph/sh-afk.sh

# Tolerate more no-progress iterations before aborting (default: 3).
MAX_NMT_STRIKES=5 bash ralph/sh-afk.sh

# Use the legacy local-markdown mode (prds/<feature>/NNN-*.md).
ISSUE_SOURCE=prds bash ralph/sh-afk.sh
```

## Per-iteration flow

1. **Stale-worktree guard.** Refuses to start if the working tree is dirty (uncommitted changes from a previous iteration would otherwise get absorbed into the next one).
2. **Collect.** Pulls every open issue labeled `ready-for-agent` via `gh issue list`, then filters to those whose body contains both `## Parent` and `## Acceptance criteria` (skips bare PRDs).
3. **Run.** Feeds the filtered set, the last five commits, and [`ralph/PROMPT.md`](../ralph/PROMPT.md) to a fresh `copilot --yolo -p` invocation. Streams the agent's reasoning, tool calls, and tool output to the terminal. Captures Copilot's exit code via `PIPESTATUS` so a crash isn't mistaken for a clean turn.
4. **Auto-close backstop.** Walks new commits for GitHub closing keywords (`Closes/Fixes/Resolves #N`, case-insensitive) **restricted to issue numbers that were in this iteration's AFK-ready pool**. Any referenced issue that's still open gets closed by the wrapper with a comment pointing at the commit SHA(s). The pool whitelist prevents a stale or mis-numbered `Closes #N` from acting on an unrelated issue.
5. **Progress accounting.** An iteration "made progress" if it produced commits or wrapper closures. Otherwise it counts as a strike.

## Exit conditions

| Exit                  | Code | When                                                                                   |
| --------------------- | ---- | -------------------------------------------------------------------------------------- |
| Clean — queue empty   | `0`  | Start of an iteration finds the AFK-ready pool empty.                                  |
| Clean — iteration cap | `0`  | Optional positional arg `N` reached without natural termination.                       |
| **Aborted — stuck**   | `1`  | `MAX_NMT_STRIKES` (default 3) consecutive iterations made no progress.                 |
| **Aborted — preflight** | `1`  | A required precondition failed before the first iteration: missing `ralph/PROMPT.md`, missing [`docs/agents/issue-tracker.md`](customization.md#auto-bootstrap-behavior) (i.e. `/setup-agent-skills` hasn't run), `gh` not authed, dirty worktree, or (Python runner only) malformed pricing / stash failure. |

The legacy `<promise>NO MORE TASKS</promise>` sentinel is now **informational only**: the wrapper counts it as a strike if the iteration made no progress, otherwise ignores it. The next iteration's collection is always the source of truth on whether work remains.

## Commit-message contract

The auto-close backstop relies on commit messages following the GitHub closing-keyword convention:

- **Completion commits:** `Closes #N`, `Fixes #N`, or `Resolves #N` (case-insensitive forms — `close[sd]?`, `fix(es|ed)?`, `resolve[sd]?` — followed by whitespace then `#N`).
- **Partial-progress commits:** use `Refs #N` or `Progress on #N` so the wrapper does **not** auto-close.

[`ralph/PROMPT.md`](../ralph/PROMPT.md) instructs the agent in this contract and also lays out a **FINAL SEQUENCE** for issue closure (re-fetch state → `gh issue close` → verify state is `CLOSED` → retry once → fall through to wrapper backstop). If you customize `PROMPT.md`, keep that contract intact or the backstop will misfire — and update the regex in `extract_close_refs` inside [`ralph/sh-afk.sh`](../ralph/sh-afk.sh) so it still matches.

## Skill routing

[`ralph/PROMPT.md`](../ralph/PROMPT.md) directs each iteration's work to the right skill:

- `/diagnose` for hard bugs
- `/prototype` for sketchy areas
- `/tdd` for slice implementation
- `/improve-codebase-architecture` for refactors
- `/grill-with-docs` for plan stress-testing
- `/zoom-out` when the agent needs a higher-level map first

Skills the loop **will not invoke** (out of scope for unattended runs): `/setup-agent-skills` (one-shot setup, `disable-model-invocation: true`), `/triage`, `/to-prd`, `/to-issues` (they create or relabel issues — human-driven), `/handoff` (pointless inside a one-shot iteration), `/caveman` (reviewability beats compression while running unattended).

---

**Next:**
- [`docs/workflow.md`](workflow.md) — where the AFK loop fits in the broader Idea → QA workflow.
- [`docs/customization.md`](customization.md) — adjusting `AGENTS.md` feedback loops and `PROMPT.md` skill routing.
- [`ralph/python/README.md`](../ralph/python/README.md) — Python-specific bootstrap, observability artefacts, OpenTelemetry tracing.
- Back to [`README.md`](../README.md).
