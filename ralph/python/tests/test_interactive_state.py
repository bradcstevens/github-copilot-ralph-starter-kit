"""Tests for ``ralph_afk.interactive.state`` (issue #23 — live run model).

:class:`~ralph_afk.interactive.state.LiveRunState` is the Textual-agnostic
**interactive sink** the Textual app observes (ADR-0001). These tests pin:

* event folding — run-start / iteration-start / strike / run-end milestones;
* the live-ticking elapsed timer (injected monotonic clock) and its freeze on
  end / Stop;
* :func:`~ralph_afk.interactive.state.format_header` content;
* structural conformance to the :class:`ralph_afk.sinks.EventSink` protocol;
* the module's import-guard (stdlib + ``typing`` only — **no Textual**, no SDK);
* parity between the locally re-declared event-type literals and
  :mod:`ralph_afk.events`.
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

from ralph_afk import events as events_module
from ralph_afk import sinks as sinks_module
from ralph_afk.interactive import state as state_module
from ralph_afk.interactive.state import LiveRunState, format_header


class _FakeClock:
    """A controllable monotonic clock: ``advance`` then call to read."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, by: float) -> None:
        self.value += by


_FIXED_WALL = datetime(2026, 6, 21, 12, 0, 0)


def _make_state(**kwargs: object) -> LiveRunState:
    kwargs.setdefault("monotonic", _FakeClock())
    kwargs.setdefault("wall_clock", lambda: _FIXED_WALL)
    return LiveRunState(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Construction + event folding
# ---------------------------------------------------------------------------


def test_initial_state_is_starting_with_zero_elapsed() -> None:
    state = _make_state(model="claude-opus-4.8", reasoning_effort="max")
    assert state.status == "starting"
    assert state.iteration == 0
    assert state.strikes == 0
    assert state.elapsed_seconds() == 0.0
    assert state.started_wall is None


def test_run_start_marks_running_and_captures_max_strikes() -> None:
    state = _make_state()
    state.render(
        {
            "type": events_module.WRAPPER_RUN_START,
            "run_id": "01RUN",
            "iter": None,
            "max_nmt_strikes": 3,
        }
    )
    assert state.status == "running"
    assert state.max_strikes == 3
    assert state.run_id == "01RUN"
    assert state.started_wall == _FIXED_WALL


def test_iteration_start_updates_current_iteration() -> None:
    state = _make_state()
    state.render({"type": events_module.WRAPPER_ITERATION_START, "iter": 4})
    assert state.iteration == 4
    assert state.status == "running"


def test_strike_updates_count_and_max() -> None:
    state = _make_state()
    state.render(
        {
            "type": events_module.WRAPPER_STRIKE,
            "iter": 2,
            "strikes": 2,
            "max_strikes": 5,
        }
    )
    assert state.strikes == 2
    assert state.max_strikes == 5


def test_run_end_sets_terminal_status_from_outcome() -> None:
    state = _make_state()
    state.render({"type": events_module.WRAPPER_RUN_START, "max_nmt_strikes": 3})
    state.render(
        {"type": events_module.WRAPPER_RUN_END, "outcome": "empty_pool"}
    )
    assert state.status == "empty_pool"
    assert state.ended is True


def test_run_id_is_learned_from_first_event_when_not_preset() -> None:
    state = _make_state()
    assert state.run_id == ""
    state.render({"type": events_module.WRAPPER_ITERATION_START, "iter": 1, "run_id": "01ABC"})
    assert state.run_id == "01ABC"


def test_unknown_event_types_are_ignored() -> None:
    state = _make_state()
    state.render({"type": "tool.call", "run_id": "01XYZ", "name": "bash"})
    # run_id is still learned, but no milestone state changes.
    assert state.run_id == "01XYZ"
    assert state.status == "starting"
    assert state.iteration == 0


def test_malformed_numeric_payloads_keep_prior_values() -> None:
    state = _make_state()
    state.render({"type": events_module.WRAPPER_ITERATION_START, "iter": 7})
    state.render({"type": events_module.WRAPPER_ITERATION_START, "iter": "not-an-int"})
    assert state.iteration == 7


# ---------------------------------------------------------------------------
# Live-ticking + frozen elapsed
# ---------------------------------------------------------------------------


def test_elapsed_ticks_with_the_monotonic_clock() -> None:
    clock = _FakeClock()
    state = _make_state(monotonic=clock)
    state.render({"type": events_module.WRAPPER_RUN_START, "max_nmt_strikes": 3})
    assert state.elapsed_seconds() == 0.0
    clock.advance(83)
    assert state.elapsed_seconds() == 83.0


def test_elapsed_freezes_after_run_end() -> None:
    clock = _FakeClock()
    state = _make_state(monotonic=clock)
    state.render({"type": events_module.WRAPPER_RUN_START, "max_nmt_strikes": 3})
    clock.advance(10)
    state.render({"type": events_module.WRAPPER_RUN_END, "outcome": "iteration_cap"})
    clock.advance(1000)
    assert state.elapsed_seconds() == 10.0


def test_mark_stopped_sets_status_and_freezes_elapsed() -> None:
    clock = _FakeClock()
    state = _make_state(monotonic=clock)
    state.render({"type": events_module.WRAPPER_RUN_START, "max_nmt_strikes": 3})
    clock.advance(42)
    state.mark_stopped()
    assert state.status == "stopped"
    assert state.ended is True
    clock.advance(500)
    assert state.elapsed_seconds() == 42.0


def test_streaming_hooks_are_accepted_no_ops() -> None:
    state = _make_state()
    # Must not raise; #23 parks them, #27 fills the transcript pane.
    state.stream_reasoning("thinking…")
    state.stream_message("hello")
    assert state.iteration == 0


# ---------------------------------------------------------------------------
# format_header
# ---------------------------------------------------------------------------


def test_format_header_contains_all_fields() -> None:
    clock = _FakeClock()
    state = _make_state(
        run_id="01RUN", model="claude-opus-4.8", reasoning_effort="max", monotonic=clock
    )
    state.render({"type": events_module.WRAPPER_RUN_START, "max_nmt_strikes": 3})
    state.render({"type": events_module.WRAPPER_ITERATION_START, "iter": 2})
    state.render(
        {"type": events_module.WRAPPER_STRIKE, "strikes": 1, "max_strikes": 3}
    )
    clock.advance(3661)  # 1h 1m 1s
    header = format_header(state)
    assert "01RUN" in header
    assert "claude-opus-4.8 (max)" in header
    assert "start 12:00:00" in header
    assert "elapsed 1:01:01" in header
    assert "iter 2" in header
    assert "running" in header
    assert "strikes 1/3" in header


def test_format_header_without_model_says_default() -> None:
    state = _make_state(model=None)
    header = format_header(state)
    assert "model default" in header


def test_format_header_now_override_is_used_for_elapsed() -> None:
    clock = _FakeClock()
    state = _make_state(monotonic=clock)
    state.render({"type": events_module.WRAPPER_RUN_START, "max_nmt_strikes": 3})
    header = format_header(state, now=125.0)
    assert "elapsed 0:02:05" in header


# ---------------------------------------------------------------------------
# Protocol conformance + import guard
# ---------------------------------------------------------------------------


def test_live_run_state_satisfies_event_sink_protocol() -> None:
    state = _make_state()
    assert isinstance(state, sinks_module.EventSink)


def test_state_event_type_constants_match_events() -> None:
    """The locally re-declared literals must equal the events.py contract."""
    assert state_module._RUN_START == events_module.WRAPPER_RUN_START
    assert state_module._RUN_END == events_module.WRAPPER_RUN_END
    assert state_module._ITERATION_START == events_module.WRAPPER_ITERATION_START
    assert state_module._STRIKE == events_module.WRAPPER_STRIKE


def test_state_module_imports_are_constrained() -> None:
    """``state.py`` is deep + pure: stdlib + ``typing`` only — no Textual/SDK.

    The interactive sink must stay unit-testable without a TTY and must never
    import Textual (issue #23 acceptance criterion; ADR-0001 import-guard
    convention, mirroring ``ralph_afk.sinks``).
    """
    source = Path(state_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allow = {"__future__", "time", "datetime", "typing"}
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                seen.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "state.py must use absolute imports only"
            assert node.module is not None
            seen.add(node.module)
    leaked = seen - allow
    assert not leaked, f"state.py imports non-allowlisted modules: {leaked}"
    assert "textual" not in seen, "LiveRunState must not import Textual"
