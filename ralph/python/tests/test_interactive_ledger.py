"""Tests for the per-run **ledger** in ``ralph_afk.interactive.state`` (issue #25).

These pin the data layer behind the live Queue (#26) and drill-in (#27): the
per-issue attribution and timing that :class:`LiveRunState` folds out of the
wrapper event stream (fanned in via issue #22) plus the agent's **working
marker** (tapped from the message stream).

Everything here is pure and **TTY/Textual-free** — the ledger reacts to plain
event dicts (``render``) and message-delta strings (``stream_message``), exactly
what the sink fan-out delivers, so the run model stays unit-testable without a
terminal (issue #25 acceptance criterion; ADR-0001 import-guard convention).

Status vocabulary under test (CONTEXT.md glossary):
queued / active / closed / advanced / no-progress / gone.
"""

from __future__ import annotations

from ralph_afk import events as events_module
from ralph_afk.interactive.state import (
    STATUS_ACTIVE,
    STATUS_ADVANCED,
    STATUS_CLOSED,
    STATUS_GONE,
    STATUS_NO_PROGRESS,
    STATUS_QUEUED,
    LiveRunState,
    format_header,
)


class _FakeClock:
    """A controllable monotonic clock: ``advance`` then call to read."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, by: float) -> None:
        self.value += by


def _make_state(clock: _FakeClock | None = None, **kwargs: object) -> LiveRunState:
    clock = clock or _FakeClock()
    kwargs.setdefault("wall_clock", lambda: None)
    return LiveRunState(monotonic=clock, **kwargs)  # type: ignore[arg-type]


def _ev(etype: str, **payload: object) -> dict[str, object]:
    return {"type": etype, **payload}


def _start_iteration(state: LiveRunState, *, iteration: int, issues: list[object]) -> None:
    """Drive the per-iteration opening events: iteration.start then the pool."""
    state.render(_ev(events_module.WRAPPER_ITERATION_START, iter=iteration))
    state.render(_ev(events_module.WRAPPER_AFK_READY_COLLECTED, issues=issues))


# ---------------------------------------------------------------------------
# Pool -> queued
# ---------------------------------------------------------------------------


def test_pool_collection_creates_queued_entries() -> None:
    clock = _FakeClock()
    state = _make_state(clock)
    state.render(_ev(events_module.WRAPPER_RUN_START, max_nmt_strikes=3))
    clock.advance(4)
    _start_iteration(state, iteration=1, issues=[12, 13])

    assert set(state.ledger) == {12, 13}
    entry = state.ledger[12]
    assert entry.status == STATUS_QUEUED
    assert entry.first_seen_at == 4.0
    assert entry.first_seen_iter == 1
    assert entry.started_at is None
    assert entry.waiting_duration is None
    assert entry.active_duration == 0.0
    assert state.active_ref is None


def test_first_seen_is_pinned_to_first_pool_appearance() -> None:
    clock = _FakeClock()
    state = _make_state(clock)
    _start_iteration(state, iteration=1, issues=[12])
    clock.advance(500)
    # Re-seen in a later pool: first_seen must not move.
    _start_iteration(state, iteration=2, issues=[12])
    assert state.ledger[12].first_seen_at == 0.0
    assert state.ledger[12].first_seen_iter == 1


# ---------------------------------------------------------------------------
# Working marker -> active
# ---------------------------------------------------------------------------


def test_working_marker_activates_issue_with_started_and_waiting() -> None:
    clock = _FakeClock()
    state = _make_state(clock)
    _start_iteration(state, iteration=1, issues=[12])
    clock.advance(5)
    state.stream_message("<working issue=12>")

    assert state.active_ref == 12
    entry = state.ledger[12]
    assert entry.status == STATUS_ACTIVE
    assert entry.started_at == 5.0
    assert entry.waiting_duration == 5.0


def test_active_timer_ticks_while_active() -> None:
    clock = _FakeClock()
    state = _make_state(clock)
    _start_iteration(state, iteration=1, issues=[12])
    clock.advance(5)
    state.stream_message("<working issue=12>")
    assert state.active_seconds() == 0.0
    clock.advance(15)
    assert state.active_seconds() == 15.0
    assert state.ledger[12].active_seconds(clock()) == 15.0


def test_marker_split_across_streaming_deltas_is_detected() -> None:
    state = _make_state()
    _start_iteration(state, iteration=1, issues=[12])
    state.stream_message("I'll take <working iss")
    assert state.active_ref is None
    state.stream_message("ue=12> now.")
    assert state.active_ref == 12


def test_marker_in_final_assistant_message_event_is_detected() -> None:
    state = _make_state()
    _start_iteration(state, iteration=1, issues=[7])
    state.render(
        _ev(
            events_module.ASSISTANT_MESSAGE,
            content="Picking <working issue=7> for this iteration.",
        )
    )
    assert state.active_ref == 7
    assert state.ledger[7].status == STATUS_ACTIVE


def test_marker_tolerates_hash_and_whitespace_and_case() -> None:
    state = _make_state()
    _start_iteration(state, iteration=1, issues=[42])
    state.stream_message("<Working  issue = #42 >")
    assert state.active_ref == 42


# ---------------------------------------------------------------------------
# Active time sums across revisits
# ---------------------------------------------------------------------------


def test_active_duration_sums_across_revisits() -> None:
    clock = _FakeClock()
    state = _make_state(clock)

    # Iteration 1: marker at t=10, advanced (commit, no close), ends at t=70.
    state.render(_ev(events_module.WRAPPER_ITERATION_START, iter=1))
    state.render(_ev(events_module.WRAPPER_AFK_READY_COLLECTED, issues=[12]))
    clock.advance(10)
    state.stream_message("<working issue=12>")
    state.render(_ev(events_module.WRAPPER_COMMIT_RECORDED, sha="abc", subject="x"))
    clock.advance(60)  # t=70
    state.render(_ev(events_module.WRAPPER_ITERATION_END, iter=1))
    assert state.ledger[12].status == STATUS_ADVANCED
    assert state.ledger[12].active_duration == 60.0
    assert state.active_ref is None

    # Iteration 2: revisits #12, marker at t=110, closed at t=160, ends t=165.
    clock.advance(30)  # t=100
    state.render(_ev(events_module.WRAPPER_ITERATION_START, iter=2))
    state.render(_ev(events_module.WRAPPER_AFK_READY_COLLECTED, issues=[12]))
    clock.advance(10)  # t=110
    state.stream_message("<working issue=12>")
    clock.advance(50)  # t=160
    state.render(_ev(events_module.WRAPPER_AUTO_CLOSE, issue=12, sha="def", shas=["def"]))
    state.render(_ev(events_module.WRAPPER_ITERATION_END, iter=2))

    entry = state.ledger[12]
    assert entry.status == STATUS_CLOSED
    assert entry.started_at == 10.0  # first marker, not the revisit
    assert entry.ended_at == 160.0
    assert entry.active_duration == 60.0 + 50.0


# ---------------------------------------------------------------------------
# Terminal-status reconciliation
# ---------------------------------------------------------------------------


def test_auto_close_without_marker_infers_active_and_closes() -> None:
    clock = _FakeClock()
    state = _make_state(clock)
    state.render(_ev(events_module.WRAPPER_ITERATION_START, iter=1))
    state.render(_ev(events_module.WRAPPER_AFK_READY_COLLECTED, issues=[12, 99]))
    state.render(_ev(events_module.WRAPPER_COMMIT_RECORDED, sha="abc", subject="x"))
    clock.advance(50)
    state.render(_ev(events_module.WRAPPER_AUTO_CLOSE, issue=12, sha="abc", shas=["abc"]))
    state.render(_ev(events_module.WRAPPER_ITERATION_END, iter=1))

    entry = state.ledger[12]
    assert entry.status == STATUS_CLOSED
    assert entry.ended_at == 50.0
    assert entry.started_at == 0.0  # iteration-start fallback
    assert entry.active_duration == 50.0
    # The other pool member was never worked -> still queued.
    assert state.ledger[99].status == STATUS_QUEUED


def test_pr_advanced_marks_issue_advanced() -> None:
    clock = _FakeClock()
    state = _make_state(clock)
    _start_iteration(state, iteration=1, issues=[99])
    clock.advance(5)
    state.stream_message("<working issue=99>")
    clock.advance(35)
    state.render(_ev(events_module.WRAPPER_PR_ADVANCED, pr=99, sha="abc", shas=["abc"]))
    state.render(_ev(events_module.WRAPPER_ITERATION_END, iter=1))

    entry = state.ledger[99]
    assert entry.status == STATUS_ADVANCED
    assert entry.ended_at == 40.0


def test_commits_without_close_mark_active_issue_advanced() -> None:
    clock = _FakeClock()
    state = _make_state(clock)
    _start_iteration(state, iteration=1, issues=[12])
    clock.advance(5)
    state.stream_message("<working issue=12>")
    state.render(_ev(events_module.WRAPPER_COMMIT_RECORDED, sha="abc", subject="x"))
    clock.advance(55)
    state.render(_ev(events_module.WRAPPER_ITERATION_END, iter=1))

    entry = state.ledger[12]
    assert entry.status == STATUS_ADVANCED
    assert entry.ended_at == 60.0
    assert entry.active_duration == 55.0


def test_strike_marks_active_issue_no_progress() -> None:
    clock = _FakeClock()
    state = _make_state(clock)
    _start_iteration(state, iteration=1, issues=[12])
    clock.advance(5)
    state.stream_message("<working issue=12>")
    clock.advance(25)
    state.render(
        _ev(events_module.WRAPPER_STRIKE, strikes=1, max_strikes=3, outcome="warn")
    )
    state.render(_ev(events_module.WRAPPER_ITERATION_END, iter=1))

    entry = state.ledger[12]
    assert entry.status == STATUS_NO_PROGRESS
    assert entry.ended_at is None  # no-progress is not a closure
    assert entry.active_duration == 25.0
    # The existing strike-count behaviour is preserved.
    assert state.strikes == 1
    assert state.max_strikes == 3


def test_single_pool_member_inference_at_iteration_end() -> None:
    clock = _FakeClock()
    state = _make_state(clock)
    state.render(_ev(events_module.WRAPPER_ITERATION_START, iter=1))
    state.render(_ev(events_module.WRAPPER_AFK_READY_COLLECTED, issues=[42]))
    state.render(_ev(events_module.WRAPPER_COMMIT_RECORDED, sha="abc", subject="x"))
    clock.advance(50)
    state.render(_ev(events_module.WRAPPER_ITERATION_END, iter=1))

    entry = state.ledger[42]
    assert entry.status == STATUS_ADVANCED
    assert entry.active_duration == 50.0


# ---------------------------------------------------------------------------
# gone
# ---------------------------------------------------------------------------


def test_queued_issue_that_leaves_pool_becomes_gone() -> None:
    state = _make_state()
    # Iter 1: work + close #12; #13 only queued.
    state.render(_ev(events_module.WRAPPER_ITERATION_START, iter=1))
    state.render(_ev(events_module.WRAPPER_AFK_READY_COLLECTED, issues=[12, 13]))
    state.stream_message("<working issue=12>")
    state.render(_ev(events_module.WRAPPER_AUTO_CLOSE, issue=12, sha="a", shas=["a"]))
    state.render(_ev(events_module.WRAPPER_ITERATION_END, iter=1))

    # Iter 2: #13 vanished, #14 new.
    state.render(_ev(events_module.WRAPPER_ITERATION_START, iter=2))
    state.render(_ev(events_module.WRAPPER_AFK_READY_COLLECTED, issues=[14]))

    assert state.ledger[13].status == STATUS_GONE
    assert state.ledger[12].status == STATUS_CLOSED  # resolved, not gone
    assert state.ledger[14].status == STATUS_QUEUED


def test_gone_issue_reappears_returns_to_queued() -> None:
    state = _make_state()
    _start_iteration(state, iteration=1, issues=[12, 13])
    state.render(_ev(events_module.WRAPPER_ITERATION_END, iter=1))
    _start_iteration(state, iteration=2, issues=[12])  # 13 vanishes
    assert state.ledger[13].status == STATUS_GONE
    state.render(_ev(events_module.WRAPPER_ITERATION_END, iter=2))
    _start_iteration(state, iteration=3, issues=[12, 13])  # 13 returns
    assert state.ledger[13].status == STATUS_QUEUED


# ---------------------------------------------------------------------------
# Stop freezes the active timer
# ---------------------------------------------------------------------------


def test_mark_stopped_freezes_active_timer() -> None:
    clock = _FakeClock()
    state = _make_state(clock)
    _start_iteration(state, iteration=1, issues=[12])
    clock.advance(5)
    state.stream_message("<working issue=12>")
    clock.advance(25)
    state.mark_stopped()
    assert state.status == "stopped"
    clock.advance(1000)
    assert state.active_seconds() == 25.0


# ---------------------------------------------------------------------------
# Header surfaces the active issue + its live timer
# ---------------------------------------------------------------------------


def test_header_shows_active_issue_and_live_timer() -> None:
    clock = _FakeClock()
    state = _make_state(clock)
    state.render(_ev(events_module.WRAPPER_RUN_START, max_nmt_strikes=3))
    _start_iteration(state, iteration=1, issues=[12])
    clock.advance(5)
    state.stream_message("<working issue=12>")
    clock.advance(60)
    header = format_header(state)
    assert "active #12" in header
    assert "0:01:00" in header  # the 60s active timer


def test_header_shows_placeholder_when_no_active_issue() -> None:
    state = _make_state()
    state.render(_ev(events_module.WRAPPER_RUN_START, max_nmt_strikes=3))
    header = format_header(state)
    assert "active —" in header


# ---------------------------------------------------------------------------
# Whole-iteration lifecycle through the real event sequence
# ---------------------------------------------------------------------------


def test_full_iteration_lifecycle_marker_then_close() -> None:
    clock = _FakeClock()
    state = _make_state(clock)
    state.render(_ev(events_module.WRAPPER_RUN_START, run_id="01RUN", max_nmt_strikes=3))
    state.render(_ev(events_module.WRAPPER_ITERATION_START, iter=1, run_id="01RUN"))
    state.render(_ev(events_module.WRAPPER_AFK_READY_COLLECTED, issues=[25, 26]))
    clock.advance(3)
    state.stream_message("Working now. <working issue=25>")
    assert state.active_ref == 25
    assert state.ledger[25].status == STATUS_ACTIVE
    clock.advance(40)
    state.render(_ev(events_module.WRAPPER_COMMIT_RECORDED, sha="s", subject="Closes #25"))
    state.render(_ev(events_module.WRAPPER_AUTO_CLOSE, issue=25, sha="s", shas=["s"]))
    state.render(_ev(events_module.WRAPPER_ITERATION_END, iter=1))
    state.render(_ev(events_module.WRAPPER_RUN_END, outcome="empty_pool"))

    closed = state.ledger[25]
    assert closed.status == STATUS_CLOSED
    assert closed.started_at == 3.0
    assert closed.waiting_duration == 3.0
    assert closed.active_duration == 40.0
    assert closed.ended_at == 43.0
    assert state.ledger[26].status == STATUS_QUEUED
    assert state.active_ref is None


# ---------------------------------------------------------------------------
# Runner Checkpoint (issue #32) — folded but never counted as agent work
# ---------------------------------------------------------------------------


def test_checkpoint_does_not_count_as_a_commit_or_advance() -> None:
    """A runner Checkpoint must not mark the active issue ``advanced``.

    Only agent commits / closures advance an issue. A Checkpoint-only
    iteration is still no-progress (a strike), exactly as if the agent had
    left a dirty tree without committing.
    """
    clock = _FakeClock()
    state = _make_state(clock)
    _start_iteration(state, iteration=1, issues=[12])
    clock.advance(5)
    state.stream_message("<working issue=12>")
    state.render(
        _ev(events_module.WRAPPER_CHECKPOINT_RECORDED, sha="cap123", issue=12)
    )
    clock.advance(25)
    state.render(_ev(events_module.WRAPPER_ITERATION_END, iter=1))

    entry = state.ledger[12]
    assert entry.status == STATUS_NO_PROGRESS
    assert entry.ended_at is None


def test_checkpoint_renders_as_distinct_transcript_line() -> None:
    state = _make_state()
    _start_iteration(state, iteration=1, issues=[12])
    state.stream_message("<working issue=12>")
    state.render(
        _ev(events_module.WRAPPER_CHECKPOINT_RECORDED, sha="cap1234567890", issue=12)
    )
    texts = [line.text for line in state.transcript()]
    assert any("checkpoint" in t.lower() for t in texts)
    assert any("cap1234567" in t for t in texts)
