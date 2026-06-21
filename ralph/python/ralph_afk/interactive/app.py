"""``ralph_afk.interactive.app`` — the Textual app (the *observer*).

The tabbed dashboard for issue #26: a focusable **tab bar** (Dashboard / Log /
Summary) over a :class:`~textual.widgets.ContentSwitcher`, all observing a
:class:`~ralph_afk.interactive.state.LiveRunState` (ADR-0001). The app
*observes* — it never owns the run — so a later slice (#28) can tear the app
down while the loop keeps going.

Navigation is arrow + Enter + Esc driven (decision D5/D5c):

* the **tab bar** has focus by default; ``left`` / ``right`` move the *selection*
  and ``enter`` *activates* the selected tab (switching the visible pane and
  handing focus into it);
* ``escape`` returns focus to the tab bar from any pane.

Panes:

* **Dashboard** — the #23 header band plus the live **Queue** (the #25 ledger
  projected by :func:`~ralph_afk.interactive.state.queue_rows`): one cursor-
  selectable row per issue seen this run, ordered active-first, with live-ticking
  timers. ``up`` / ``down`` move the queue cursor (when the Dashboard is active).
* **Log** — today's raw line-by-line output in a scroll pane, captured from a
  buffer-backed line-printer :class:`~ralph_afk.ui.renderer.Renderer` registered
  as a second sink on the interactive path (the real stdout Renderer stays parked
  for #28's Detach, since Textual owns the terminal while the app runs).
* **Summary** — the live run-summary table, mirroring the run-end table but
  updating per iteration.

This module imports Textual, so it is imported **only on the interactive path**,
after :func:`ralph_afk.interactive.detect.resolve_interactive` has confirmed the
optional ``[tui]`` extra is importable. The pure model lives in
:mod:`ralph_afk.interactive.state`; everything here is presentation.

The per-issue drill-in / live transcript lands in #27.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from rich.console import RenderableType
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import ContentSwitcher, DataTable, Footer, Static

from ralph_afk.interactive.state import (
    LiveRunState,
    format_duration,
    format_header,
    queue_rows,
)

if TYPE_CHECKING:
    from ralph_afk.ui.summary import RunSummary

__all__ = ["RalphApp", "TabBar"]

#: How often the panes repaint so the elapsed/queue clocks visibly tick.
_DEFAULT_REFRESH_INTERVAL = 0.25

#: Tab labels and their matching ContentSwitcher pane ids, index-aligned.
_TAB_LABELS = ("Dashboard", "Log", "Summary")
_PANE_IDS = ("dashboard-pane", "log-pane", "summary-pane")


class TabBar(Static):
    """A focusable, arrow-navigated tab strip (decision D5c).

    ``left`` / ``right`` move the *selection* cursor; ``enter`` *activates* the
    selected tab and posts :class:`TabBar.Activated` so the app switches the
    visible pane and hands focus into it. Two indices are tracked because
    selection (what the cursor is on) and activation (what is shown) are
    distinct: arrowing previews without switching until Enter commits.
    """

    can_focus = True

    BINDINGS = [
        Binding("left", "prev", "Prev tab", show=False),
        Binding("right", "next", "Next tab", show=False),
        Binding("enter", "activate", "Open tab", show=False),
    ]

    class Activated(Message):
        """Posted when the user activates (Enter) the selected tab."""

        def __init__(self, index: int) -> None:
            self.index = index
            super().__init__()

    def __init__(self, labels: tuple[str, ...]) -> None:
        super().__init__()
        self._labels = labels
        #: The highlighted tab (moves with left/right).
        self.selected = 0
        #: The activated tab whose pane is visible (moves on Enter).
        self.active = 0

    def render(self) -> RenderableType:
        text = Text()
        for index, label in enumerate(self._labels):
            cell = f" {label} "
            if index == self.selected:
                text.append(cell, style="reverse")
            elif index == self.active:
                text.append(cell, style="bold underline")
            else:
                text.append(cell)
            text.append("  ")
        return text

    def action_prev(self) -> None:
        if self.selected > 0:
            self.selected -= 1
            self.refresh()

    def action_next(self) -> None:
        if self.selected < len(self._labels) - 1:
            self.selected += 1
            self.refresh()

    def action_activate(self) -> None:
        self.active = self.selected
        self.refresh()
        self.post_message(self.Activated(self.selected))


class _DashboardPane(Vertical):
    """The header band stacked above the live Queue list."""

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield DataTable(id="queue", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one("#queue", DataTable)
        table.add_column("Issue", key="issue")
        table.add_column("Status", key="status")
        table.add_column("Active", key="active")
        table.add_column("Waiting", key="waiting")


class _LogPane(VerticalScroll):
    """Scrollable pane showing the captured line-printer output."""

    def compose(self) -> ComposeResult:
        yield Static(id="log-body")


class _SummaryPane(VerticalScroll):
    """Scrollable pane showing the live run-summary table."""

    def compose(self) -> ComposeResult:
        yield Static(id="summary-body")


class RalphApp(App[None]):
    """A tabbed Textual app observing one run's :class:`LiveRunState`.

    The app reads the state (and the loop-owned ``summary`` / ``log_source``)
    on a timer; the loop writes via the #22 sink fan-out. ``q`` / ``Ctrl+C``
    request a **Stop**: the app exits and the interactive driver (the app's
    peer) Stop-cancels the loop task.
    """

    TITLE = "ralph-afk"

    CSS = """
    TabBar {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text;
    }
    #header {
        height: 1;
        padding: 0 1;
        background: $boost;
        color: $text;
    }
    ContentSwitcher {
        height: 1fr;
    }
    #queue {
        height: 1fr;
    }
    #log-body, #summary-body {
        width: 1fr;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "stop", "Stop"),
        # Ctrl+C is also a Stop. Marked priority so it is honoured regardless
        # of focus; hidden from the footer since it duplicates `q`.
        Binding("ctrl+c", "stop", "Stop", priority=True, show=False),
        Binding("d", "detach", "Detach"),
        Binding("escape", "focus_tabs", "Tabs"),
    ]

    def __init__(
        self,
        state: LiveRunState,
        *,
        summary: "RunSummary | None" = None,
        log_source: Callable[[], str] | None = None,
        refresh_interval: float = _DEFAULT_REFRESH_INTERVAL,
    ) -> None:
        super().__init__()
        self._state = state
        self._summary = summary
        self._log_source = log_source
        self._refresh_interval = refresh_interval
        #: Set when the user requests a Stop (``q`` / ``Ctrl+C``). Lets a Pilot
        #: test assert the binding fired, and documents the exit cause.
        self.stop_requested = False
        #: Set when the user requests a **Detach** (``d``): the TUI tears down
        #: but the run keeps going. The driver (the app's peer) reads this flag
        #: to swap the live sink back to the line printer instead of cancelling
        #: the loop (issue #28).
        self.detach_requested = False
        #: Row keys currently displayed in the queue, so a steady-state refresh
        #: only ticks timer cells (preserving the cursor) and rebuilds the table
        #: solely when the set/order of issues changes.
        self._displayed_refs: list[str] = []

    def compose(self) -> ComposeResult:
        yield TabBar(_TAB_LABELS)
        with ContentSwitcher(initial="dashboard-pane"):
            yield _DashboardPane(id="dashboard-pane")
            yield _LogPane(id="log-pane")
            yield _SummaryPane(id="summary-pane")
        yield Footer()

    def on_mount(self) -> None:
        # Paint once immediately so every pane has content the instant the app
        # mounts, then tick so the clocks advance.
        self._refresh()
        self.set_interval(self._refresh_interval, self._refresh)
        self.query_one(TabBar).focus()

    # -- tab navigation ----------------------------------------------------

    @on(TabBar.Activated)
    def _switch_tab(self, message: TabBar.Activated) -> None:
        pane_id = _PANE_IDS[message.index]
        self.query_one(ContentSwitcher).current = pane_id
        self._refresh()
        self._focus_pane(pane_id)

    def _focus_pane(self, pane_id: str) -> None:
        """Hand focus into the activated pane's primary widget."""
        if pane_id == "dashboard-pane":
            self.query_one("#queue", DataTable).focus()
        else:
            self.query_one(f"#{pane_id}", VerticalScroll).focus()

    def action_focus_tabs(self) -> None:
        """Esc: return focus to the tab bar from any pane."""
        self.query_one(TabBar).focus()

    def action_stop(self) -> None:
        """Stop: tear the app down. The driver then cancels the loop task."""
        self.stop_requested = True
        self.exit()

    def action_detach(self) -> None:
        """Detach: tear the app down but leave the run going (issue #28).

        Only signals intent; the interactive driver observes
        :attr:`detach_requested` once the app exits and swaps the live sink back
        to the line-printer :class:`~ralph_afk.ui.renderer.Renderer`, so the
        remainder of the run prints to normal scrollback instead of being
        cancelled.
        """
        self.detach_requested = True
        self.exit()

    # -- repaint -----------------------------------------------------------

    def _refresh(self) -> None:
        self.query_one("#header", Static).update(format_header(self._state))
        self._sync_queue()
        if self._log_source is not None:
            # Wrap in Text (no markup parsing) — captured output may contain
            # square brackets that would otherwise be read as Rich markup.
            self.query_one("#log-body", Static).update(Text(self._log_source()))
        if self._summary is not None:
            self.query_one("#summary-body", Static).update(
                self._summary.build_run_table()
            )

    def _sync_queue(self) -> None:
        table = self.query_one("#queue", DataTable)
        rows = queue_rows(self._state)
        new_refs = [str(row.ref) for row in rows]
        if new_refs != self._displayed_refs:
            saved = self._cursor_ref(table)
            table.clear()
            for row in rows:
                table.add_row(
                    row.label,
                    row.status,
                    format_duration(row.active_seconds),
                    format_duration(row.waiting_seconds),
                    key=str(row.ref),
                )
            self._displayed_refs = new_refs
            if saved is not None and saved in new_refs:
                table.move_cursor(row=table.get_row_index(saved))
        else:
            for row in rows:
                key = str(row.ref)
                table.update_cell(key, "status", row.status)
                table.update_cell(key, "active", format_duration(row.active_seconds))
                table.update_cell(
                    key, "waiting", format_duration(row.waiting_seconds)
                )

    @staticmethod
    def _cursor_ref(table: DataTable) -> str | None:
        """The row key under the cursor, or ``None`` if the table is empty."""
        if table.row_count == 0:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        value = cell_key.row_key.value
        return str(value) if value is not None else None
