"""Pilot tests for ``ralph_afk.interactive.app`` — the tabless two-level live
interface (ADR-0003, issue #30).

Gated behind ``pytest.importorskip("textual")`` so the base (no ``[tui]`` extra)
install skips it. These cover the structural backbone:

* **Level 1 — the Dashboard** (the only top-level screen, no tab bar): the
  header band, the live **Queue**, and a compact **Summary** rollup band,
  stacked.
* **Level 2 — the per-issue Log**: ``enter`` on a Queue row opens that issue's
  **Log**; ``escape`` returns to the Dashboard with the Queue cursor preserved.

plus the unchanged exit model — **Stop** (``q`` / ``Ctrl+C``) and **Detach**
(``d``). The pure Queue / Log projections are unit-tested without a TTY in
``test_interactive_queue.py`` / ``test_interactive_transcript.py``.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from rich.text import Text  # noqa: E402
from textual.widgets import ContentSwitcher, DataTable, Static  # noqa: E402

from ralph_afk import events as events_module  # noqa: E402
from ralph_afk.interactive.app import RalphApp, _Dashboard, _LogView  # noqa: E402
from ralph_afk.interactive.state import LiveRunState  # noqa: E402


class _FakeSummary:
    """Duck-typed stand-in: the app only calls ``build_rollup_band()``."""

    def build_rollup_band(self) -> str:
        return "ROLLUP-BAND-MARKER"


def _make_state() -> LiveRunState:
    state = LiveRunState(
        run_id="01HEADER",
        model="claude-opus-4.8",
        reasoning_effort="max",
    )
    state.render(
        {"type": events_module.WRAPPER_RUN_START, "max_nmt_strikes": 3}
    )
    state.render({"type": events_module.WRAPPER_ITERATION_START, "iter": 3})
    state.render(
        {"type": events_module.WRAPPER_STRIKE, "strikes": 1, "max_strikes": 3}
    )
    return state


# ---------------------------------------------------------------------------
# Header + exit model
# ---------------------------------------------------------------------------


async def test_header_renders_run_identity_and_state() -> None:
    app = RalphApp(_make_state())
    async with app.run_test():
        header = str(app.query_one("#header", Static).renderable)
    assert "01HEADER" in header
    assert "claude-opus-4.8 (max)" in header
    assert "iter 3" in header
    assert "running" in header
    assert "strikes 1/3" in header


async def test_q_requests_stop_and_app_exits() -> None:
    app = RalphApp(_make_state())
    async with app.run_test() as pilot:
        assert app.stop_requested is False
        await pilot.press("q")
        await pilot.pause()
    # The binding fired and the app left its run loop.
    assert app.stop_requested is True
    assert app.is_running is False


async def test_d_requests_detach_and_app_exits() -> None:
    """#28: ``d`` tears the TUI down as a **Detach** (not a Stop).

    The app only *signals* the intent (``detach_requested``) and exits; the
    interactive driver — the app's peer — observes the flag and swaps the live
    sink back to the line printer so the run keeps printing to scrollback.
    """
    app = RalphApp(_make_state())
    async with app.run_test() as pilot:
        assert app.detach_requested is False
        await pilot.press("d")
        await pilot.pause()
    # The binding fired and the app left its run loop — as a Detach, not a Stop.
    assert app.detach_requested is True
    assert app.stop_requested is False
    assert app.is_running is False


# ---------------------------------------------------------------------------
# Level 1: the tabless Dashboard
# ---------------------------------------------------------------------------


def _state_with_queue() -> LiveRunState:
    """A run with one active issue (#26) and two still-queued (#27, #28)."""
    state = LiveRunState(run_id="01Q", model="m", reasoning_effort="x")
    state.render({"type": events_module.WRAPPER_RUN_START, "max_nmt_strikes": 3})
    state.render({"type": events_module.WRAPPER_ITERATION_START, "iter": 1})
    state.render(
        {
            "type": events_module.WRAPPER_AFK_READY_COLLECTED,
            "issues": [26, 27, 28],
        }
    )
    state.stream_message("<working issue=26>")
    return state


async def test_no_tab_bar_dashboard_is_the_only_top_level() -> None:
    """No tab bar / ContentSwitcher; the Dashboard stacks header + Queue + band."""
    app = RalphApp(_make_state(), refresh_interval=3600)
    async with app.run_test():
        # The retired tabbed structure is gone.
        assert len(app.query(ContentSwitcher)) == 0
        # The Dashboard is the only top-level screen and stacks the three bands.
        dashboard = app.query_one("#dashboard", _Dashboard)
        assert dashboard.display is True
        assert app.query_one("#header", Static) is not None
        assert app.query_one("#queue", DataTable) is not None
        assert app.query_one("#summary-band", Static) is not None
        # The per-issue Log (Level 2) exists but is hidden until a row is opened.
        assert app.query_one("#log", _LogView).display is False
        # The Queue holds focus from the start (no tab bar to traverse first).
        assert isinstance(app.focused, DataTable)


async def test_dashboard_queue_lists_issues_active_first_and_cursor_moves() -> None:
    app = RalphApp(_state_with_queue(), refresh_interval=3600)
    async with app.run_test() as pilot:
        table = app.query_one("#queue", DataTable)
        assert table.row_count == 3
        # Active-first ordering: #26 (active) leads, then queued #27, #28.
        assert [table.get_row_at(i)[0] for i in range(3)] == ["#26", "#27", "#28"]
        assert table.get_row_at(0)[1] == "active"
        assert table.get_row_at(1)[1] == "queued"

        # The Queue already has focus; arrow keys move its cursor.
        assert isinstance(app.focused, DataTable)
        assert table.cursor_row == 0
        await pilot.press("down")
        assert table.cursor_row == 1


async def test_summary_band_renders_rollup() -> None:
    app = RalphApp(
        _make_state(),
        summary=_FakeSummary(),  # type: ignore[arg-type]
        refresh_interval=3600,
    )
    async with app.run_test():
        band = app.query_one("#summary-band", Static)
        assert "ROLLUP-BAND-MARKER" in str(band.renderable)


# ---------------------------------------------------------------------------
# Level 2: Enter opens the per-issue Log, Esc returns to the Dashboard
# ---------------------------------------------------------------------------


def _state_with_active_transcript() -> LiveRunState:
    """The #26-active run plus a little reasoning / message / tool transcript."""
    state = _state_with_queue()  # ends with an open "<working issue=26>" message
    state.stream_reasoning("weighing the options\n")
    state.stream_message("Here is my plan\n")
    state.render(
        {
            "type": events_module.TOOL_CALL,
            "tool_name": "bash",
            "arguments": {"command": "pytest -q"},
        }
    )
    return state


def _dimmed_text(text: Text) -> str:
    """The substring(s) carrying the ``dim`` style — i.e. the reasoning lines."""
    return "".join(
        text.plain[span.start : span.end]
        for span in text.spans
        if span.style == "dim"
    )


async def test_enter_opens_active_issue_log_and_esc_returns() -> None:
    app = RalphApp(_state_with_active_transcript(), refresh_interval=3600)
    async with app.run_test() as pilot:
        # The Queue holds focus; Enter on the active row (#26) opens its Log.
        assert isinstance(app.focused, DataTable)
        await pilot.press("enter")
        await pilot.pause()

        # Level 2: the Log replaces the Dashboard (no tab bar, no switcher).
        dashboard = app.query_one("#dashboard", _Dashboard)
        log = app.query_one("#log", _LogView)
        assert log.display is True
        assert dashboard.display is False

        header = str(app.query_one("#log-header", Static).renderable)
        assert "#26" in header
        assert "status active" in header

        body = app.query_one("#log-body", Static).renderable
        assert isinstance(body, Text)
        # Interleaved transcript: reasoning + message + the tool-call event.
        assert "weighing the options" in body.plain
        assert "Here is my plan" in body.plain
        assert "» bash  command=pytest -q" in body.plain
        # Reasoning is dimmed; the assistant message is plain.
        dimmed = _dimmed_text(body)
        assert "weighing the options" in dimmed
        assert "Here is my plan" not in dimmed

        # Esc returns to the Dashboard (Level 1); the Queue regains focus.
        await pilot.press("escape")
        await pilot.pause()
        assert log.display is False
        assert dashboard.display is True
        assert isinstance(app.focused, DataTable)


async def test_enter_opens_non_active_issue_log_shows_details_only() -> None:
    app = RalphApp(_state_with_active_transcript(), refresh_interval=3600)
    async with app.run_test() as pilot:
        table = app.query_one("#queue", DataTable)
        await pilot.press("down")  # move to the queued row (#27)
        assert table.cursor_row == 1
        await pilot.press("enter")  # open the non-active issue's Log
        await pilot.pause()

        header = str(app.query_one("#log-header", Static).renderable)
        assert "#27" in header
        assert "status queued" in header

        body = app.query_one("#log-body", Static).renderable
        assert isinstance(body, Text)
        # Details only: no live transcript leaks into a non-active issue's Log.
        assert "weighing the options" not in body.plain
        assert "» bash" not in body.plain
        assert "details only" in body.plain

        # Esc returns to the Dashboard with the Queue cursor preserved on #27.
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#log", _LogView).display is False
        assert isinstance(app.focused, DataTable)
        assert table.cursor_row == 1


async def test_esc_on_dashboard_is_a_noop() -> None:
    """With no tab bar, Esc on the Dashboard does nothing (and never crashes)."""
    app = RalphApp(_state_with_queue(), refresh_interval=3600)
    async with app.run_test() as pilot:
        assert app.query_one("#dashboard", _Dashboard).display is True
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#dashboard", _Dashboard).display is True
        assert app.query_one("#log", _LogView).display is False
