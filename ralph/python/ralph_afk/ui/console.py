"""``ralph_afk.ui.console`` — singleton :class:`rich.console.Console` + STYLES.

The runner constructs **one** :class:`Console` per process. That one
instance is the single sink for every line of UI output; tests substitute
their own console at the :class:`Renderer` boundary.

Rich's :class:`Console` already auto-detects TTY-ness, colour support,
and width — we trust those defaults. When piped to ``tee`` / redirected
to a file, Rich degrades to plain text with no ANSI escapes; that's the
documented "captured output is plain" guarantee.
"""

from __future__ import annotations

from typing import Dict

from rich.console import Console

__all__ = ["get_console", "STYLES"]

# Named style tokens. Keys mirror the renderer/summary's vocabulary so
# changes to one of the literal style strings ripple through one place,
# not a grep across two modules.
STYLES: Dict[str, str] = {
    "reasoning": "dim italic",
    "tool": "cyan",
    "skill": "magenta bold",
    "panel_title": "bold",
    "panel_rule": "dim",
    "table_header": "bold cyan",
    "table_footer": "bold",
    "error": "bold red",
    "success": "bold green",
    "warning": "bold yellow",
    "meta": "dim",
}

# Lazy singleton. Built on first call to :func:`get_console` so tests
# that import the module without invoking the runner pay no Console
# construction cost.
_console: Console | None = None


def get_console() -> Console:
    """Return the process-wide :class:`Console` singleton.

    Built lazily on first call. Subsequent calls return the same instance,
    so any caller that grabs the singleton sees the same TTY-detection
    posture as every other caller.
    """
    global _console
    if _console is None:
        _console = Console()
    return _console
