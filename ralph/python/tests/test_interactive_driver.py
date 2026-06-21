"""Tests for ``ralph_afk.interactive.driver`` (issue #23 — peer orchestration).

Exercises the observer control model (ADR-0001) **without a TTY** by injecting a
fake app: the loop and app run as peers; Stop cancels the loop; natural
completion closes the app; a loop crash propagates. Issue #28 extends this with
**Detach** (swap the live sink back to the line printer, keep the loop running)
and the **scrollback-on-exit** run-end summary record.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any, Callable, Coroutine

import pytest
from rich.console import Console

from ralph_afk.config import RunConfig
from ralph_afk.interactive.driver import (
    InteractiveDriver,
    build_interactive_driver,
)
from ralph_afk.interactive.state import LiveRunState
from ralph_afk.sinks import SinkFanout


class _FakeApp:
    """Stand-in for ``RalphApp``: ``run_async`` blocks until ``exit``."""

    def __init__(
        self,
        state: LiveRunState,
        *,
        summary: object = None,
        log_source: object = None,
    ) -> None:
        self.state = state
        self.summary = summary
        self.log_source = log_source
        self.exited = False
        self._exit_event = asyncio.Event()

    async def run_async(self) -> None:
        await self._exit_event.wait()

    def exit(self, *args: object, **kwargs: object) -> None:
        self.exited = True
        self._exit_event.set()


class _SelfStoppingApp(_FakeApp):
    """Simulates the user pressing ``q`` the instant the app starts."""

    async def run_async(self) -> None:
        self.exit()


class _DetachingApp(_FakeApp):
    """Simulates the user pressing ``d`` (Detach) once the loop has emitted.

    Waits on ``gate`` so the loop can emit a few events into the live sink
    *before* the Detach, then sets :attr:`detach_requested` and exits — the
    cue the driver swaps the sink list to the line printer on.
    """

    def __init__(self, state: LiveRunState, *, gate: asyncio.Event, **kw: object) -> None:
        super().__init__(state, **kw)
        self.detach_requested = False
        self._gate = gate

    async def run_async(self) -> None:
        await self._gate.wait()
        self.detach_requested = True
        self.exit()


class _RecordingSink:
    """Captures every event/delta it is handed, in order (an ``EventSink``)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def render(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def stream_reasoning(self, delta: str) -> None:  # pragma: no cover - unused
        pass

    def stream_message(self, delta: str) -> None:  # pragma: no cover - unused
        pass


class _MarkerSummary:
    """Duck-typed ``RunSummary``: the driver only calls ``build_run_table()``."""

    RUN_END_MARKER = "RUN-END-TABLE-MARKER"

    def build_run_table(self) -> str:
        return self.RUN_END_MARKER


class _SecondInterrupt(BaseException):
    """Stand-in for the *second* ``Ctrl+C`` — a ``BaseException`` like the real
    ``KeyboardInterrupt`` it models, but without the runtime's special
    early-escape (so the test exercises the driver's re-raise without orphaning
    a task / logging a spurious 'exception never retrieved')."""


def _drive_returning(code: int) -> Callable[[], Coroutine[object, object, int]]:
    async def drive() -> int:
        return code

    return drive


def _drive_forever(
    tracker: dict[str, bool],
) -> Callable[[], Coroutine[object, object, int]]:
    async def drive() -> int:
        try:
            await asyncio.sleep(3600)
            return 0
        except asyncio.CancelledError:
            tracker["cancelled"] = True
            raise

    return drive


def _drive_raising(
    exc: BaseException,
) -> Callable[[], Coroutine[object, object, int]]:
    async def drive() -> int:
        raise exc

    return drive


def test_stop_cancels_loop_and_returns_zero() -> None:
    state = LiveRunState()
    tracker = {"cancelled": False}
    captured: list[_SelfStoppingApp] = []

    def factory(s: LiveRunState, **kwargs: object) -> _SelfStoppingApp:
        app = _SelfStoppingApp(s, **kwargs)
        captured.append(app)
        return app

    driver = InteractiveDriver(state, app_factory=factory)  # type: ignore[arg-type]
    exit_code = asyncio.run(driver.run(_drive_forever(tracker)))

    assert exit_code == 0
    assert tracker["cancelled"] is True
    assert state.status == "stopped"
    assert captured and captured[0].exited is True


def test_natural_completion_closes_app_and_returns_loop_code() -> None:
    state = LiveRunState()
    captured: list[_FakeApp] = []

    def factory(s: LiveRunState, **kwargs: object) -> _FakeApp:
        app = _FakeApp(s, **kwargs)
        captured.append(app)
        return app

    driver = InteractiveDriver(state, app_factory=factory)  # type: ignore[arg-type]
    exit_code = asyncio.run(driver.run(_drive_returning(1)))

    assert exit_code == 1
    assert captured and captured[0].exited is True
    # A natural completion is NOT a user Stop.
    assert state.status != "stopped"


def test_loop_crash_propagates_and_closes_app() -> None:
    state = LiveRunState()
    captured: list[_FakeApp] = []

    def factory(s: LiveRunState, **kwargs: object) -> _FakeApp:
        app = _FakeApp(s, **kwargs)
        captured.append(app)
        return app

    driver = InteractiveDriver(state, app_factory=factory)  # type: ignore[arg-type]
    boom = RuntimeError("loop exploded")

    with pytest.raises(RuntimeError, match="loop exploded"):
        asyncio.run(driver.run(_drive_raising(boom)))

    assert captured and captured[0].exited is True


def test_attach_panes_are_forwarded_to_the_app_factory() -> None:
    """The loop-owned Summary/Log sources reach the app (issue #26)."""
    state = LiveRunState()
    captured: list[_FakeApp] = []

    def factory(s: LiveRunState, **kwargs: object) -> _FakeApp:
        app = _FakeApp(s, **kwargs)
        captured.append(app)
        return app

    driver = InteractiveDriver(state, app_factory=factory)  # type: ignore[arg-type]
    sentinel_summary = object()

    def sentinel_log() -> str:
        return "captured-log"

    driver.attach_panes(summary=sentinel_summary, log_source=sentinel_log)  # type: ignore[arg-type]
    asyncio.run(driver.run(_drive_returning(0)))

    assert captured
    assert captured[0].summary is sentinel_summary
    assert captured[0].log_source is sentinel_log


def test_build_interactive_driver_seeds_state_from_config() -> None:
    cfg = RunConfig(
        model="claude-opus-4.8",
        reasoning_effort="max",
        max_nmt_strikes=5,
    )
    driver = build_interactive_driver(cfg)

    assert isinstance(driver, InteractiveDriver)
    assert isinstance(driver.state, LiveRunState)
    assert driver.state.model == "claude-opus-4.8"
    assert driver.state.reasoning_effort == "max"
    assert driver.state.max_strikes == 5


# ---------------------------------------------------------------------------
# Detach + scrollback-on-exit (issue #28)
# ---------------------------------------------------------------------------


def test_detach_swaps_sink_to_line_printer_and_run_continues() -> None:
    """``d`` swaps the live sink to the line printer; the loop runs to the end.

    The handoff must drop and duplicate no events: everything emitted *before*
    Detach reaches the live (TUI) sink only, everything *after* reaches the line
    printer only, and the loop returns its own exit code (it was never
    cancelled). The driver must NOT also print the run-end summary — on Detach
    the line printer owns the scrollback record.
    """
    state = LiveRunState()
    fanout = SinkFanout()
    live = _RecordingSink()
    line_printer = _RecordingSink()
    fanout.set_sinks([live])

    gate = asyncio.Event()
    captured: list[_DetachingApp] = []

    def factory(s: LiveRunState, **kwargs: object) -> _DetachingApp:
        app = _DetachingApp(s, gate=gate, **kwargs)
        captured.append(app)
        return app

    async def drive() -> int:
        # Two events while the TUI is still the sink.
        fanout.render({"type": "e1"})
        fanout.render({"type": "e2"})
        # Let the app Detach now, then spin until the driver has swapped the
        # sink list to the line printer (bounded so a regression can't hang).
        gate.set()
        for _ in range(10_000):
            if line_printer in fanout.sinks:
                break
            await asyncio.sleep(0)
        # Two more events — these must land on the line printer, not the TUI.
        fanout.render({"type": "e3"})
        fanout.render({"type": "e4"})
        return 0

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False)
    summary = _MarkerSummary()

    driver = InteractiveDriver(state, app_factory=factory)  # type: ignore[arg-type]
    driver.attach_panes(summary=summary, log_source=lambda: "")  # type: ignore[arg-type]
    driver.attach_detach(sinks=fanout, line_printer=line_printer, console=console)

    exit_code = asyncio.run(driver.run(drive))

    assert exit_code == 0
    assert captured and captured[0].detach_requested is True
    # No drop, no duplication: each event handled by exactly one sink-set.
    assert [e["type"] for e in live.events] == ["e1", "e2"]
    assert [e["type"] for e in line_printer.events] == ["e3", "e4"]
    # The sink list was swapped wholesale to the line printer.
    assert fanout.sinks == (line_printer,)
    # The run was NOT stopped — Detach leaves it running to its own outcome.
    assert state.status != "stopped"
    # On Detach the driver prints nothing; the line printer owns scrollback.
    assert buf.getvalue() == ""


