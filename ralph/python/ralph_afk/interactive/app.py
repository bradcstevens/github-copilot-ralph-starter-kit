"""``ralph_afk.interactive.app`` — the Textual app (the *observer*).

The **tabless two-level** live interface (ADR-0003), observing a
:class:`~ralph_afk.interactive.state.LiveRunState` (ADR-0001). The app
*observes* — it never owns the run — so the interactive driver (issue #28) can
tear the app down on a **Detach** while the loop keeps going.

Two levels, no tab bar:

* **Level 1 — the Dashboard** (the only top-level screen): the #23 header band,
  the live **Queue** (the #25 ledger projected by
  :func:`~ralph_afk.interactive.state.queue_rows`), and a compact **Summary**
  rollup band (run-level totals from
  :meth:`~ralph_afk.ui.summary.RunSummary.build_rollup_band`), stacked. The
  Queue holds focus; ``up`` / ``down`` move its cursor.
* **Level 2 — the per-issue Log**: ``enter`` on a Queue row opens that issue's
  **Log** (a full-region view that replaces the Dashboard); ``escape`` returns
  to the Dashboard with the Queue cursor preserved. For the *active* issue the
  Log shows the live, interleaved transcript (reasoning dimmed + assistant
  message + key structured events) tailing the state's bounded ring buffer; for
  a *non-active* issue it shows details only (the full record stays in the JSONL
  replay log).

This supersedes the #26 tabbed dashboard (a focusable tab bar over a
``ContentSwitcher`` with a Dashboard / Log / Summary split): the whole-run Log
tab and the Summary-as-a-separate-screen are retired. The full per-iteration
Summary table stays the run-end scrollback artefact (printed by the driver), not
an in-app screen. Per-issue Log accumulation (#34), timestamps (#37), and
sticky-with-release autoscroll (#38) arrive in later slices.

This module imports Textual, so it is imported **only on the interactive path**,
after :func:`ralph_afk.interactive.detect.resolve_interactive` has confirmed the
optional ``[tui]`` extra is importable. The pure model lives in
:mod:`ralph_afk.interactive.state`; everything here is presentation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Static

from ralph_afk.interactive.state import (
    LiveRunState,
    format_detail_header,
    format_duration,
    format_header,
    issue_detail,
    queue_rows,
)

if TYPE_CHECKING:
    from ralph_afk.ui.summary import RunSummary

__all__ = ["RalphApp"]

#: How often the panes repaint so the elapsed/queue clocks visibly tick.
_DEFAULT_REFRESH_INTERVAL = 0.25


class _Dashboard(Vertical):
    """Level 1: the header band, the live Queue, and the Summary rollup band."""

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield DataTable(id="queue", cursor_type="row", zebra_stripes=True)
        yield Static(id="summary-band")

    def on_mount(self) -> None:
        table = self.query_one("#queue", DataTable)
        table.add_column("Issue", key="issue")
        table.add_column("Status", key="status")
        table.add_column("Active", key="active")
        table.add_column("Waiting", key="waiting")


class _LogView(VerticalScroll):
    """Level 2: one issue's full-region **Log** (the per-issue drill-down).

    Opened by ``enter`` on a Queue row and closed by ``escape``; it replaces the
    Dashboard while showing (their ``display`` is toggled). For the *active*
    issue the body is the live, interleaved transcript (reasoning dimmed +
    assistant message + key structured events) tailing the state's bounded ring
    buffer; for a *non-active* issue it is details only (the full record stays in
    the JSONL replay log).
    """

    def compose(self) -> ComposeResult:
        yield Static(id="log-header")
        yield Static(id="log-body")


class RalphApp(App[None]):
    """A tabless, two-level Textual app observing one run's :class:`LiveRunState`.

    The app reads the state (and the loop-owned ``summary``) on a timer; the
    loop writes via the #22 sink fan-out. ``q`` / ``Ctrl+C`` request a **Stop**
    (the app exits and the interactive driver — the app's peer — Stop-cancels the
    loop task); ``d`` requests a **Detach** (the driver swaps the live sink back
    to the line printer and the run keeps going).
    """

    TITLE = "ralph-afk"

    CSS = """
    #dashboard {
        height: 1fr;
    }
    #header {
        height: 1;
        padding: 0 1;
        background: $boost;
        color: $text;
    }
    #queue {
        height: 1fr;
    }
    #summary-band {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text;
    }
    #log {
        height: 1fr;
        display: none;
    }
    #log-header {
        height: 1;
        padding: 0 1;
        background: $boost;
        color: $text;
    }
    #log-body {
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
        Binding("escape", "dashboard", "Back"),
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
        #: Retained for the driver's app-factory contract (issue #26). The
        #: whole-run Log tab it fed is retired (ADR-0003), so it is no longer
        #: rendered; the per-issue Log reads the state's transcript instead.
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
        #: The issue ref whose Log is open (``None`` while the Dashboard shows).
        #: Esc reads this: in a Log it returns to the Dashboard; on the
        #: Dashboard it is a no-op (there is no tab bar to return to).
        self._open_ref: str | None = None

    def compose(self) -> ComposeResult:
        yield _Dashboard(id="dashboard")
        yield _LogView(id="log")
        yield Footer()

    def on_mount(self) -> None:
        # Paint once immediately so every band has content the instant the app
        # mounts, then tick so the clocks advance. The Queue holds focus from
        # the start (no tab bar) so ``enter`` opens a Log straight away.
        self._refresh()
        self.set_interval(self._refresh_interval, self._refresh)
        self.query_one("#queue", DataTable).focus()

    # -- Stop / Detach -----------------------------------------------------

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

    def action_dashboard(self) -> None:
        """Esc: close an open Log (return to the Dashboard); else a no-op."""
        if self._open_ref is not None:
            self._close_log()

    # -- Level 2: per-issue Log -------------------------------------------

    @on(DataTable.RowSelected)
    def _open_from_queue(self, event: DataTable.RowSelected) -> None:
        """``enter`` on a Queue row opens that issue's Log (Level 2).

        Only the Dashboard's Queue triggers this; the row key is the issue ref
        (a string) :func:`issue_detail` normalises back to the ledger.
        """
        if event.data_table.id != "queue":
            return
        key = event.row_key.value
        if key is None:
            return
        self._open_log(str(key))

    def _open_log(self, ref: str) -> None:
        """Show ``ref``'s Log in place of the Dashboard."""
        self._open_ref = ref
        log = self.query_one("#log", _LogView)
        self._sync_log()
        self.query_one("#dashboard", _Dashboard).display = False
        log.display = True
        log.focus()

    def _close_log(self) -> None:
        """Return from the Log to the Dashboard (Esc), preserving the cursor."""
        self._open_ref = None
        self.query_one("#log", _LogView).display = False
        dashboard = self.query_one("#dashboard", _Dashboard)
        dashboard.display = True
        # The Queue's cursor row is retained across the display toggle (the
        # table was never cleared), so focusing it re-engages the same row.
        self.query_one("#queue", DataTable).focus()

    # -- repaint -----------------------------------------------------------

    def _refresh(self) -> None:
        self.query_one("#header", Static).update(format_header(self._state))
        self._sync_queue()
        self._sync_summary_band()
        self._sync_log()

    def _sync_summary_band(self) -> None:
        """Repaint the compact Summary rollup band from the loop-owned summary."""
        if self._summary is not None:
            self.query_one("#summary-band", Static).update(
                self._summary.build_rollup_band()
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

    def _sync_log(self) -> None:
        """Repaint the open Log (a no-op while the Dashboard is showing).

        For the active issue the body tails the state's bounded transcript ring
        buffer — reasoning lines dimmed, message + event lines plain — so it
        updates live as the model works. For a non-active issue it is details
        only; the full record stays in the JSONL replay log.
        """
        if self._open_ref is None:
            return
        detail = issue_detail(self._state, self._open_ref)
        self.query_one("#log-header", Static).update(format_detail_header(detail))
        body = Text()
        if detail.is_active:
            for line in self._state.transcript():
                body.append(line.text, style="dim" if line.dim else "")
                body.append("\n")
            if not body.plain:
                body.append("(waiting for the model's output…)", style="dim")
        else:
            body.append(
                f"{detail.status} — details only (no live stream). "
                "The full record is in the JSONL replay log.",
                style="dim",
            )
        self.query_one("#log-body", Static).update(body)

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
