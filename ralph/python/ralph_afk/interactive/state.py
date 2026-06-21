"""``ralph_afk.interactive.state`` — the Textual-agnostic live run model.

:class:`LiveRunState` is the **interactive sink** in the issue #22 fan-out
(ADR-0001): the ralph loop dispatches every wrapper event — and every streaming
reasoning/message delta — to it, and the Textual app *observes* it to paint the
screen. The app reads; the loop writes; both run on the one asyncio event loop,
so no locking is needed.

This module is **deep and pure** — stdlib + ``typing`` only, **no Textual**, no
``rich``, no SDK — so the run model stays unit-testable without a TTY and
honours the repo's import-guard convention (ADR-0001; mirrors
:mod:`ralph_afk.sinks`). Enforced by
``tests/test_interactive_state.py::test_state_module_imports_are_constrained``.

Because importing :mod:`ralph_afk.events` would pull the Copilot SDK (it types
``map_sdk_event`` against the SDK's event package), the handful of event-type
string literals this module switches on are re-declared locally. Their values —
not the constant names — are the contract; a parity test
(``test_state_event_type_constants_match_events``) keeps them in lockstep with
:mod:`ralph_afk.events`.

For this slice (#23) the model carries exactly what the live **header band**
needs: run id, model + reasoning effort, the run-start wall clock, a
live-ticking elapsed timer, the current iteration number, the run status, and
the strike count ``x/N``. The streaming hooks are accepted but parked — the
interleaved transcript lands in #27, the per-issue ledger in #25.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Mapping

__all__ = ["LiveRunState", "format_header"]

# Event-type string literals this model reacts to. Re-declared locally (rather
# than imported from ``ralph_afk.events``, which would pull the SDK) and kept in
# lockstep by ``test_state_event_type_constants_match_events``.
_RUN_START = "wrapper.run.start"
_RUN_END = "wrapper.run.end"
_ITERATION_START = "wrapper.iteration.start"
_STRIKE = "wrapper.strike"

#: Status shown before the first ``wrapper.run.start`` is observed.
_STATUS_STARTING = "starting"
#: Status while the loop is driving iterations.
_STATUS_RUNNING = "running"
#: Terminal status when the user Stops (``q`` / ``Ctrl+C``) — distinct from the
#: loop's own natural outcomes (``empty_pool`` / ``iteration_cap`` / ...), which
#: arrive as the ``wrapper.run.end`` ``outcome``.
_STATUS_STOPPED = "stopped"


def _default_wall_clock() -> datetime:
    """Local wall-clock time, used for the human-readable run-start stamp."""
    return datetime.now()


class LiveRunState:
    """Mutable, Textual-agnostic snapshot of one run, fed via the sink fan-out.

    Satisfies the :class:`ralph_afk.sinks.EventSink` protocol structurally
    (``render`` / ``stream_reasoning`` / ``stream_message``). The loop calls
    those; the app reads the attributes (or :func:`format_header`) on a timer.

    The run-start wall clock and the monotonic elapsed baseline are captured
    when the first ``wrapper.run.start`` (or, defensively, the first
    ``wrapper.iteration.start``) is observed — not at construction — so the
    elapsed timer measures the run, not the time the app spent starting up.
    """

    def __init__(
        self,
        *,
        run_id: str = "",
        model: str | None = None,
        reasoning_effort: str | None = None,
        max_strikes: int = 0,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _default_wall_clock,
    ) -> None:
        self.run_id = run_id
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_strikes = max_strikes
        self._monotonic = monotonic
        self._wall_clock = wall_clock

        self.started_wall: datetime | None = None
        self._started_monotonic: float | None = None
        self._ended_monotonic: float | None = None
        self.iteration = 0
        self.status = _STATUS_STARTING
        self.strikes = 0
        self.ended = False

    # -- EventSink protocol -------------------------------------------------

    def render(self, event: Mapping[str, Any]) -> None:
        """Fold one wrapper event into the live model.

        Unknown event types are ignored: the header tracks only run-scope
        milestones (start / iteration / strike / end). The full per-issue
        ledger (#25) and transcript (#27) hang off later, richer reactions.
        """
        run_id = event.get("run_id")
        if run_id and not self.run_id:
            self.run_id = str(run_id)

        etype = event.get("type")
        if etype == _RUN_START:
            self._mark_started()
            self.status = _STATUS_RUNNING
            max_strikes = event.get("max_nmt_strikes")
            if max_strikes is not None:
                self.max_strikes = _coerce_int(max_strikes, self.max_strikes)
        elif etype == _ITERATION_START:
            self._mark_started()
            self.iteration = _coerce_int(event.get("iter"), self.iteration)
            self.status = _STATUS_RUNNING
        elif etype == _STRIKE:
            self.strikes = _coerce_int(event.get("strikes"), self.strikes)
            self.max_strikes = _coerce_int(
                event.get("max_strikes"), self.max_strikes
            )
        elif etype == _RUN_END:
            outcome = event.get("outcome")
            self.status = str(outcome) if outcome is not None else "ended"
            self._mark_ended()

    def stream_reasoning(self, delta: str) -> None:
        """Accept a reasoning delta. Parked until the #27 transcript pane."""

    def stream_message(self, delta: str) -> None:
        """Accept a message delta. Parked until the #27 transcript pane."""

    # -- driver-facing controls --------------------------------------------

    def mark_stopped(self) -> None:
        """Record a user **Stop** (``q`` / ``Ctrl+C``) as the terminal status.

        Called by the interactive driver when the user ends the run from the
        TUI, so the final header reads ``stopped`` rather than freezing on
        ``running`` — distinct from the loop's own natural ``wrapper.run.end``
        outcomes.
        """
        self.status = _STATUS_STOPPED
        self._mark_ended()

    # -- live timers --------------------------------------------------------

    def elapsed_seconds(self, now: float | None = None) -> float:
        """Seconds since the run started, frozen once the run has ended.

        Returns ``0.0`` before the run-start baseline is captured. While the
        run is live the elapsed time is measured against ``now`` (defaulting to
        the injected monotonic clock), so the header ticks; once ended it is
        pinned to the end baseline so the final frame is stable.
        """
        if self._started_monotonic is None:
            return 0.0
        end = self._ended_monotonic
        if end is None:
            end = now if now is not None else self._monotonic()
        return max(0.0, end - self._started_monotonic)

    # -- internals ----------------------------------------------------------

    def _mark_started(self) -> None:
        if self._started_monotonic is None:
            self._started_monotonic = self._monotonic()
            self.started_wall = self._wall_clock()

    def _mark_ended(self) -> None:
        self.ended = True
        if self._ended_monotonic is None and self._started_monotonic is not None:
            self._ended_monotonic = self._monotonic()


def _coerce_int(value: Any, fallback: int) -> int:
    """Best-effort int coercion: malformed payloads keep the prior value."""
    if value is None:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _format_elapsed(seconds: float) -> str:
    """Render elapsed seconds as ``H:MM:SS`` (hours never zero-padded)."""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


def format_header(state: LiveRunState, *, now: float | None = None) -> str:
    """Compose the single-line header band from a :class:`LiveRunState`.

    Pure and Textual-free so the header's *content* is unit-testable without a
    TTY; the app simply drops the returned string into a widget. The fields
    mirror issue #23's header contract: run id, model + reasoning effort,
    run-start clock, live-ticking elapsed, iteration number, status, strikes.
    """
    run_id = state.run_id or "—"

    if state.model:
        model = state.model
        if state.reasoning_effort:
            model = f"{model} ({state.reasoning_effort})"
    else:
        model = "default"

    started = state.started_wall.strftime("%H:%M:%S") if state.started_wall else "—"
    elapsed = _format_elapsed(state.elapsed_seconds(now))

    return (
        f"ralph-afk  run {run_id}"
        f"  •  model {model}"
        f"  •  start {started}  elapsed {elapsed}"
        f"  •  iter {state.iteration}"
        f"  •  {state.status}"
        f"  •  strikes {state.strikes}/{state.max_strikes}"
    )