def test_stop_prints_run_end_summary_to_scrollback() -> None:
    """On Stop the run-end summary table is written to normal scrollback."""
    state = LiveRunState()
    tracker = {"cancelled": False}

    def factory(s: LiveRunState, **kwargs: object) -> _SelfStoppingApp:
        return _SelfStoppingApp(s, **kwargs)

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False)
    summary = _MarkerSummary()

    driver = InteractiveDriver(state, app_factory=factory)  # type: ignore[arg-type]
    driver.attach_panes(summary=summary, log_source=lambda: "")  # type: ignore[arg-type]
    driver.attach_detach(
        sinks=SinkFanout([state]), line_printer=_RecordingSink(), console=console
    )

    exit_code = asyncio.run(driver.run(_drive_forever(tracker)))

    assert exit_code == 0
    assert state.status == "stopped"
    assert tracker["cancelled"] is True
    # The permanent textual record: the run-end summary table in scrollback.
    assert _MarkerSummary.RUN_END_MARKER in buf.getvalue()


def test_natural_completion_prints_run_end_summary_to_scrollback() -> None:
    """A run that ends on its own still leaves a scrollback record (no blank
    screen after the TUI tears down)."""
    state = LiveRunState()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False)
    summary = _MarkerSummary()

    def factory(s: LiveRunState, **kwargs: object) -> _FakeApp:
        return _FakeApp(s, **kwargs)

    driver = InteractiveDriver(state, app_factory=factory)  # type: ignore[arg-type]
    driver.attach_panes(summary=summary, log_source=lambda: "")  # type: ignore[arg-type]
    driver.attach_detach(
        sinks=SinkFanout([state]), line_printer=_RecordingSink(), console=console
    )

    exit_code = asyncio.run(driver.run(_drive_returning(1)))

    assert exit_code == 1
    assert _MarkerSummary.RUN_END_MARKER in buf.getvalue()


def test_base_interrupt_is_never_swallowed_forcing_immediate_exit() -> None:
    """The exit path must never swallow a ``BaseException``.

    A second ``Ctrl+C`` — a real ``KeyboardInterrupt`` once the TUI has restored
    the terminal — is a ``BaseException``; the runtime escapes the event loop
    with it and ``driver.run`` re-raises rather than catching, so the process
    exits immediately. (A real ``KeyboardInterrupt`` escapes even earlier, in
    ``asyncio``'s task step; the contract under test is simply that the driver
    never wraps the run in a ``BaseException``-swallowing ``except``.)
    """
    state = LiveRunState()

    def factory(s: LiveRunState, **kwargs: object) -> _FakeApp:
        return _FakeApp(s, **kwargs)

    driver = InteractiveDriver(state, app_factory=factory)  # type: ignore[arg-type]

    with pytest.raises(_SecondInterrupt):
        asyncio.run(driver.run(_drive_raising(_SecondInterrupt())))
