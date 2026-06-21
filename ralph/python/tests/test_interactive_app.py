"""Pilot smoke test for ``ralph_afk.interactive.app`` (issue #23).

Gated behind ``pytest.importorskip("textual")`` so the base (no ``[tui]`` extra)
install skips it. Asserts the live header renders the run's identity/state and
that **Stop** (``q``) tears the app down — the end-to-end proof that the Textual
observer is wired to the :class:`~ralph_afk.interactive.state.LiveRunState`.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from textual.widgets import Static  # noqa: E402

from ralph_afk import events as events_module  # noqa: E402
from ralph_afk.interactive.app import RalphApp  # noqa: E402
from ralph_afk.interactive.state import LiveRunState  # noqa: E402


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
