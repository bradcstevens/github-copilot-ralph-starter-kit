#!/bin/bash
# Convenience launcher for the Python AFK runner (ralph/python/).
# This is NOT a separate runner — it just invokes `ralph-afk` with a default
# model. See ralph/python/README.md and docs/runners.md for the full surface.
#
# MODEL=claude-opus-4.7 REASONING_EFFORT=xhigh uv run --project ralph/python ralph-afk
MODEL=claude-opus-4.8 REASONING_EFFORT=max uv run --project ralph/python ralph-afk