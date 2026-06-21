"""``ralph_afk.interactive`` — the opt-in, TTY-gated interactive runtime.

Introduced by issue #23 as an **additive** layer over the line-printer runner
(design decisions D0/D3, ADR-0001 — the *observer* control model). The ralph
loop runs as a **peer asyncio task**; a Textual app merely *observes* a
Textual-agnostic :class:`~ralph_afk.interactive.state.LiveRunState` that is fed
through the issue #22 sink fan-out. The app never *owns* the run, so a later
slice (#28) can **Detach** — tear the app down while the loop keeps going.

Import hygiene
--------------
This package ``__init__`` is deliberately import-light: it pulls in **no
Textual** and **no SDK**, so merely importing :mod:`ralph_afk.interactive` (e.g.
to reach the pure :mod:`~ralph_afk.interactive.state` /
:mod:`~ralph_afk.interactive.detect` modules) never costs a Textual import or
touches the screen.

* :mod:`ralph_afk.interactive.state` and :mod:`ralph_afk.interactive.detect` are
  **deep + pure** (stdlib + ``typing`` only) and unit-testable without a TTY.
* :mod:`ralph_afk.interactive.app` and :mod:`ralph_afk.interactive.driver` import
  Textual and are imported **lazily**, only once
  :func:`ralph_afk.interactive.detect.resolve_interactive` has confirmed the
  interactive path (an interactive TTY plus an importable ``[tui]`` extra).
"""

from __future__ import annotations

__all__: list[str] = []
