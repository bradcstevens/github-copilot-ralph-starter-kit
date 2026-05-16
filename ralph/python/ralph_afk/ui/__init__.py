"""``ralph_afk.ui`` — Rich-based terminal rendering for the AFK runner.

Public surface re-exported here for caller convenience:

* :func:`get_console` — lazy singleton :class:`rich.console.Console`.
* :data:`STYLES` — named style-token dict consumed by the renderer +
  summary modules.
* :class:`Renderer` — event-driven dispatcher; ``render(event: dict) -> None``.
* :class:`RunSummary` — per-iteration counter accumulator + frozen
  iteration ``Panel`` + frozen run-end ``Table`` builder.
* :class:`IterationSnapshot` — per-iteration counter dataclass; carries
  a :meth:`IterationSnapshot.to_counters` conversion seam for the
  ``ralph_afk.persist`` writer.
"""

from __future__ import annotations

from .console import STYLES, get_console
from .renderer import Renderer
from .summary import IterationSnapshot, RunSummary, RunTotals

__all__ = [
    "STYLES",
    "get_console",
    "Renderer",
    "RunSummary",
    "RunTotals",
    "IterationSnapshot",
]
