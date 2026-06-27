# `ralph-afk` — the autonomous AFK loop runner

`ralph/python/` is the AFK loop runner for this kit, built on the
[GitHub Copilot Python SDK](https://github.com/github/copilot-sdk/tree/main/python).
It loads [`ralph/PROMPT.md`](../PROMPT.md) each iteration and enforces the
**wrapper contract** — a `ready-for-agent` filter, a `## What to build` +
`## Acceptance criteria` discriminator, a `Closes/Fixes/Resolves #N`
auto-close backstop, the `MODEL` / `ISSUE_SOURCE` / `MAX_NMT_STRIKES`
env-var surface, and a clean-exit-on-empty / abort-on-stuck termination
model.

The runner gives you a rich terminal UX — frozen iteration `Panel`s,
per-iteration token + estimated-cost signal, a JSONL replay log under
`.ralph/logs/`, a run-summary JSON under `.ralph/runs/`, and opt-in
OpenTelemetry tracing — after a one-time `uv sync` bootstrap. See the kit
root [`README.md`](../../README.md#prerequisites) for prerequisites and
[`docs/runners.md`](../../docs/runners.md) for the full runner reference.

[`ralph/afk.sh`](../afk.sh) is an optional one-line convenience launcher
that invokes this runner with a default model; there is no separate shell
runner.

---

## One-time bootstrap

```bash
# From the repo root: install the runner's dependencies.
uv sync --project ralph/python

# Optional: install the OpenTelemetry extra to enable opt-in tracing.
uv sync --project ralph/python --extra otel

# Optional: install the interactive TUI extra (live dashboard + Stop).
uv sync --project ralph/python --extra tui
```

**Requires:** Python **≥ 3.11** on PATH, and either
[`uv`](https://docs.astral.sh/uv/) (recommended) or `pip` **≥ 24** as
a fallback. The other prerequisites (`gh` signed in, `git`, `copilot`)
are listed in the kit root
[`README.md`](../../README.md#prerequisites).

The bootstrap is per-clone; subsequent invocations of `ralph-afk` use
the cached environment under `ralph/python/.venv/`.

---

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

# Deny a tool or skill at the SDK permission gate (repeatable, additive
# with RALPH_DENY_TOOLS / RALPH_DENY_SKILLS env vars).
uv run --project ralph/python ralph-afk --deny-tool bash --deny-skill caveman

# Use the legacy local-markdown mode (prds/<feature>/NNN-*.md).
ISSUE_SOURCE=prds uv run --project ralph/python ralph-afk
```

`uv run --project ralph/python ralph-afk --help` prints the full CLI
surface including verbosity flags (`-v`, `-vv`, `-vvv`) and
`--no-reasoning`.

---

## Exit codes

| Exit                  | Code | When                                                                                                                                                                                                                                                |
| --------------------- | ---- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Clean — queue empty   | `0`  | Start of an iteration finds the AFK-ready pool empty.                                                                                                                                                                                              |
| Clean — iteration cap | `0`  | Positional `<max-iterations>` reached without natural termination.                                                                                                                                                                                |
| Aborted — stuck       | `1`  | `MAX_NMT_STRIKES` (default 3) consecutive iterations made no progress.                                                                                                                                                                             |
| Aborted — preflight   | `1`  | Pre-loop setup failed: not inside a git repo, `gh` not authed or not on PATH, prompt file missing, malformed `RALPH_PRICING_FILE`, `CopilotClient` construction failed, writers bundle failed, or unknown `ISSUE_SOURCE`. Surfaces cleanly via stderr. |

---

## Env-var surface

| Env var                           | Default                        | Notes                                                                                                                                                                                                            |
| --------------------------------- | ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MODEL`                           | `claude-opus-4.8`              | Copilot CLI model id. Use a **bare base id** — model id and reasoning effort are separate axes (a suffixed id like `claude-opus-4.7-xhigh` is rejected as "not available"). A recognised trailing `-<effort>` segment is peeled off into `REASONING_EFFORT` for backward compatibility. On an interactive run this value is the startup picker's **pre-selected cursor** (see `RALPH_INTERACTIVE`); the model the run actually uses is whatever you confirm there.                                                                                                                                                                                            |
| `REASONING_EFFORT`                | `max` (kit default model only) | One of `low` / `medium` / `high` / `xhigh` / `max`. Precedence: this env var (validated; an invalid value aborts exit `1`) → a `-<effort>` suffix on `MODEL` → the kit default (`max`, applied only when `MODEL` is unset) → unset. A reasoning-incapable model (`claude-opus-4.5`, `claude-sonnet-4.5`, `claude-haiku-4.5`) forces this to **unset** (the CLI hard-rejects `session.create` otherwise); an unknown model warns and passes the value through to the CLI. On an interactive run this is the startup picker's **pre-selected effort** (the picker's stage 2 is auto-skipped for a reasoning-incapable model); the effort the run uses is whatever you confirm there. |
| `ISSUE_SOURCE`                    | `github`                       | `github` or `prds`. `prds` walks `prds/<feature>/NNN-*.md` files.                                                                                                                                                |
| `MAX_NMT_STRIKES`                 | `3`                            | Consecutive no-progress iterations before aborting exit `1`. Integer ≥ 1.                                                                                                                                        |
| `RALPH_DENY_TOOLS`                | _(empty)_                      | Comma-separated tool denylist. **Unioned** with `--deny-tool` CLI flags — CLI does NOT override env (security-positive divergence).                                                                              |
| `RALPH_DENY_SKILLS`               | _(empty)_                      | Comma-separated skill denylist for the `skill` meta-tool's `arguments.skill` field. **Unioned** with `--deny-skill` CLI flags.                                                                                   |
| `RALPH_PRICING_FILE`              | packaged `pricing.toml`        | Explicit `pricing.toml` path. A malformed file aborts the run with exit `1` (no silent fallback — operator intent is preserved).                                                                                 |
| `RALPH_OTEL_ENABLED`              | unset (disabled)               | Truthy (`1`, `true`, `yes`, `on`) enables OpenTelemetry tracing. Requires the `[otel]` extra. When disabled, `opentelemetry` is never imported — base install pays zero cost.                                    |
| `OTEL_EXPORTER_OTLP_ENDPOINT`     | unset                          | Presence (non-empty) also enables OTel tracing — matches the conventional OTel-ecosystem activation pattern.                                                                                                     |
| `RALPH_SEND_TIMEOUT_SECONDS`      | `7200` (2 h)                   | Per-iteration `send_and_wait` timeout. The SDK's default of `60` is far too short for AFK iterations that frequently run 30+ minutes.                                                                            |
| `RALPH_INTERACTIVE`               | unset (auto-detect from TTY)   | Truthy (`1`, `true`, `yes`, `on`) forces the interactive Textual dashboard; falsy (`0`, ...) forces today's line printer. Unset = auto-detect (interactive only on a TTY). Either way the interactive path additionally requires the `[tui]` extra; if it is missing, an explicit request warns and falls back to the line printer. **Before the loop starts, an interactive run opens a one-time, two-stage startup picker** (model, then reasoning effort): stage 1 lists models live from `list_models()` (id, display name, premium multiplier, context-window limit, reasoning support + default effort) with policy-disabled models greyed-out and non-selectable and the cursor pre-selected on `MODEL` (or the kit default); stage 2 lists the chosen model's supported efforts and is auto-skipped when it supports none. `Enter` confirms, `Esc` steps back / cancels, `q` / `Ctrl+C` cancels (keeping the env/default). The confirmed model + effort are baked into the run. On any `list_models()` failure (offline / unauthed / error) the picker falls back to the env/default values with a warning and the run still proceeds; `--no-interactive` and non-TTY runs skip the picker and use the env values directly. The dashboard is tab-navigated (`Dashboard` / `Log` / `Summary`): arrow keys move the tab-bar selection, `Enter` activates a tab, `Esc` returns focus to the tab bar, and `Up`/`Down` move the Dashboard's live-Queue cursor. Pressing `Enter` on a selected Queue row drills into that issue's detail view inside the Dashboard tab — the **active** issue shows a live, interleaved transcript (reasoning dimmed + assistant message + key events, a bounded tail; the full record stays in the `Log` tab and the JSONL replay log), a **non-active** issue shows details only — and `Esc` returns to the Queue. `d` **Detaches** (tears down the dashboard but lets the run continue, printing the remainder to normal scrollback); `q` / `Ctrl+C` **Stops** the run, writing the run-end summary table to scrollback (a second `Ctrl+C` forces an immediate exit). |

CLI flags (`-v` / `-vv` / `-vvv`, `--no-reasoning`, `--deny-tool`,
`--deny-skill`, `--interactive` / `--no-interactive`) are the runner's only
non-positional flags. See `ralph-afk --help` for the full list.

---

## Supported models

`MODEL` accepts any id the Copilot CLI exposes, but the runner ships a
capability matrix (`ralph_afk/config.py` → `MODEL_REASONING_EFFORTS`)
that gates `REASONING_EFFORT` per model. A model not in this table is
**warned** about once and passed through unchanged (the CLI is the final
authority). A model with an empty effort set is sent **no** reasoning
effort — the CLI hard-rejects `session.create` otherwise.

| Model id                    | Reasoning efforts                 |
| --------------------------- | --------------------------------- |
| `claude-opus-4.8` (default) | `low` `medium` `high` `xhigh` `max` |
| `claude-opus-4.7`           | `low` `medium` `high` `xhigh` `max` |
| `claude-opus-4.6`           | `low` `medium` `high` `max`       |
| `claude-opus-4.5`           | _(none — effort forced unset)_    |
| `claude-sonnet-4.6`         | `low` `medium` `high` `max`       |
| `claude-sonnet-4.5`         | _(none — effort forced unset)_    |
| `claude-haiku-4.5`          | _(none — effort forced unset)_    |
| `gpt-5.5`                   | `low` `medium` `high` `xhigh`     |
| `gpt-5.4`                   | `low` `medium` `high` `xhigh`     |
| `gpt-5.3-codex`             | `low` `medium` `high` `xhigh`     |
| `gpt-5.4-mini`              | `low` `medium` `high` `xhigh`     |
| `gpt-5-mini`                | `low` `medium` `high`             |
| `gemini-3.1-pro-preview`    | `low` `medium` `high`             |
| `gemini-3.5-flash`          | `low` `medium` `high`             |
| `mai-code-1-flash-internal` | `low` `medium` `high`             |

A subset of these carry list prices in the packaged `pricing.toml`
(`claude-opus-4.8`, `claude-opus-4.7`, `claude-sonnet-4.6`, `gpt-5.4`,
`gpt-5-mini`); any other model runs unpriced and renders `—` in the cost
column rather than a fabricated estimate.

---

## Observability artefacts

The Python runner writes three artefacts per invocation, all under the
**repo root**. Directories are created lazily on first write; a process
that exits before producing any output leaves no on-disk footprint. The
runner appends `.ralph/` to `.gitignore` once (idempotent) on first run
so the artefacts don't get accidentally committed.

| Artefact          | Path                                            | Format                                                                                                            |
| ----------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Event log         | `.ralph/logs/<iso>-<run_id>.jsonl`              | Append-only JSONL, one envelope per line, replay-grade. Flushed after every write so a crash leaves a partial-but-parseable file. |
| Run summary       | `.ralph/runs/<iso>-<run_id>.json`               | Per-iteration counter rollup (duration, tokens, estimated cost, tool / skill / commit / auto-closure / strike counts). Written on close. |
| Process diag.     | stderr **and** `.ralph/logs/<iso>-<run_id>.log` | Human-readable diagnostics. The stderr stream is primary; the `.log` file is the mirror.                          |

`<iso>` is a filesystem-safe `YYYY-MM-DDTHH-MM-SSZ` timestamp;
`<run_id>` is a 26-char Crockford-base32 ULID. The three files for a
single invocation share the same stem, so `ls .ralph/logs/` and
`ls .ralph/runs/` line up by-eye.

The run-summary JSON schema is documented at the top of
[`ralph_afk/persist.py`](ralph_afk/persist.py).

---

## Cost figure caveat

The Python runner surfaces an **estimated cost in USD per iteration** in
each iteration `Panel` and in the run-end summary table. This figure is
an **estimate based on provider list prices** — it is **not** the
amount GitHub Copilot will bill you. The Copilot CLI is billed on a
premium-request quota that the SDK does not expose; the figures the
renderer shows are useful for **cost-shape signal only** (which model
is heavier than which, how iteration cost trends over a run).

- The packaged pricing table at
  [`ralph_afk/pricing.toml`](ralph_afk/pricing.toml) is dated
  **2026-05-16**. Pricing drifts; update the file or override via the
  env var below.
- Override the packaged table at runtime via
  `RALPH_PRICING_FILE=/path/to/your.toml`. Schema and example entries
  are in the packaged file.
- The cost figure renders `—` (em dash) for any model not present in
  the active pricing table — **never** `$0.00`, so downstream consumers
  can distinguish "unknown" from "free".

---

## OpenTelemetry tracing (opt-in)

Install the extra and set either env var:

```bash
uv sync --project ralph/python --extra otel

# Activate by either of:
RALPH_OTEL_ENABLED=1 uv run --project ralph/python ralph-afk
# or
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 uv run --project ralph/python ralph-afk
```

When enabled, the runner emits the following span tree per invocation:

```
ralph_afk.run                          (root, one per ralph-afk invocation)
└─ ralph_afk.iteration                  (attrs: iter, issue, issues)
   ├─ ralph_afk.collect_issues
   ├─ ralph_afk.session                 (wraps the SDK session lifecycle)
   │  └─ <SDK-emitted spans>             (nest here via W3C context propagation)
   └─ ralph_afk.enforce_closures
```

When disabled (default), `opentelemetry` is never imported and the
runner pays **zero observability cost**.

---

## See also

- Kit root [`README.md`](../../README.md) — overview, prerequisites, and
  human-driven workflow phases (`/grill-me`, `/to-prd`, `/to-issues`,
  `/triage`).
- [`docs/runners.md`](../../docs/runners.md) — the full runner reference:
  per-iteration flow, exit conditions, commit-message contract, and skill
  routing.
- [`ralph/PROMPT.md`](../PROMPT.md) — the prompt loaded into every
  iteration.
- [`ralph/afk.sh`](../afk.sh) — optional one-line convenience launcher for
  this runner.
