"""``ralph_afk.interactive.app`` — the Textual app (the *observer*).

The walking-skeleton dashboard for issue #23: a single **live header band** that
reflects a :class:`~ralph_afk.interactive.state.LiveRunState`. The app *observes*
that state (ADR-0001) — it never owns the run — so a later slice (#28) can tear
the app down while the loop keeps going.

This module imports Textual, so it is imported **only on the interactive path**,
after :func:`ralph_afk.interactive.detect.resolve_interactive` has confirmed the
optional ``[tui]`` extra is importable. The pure model lives in
:mod:`ralph_afk.interactive.state`; everything here is presentation.

The richer tabs / queue / drill-in / pickers land in #24-#28; this slice proves
the architecture end-to-end with the header plus **Stop** (``q`` / ``Ctrl+C``).
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Static

from ralph_afk.interactive.state import LiveRunState, format_header

__all__ = ["RalphApp"]

#: How often the header repaints so the elapsed clock visibly ticks.
_DEFAULT_REFRESH_INTERVAL = 0.25


class RalphApp(App[None]):
    """A minimal Textual app rendering the live header band for one run.

    Observes a :class:`LiveRunState` fed via the issue #22 sink fan-out and
    repaints the header on a timer so the elapsed clock and iteration number
    update in real time. ``q`` / ``Ctrl+C`` request a **Stop**: the app exits,
    and the interactive driver (the app's peer) Stop-cancels the loop task.
    """

    TITLE = "ralph-afk"

    CSS = """
    #header {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text;
    }
    """

    BINDINGS = [
        Binding("q", "stop", "Stop"),
        # Ctrl+C is also a Stop. Marked priority so it is honoured regardless
        # of focus; hidden from the footer since it duplicates `q`.
        Binding("ctrl+c", "stop", "Stop", priority=True, show=False),
    ]

    def __init__(
        self,
        state: LiveRunState,
        *,
        refresh_interval: float = _DEFAULT_REFRESH_INTERVAL,
    ) -> None:
        super().__init__()
        self._state = state
        self._refresh_interval = refresh_interval
        #: Set when the user requests a Stop (``q`` / ``Ctrl+C``). Lets a Pilot
        #: test assert the binding fired, and documents the exit cause.
        self.stop_requested = False

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield Footer()

    def on_mount(self) -> None:
        # Paint once immediately so the header has content the instant the app
        # mounts, then tick so the elapsed clock advances.
        self._refresh_header()
        self.set_interval(self._refresh_interval, self._refresh_header)

    def _refresh_header(self) -> None:
        self.query_one("#header", Static).update(format_header(self._state))

    def action_stop(self) -> None:
        """Stop: tear the app down. The driver then cancels the loop task."""
        self.stop_requested = True
        self.exit()
