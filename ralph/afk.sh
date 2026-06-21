#!/bin/bash
# Convenience launcher for the Python AFK runner (ralph/python/).
# This is NOT a separate runner — it just invokes `ralph-afk` with a default
# model. See ralph/python/README.md and docs/runners.md for the full surface.
#
# On an interactive run (a TTY with the `[tui]` extra, or RALPH_INTERACTIVE=1)
# a one-time startup picker now lets you choose the model + reasoning effort
# live from `list_models()` before the loop starts. The MODEL / REASONING_EFFORT
# below are the **pre-selected default** (the picker's cursor lands on them);
# `--no-interactive` and non-TTY runs use them directly without the picker.
#
MODEL=claude-opus-4.8 REASONING_EFFORT=max uv run --project ralph/python ralph-afk