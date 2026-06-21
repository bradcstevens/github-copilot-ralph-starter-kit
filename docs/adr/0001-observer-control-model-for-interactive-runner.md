# Observer control model for the interactive AFK runner

**Status:** accepted

## Context

The interactive TUI must let the user **Detach** (leave the live view while the run
keeps going unattended) as well as **Stop** (end the run). The obvious shape — run
the ralph loop as a child *worker* of the Textual app — ties the run's lifetime to
the app, so leaving the app would always kill the run and Detach becomes impossible.

## Decision

The ralph loop runs as a **peer asyncio task** that the Textual app merely
*observes*. The loop writes JSONL (always-on) and fans out events to a swappable list
of **sinks**; a Textual-agnostic `LiveRunState` is the interactive sink, the existing
line-printer Renderer is the non-interactive sink. **Detach** swaps the sink back to
the line-printer so the run continues after the app exits; **Stop** cancels the loop
task. The app and loop are launched as peers (e.g. `asyncio.gather`), not parent and
child.

## Consequences

- The entrypoint must launch the app and the loop as **peers**, inverting the
  current `asyncio.run(loop.run(...))` ownership only on the interactive path.
- `LiveRunState` must **not import Textual**, so it stays unit-testable without a TTY
  and honours the repo's import-guard convention.
- The non-interactive path (pipe / redirect / CI / `--no-interactive` / `[tui]` extra
  absent) keeps the Renderer + JSONL output **byte-for-byte unchanged**.
