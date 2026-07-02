"""End-to-end integration tests for Parallel mode (#61/#62, ADR-0008/0009).

Drives the opt-in Wave/Lane orchestrator through the public
:func:`ralph_afk.loop.run` seam with the SDK + git / gh / gate seams faked,
asserting the **observable effects** of concurrent isolated execution — one
worktree + branch per Lane created in a sibling directory, each session pinned
to its Lane's worktree via ``working_directory``, per-Lane commits landing on
Lane branches, and the worktrees torn down at the Wave barrier — not internal
call ordering.

The fakes here (unlike the serial ``test_iteration_end_to_end`` client) record
the per-session ``working_directory`` and route each Lane's simulated agent
commit to the *right* worktree's child :class:`~tests.fakes.FakeGitClient`, so
the test can prove per-Lane isolation. At the Wave barrier **Integration** (#62)
lands each green Lane's branch on base in ascending issue-number order, gates it
via the injected :class:`~ralph_afk.gate.GateRunner`, and closes the issue with
the serial closure semantics; a red gate skips the Lane and keeps its branch as
a breadcrumb (revert + auto-resolution is #63).

**Drain-everything (#67, ADR-0008).** A Parallel run interleaves Waves for the
``parallel-safe`` issues with serial Iterations for every other
``ready-for-agent`` issue, in one run, draining all eligible work with the
Strike machine ticking once per round (a Wave or a serial Iteration): see
:func:`test_parallel_run_drains_waves_then_serial_in_one_run`.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from copilot.generated.session_events import (
    AssistantUsageData,
    SessionEvent,
    SessionEventType,
)

from ralph_afk import gh as gh_module
from ralph_afk import git as git_module
from ralph_afk import loop as loop_module
from ralph_afk.config import RunConfig
from tests.fakes import FakeGateRunner, FakeGitClient, FakeGitHubClient


# ---------------------------------------------------------------------------
# Parallel-aware SDK fakes — record working_directory + route per-Lane commits.
# ---------------------------------------------------------------------------


class _ParallelFakeSession:
    """A per-Lane SDK session stub pinned to one worktree.

    ``send_and_wait`` models the Lane's agent committing *into its own
    worktree* — it looks the live child :class:`FakeGitClient` up on the parent
    fake by ``working_directory`` and advances that Lane's log — so per-Lane
    commit accounting sees exactly that Lane's commit and no other. A ``None``
    working directory (the serial-fallback path) commits on the main worktree.
    """

    def __init__(
        self,
        *,
        on_event: Callable[[SessionEvent], None] | None,
        working_directory: str | None,
        fake_git: FakeGitClient,
        scripted_events: list[SessionEvent],
        serial_closes: bool = False,
    ) -> None:
        self._on_event = on_event
        self._working_directory = working_directory
        self._fake_git = fake_git
        self._scripted_events = scripted_events
        self._serial_closes = serial_closes
        self.session_id = f"fake-session-{working_directory}"
        self.send_and_wait_calls: list[tuple[str, float]] = []

    async def send_and_wait(
        self, prompt: str, *, timeout: float = 60.0, **_extra: Any
    ) -> SessionEvent | None:
        self.send_and_wait_calls.append((prompt, timeout))
        if self._working_directory is not None:
            target = self._fake_git.worktree_client(
                Path(self._working_directory)
            )
            # The Lane's agent commit references its issue so the reused serial
            # closure path fires at Integration. The worktree dir is named
            # ``issue-<N>`` (see ``_lane_worktree_path``), so parse N from it.
            ref = Path(self._working_directory).name.removeprefix("issue-")
            body = f"Closes #{ref}"
        else:
            target = self._fake_git
            # The serial-fallback agent "picks one" issue and closes it. Parse
            # the pool from the rendered ``=== Issue #N:`` block HEADERS only
            # (never the Previous-commits block, which can carry a stale
            # ``Closes #N``), pick the lowest, and reference it so the reused
            # serial closure path fires — enough to drain a plain
            # ``ready-for-agent`` issue and let a multi-round run reach an empty
            # pool. Opt-in so the no-progress serial fakes keep their behaviour.
            body = ""
            if self._serial_closes:
                refs = [
                    int(n) for n in re.findall(r"=== Issue #(\d+):", prompt)
                ]
                if refs:
                    body = f"Closes #{min(refs)}"
        if target is not None:
            target.simulate_agent_commit(
                subject="feat(lane): implement issue",
                body=body,
            )
        last: SessionEvent | None = None
        for evt in self._scripted_events:
            if self._on_event is not None:
                self._on_event(evt)
            last = evt
        return last

    async def disconnect(self) -> None:
        return None


class _ParallelFakeClient:
    """One long-lived client hosting N concurrent Lane sessions (in-process).

    Records every ``create_session`` call's ``working_directory`` (the seam
    the loop pins each Lane to its worktree with) and hands back a
    :class:`_ParallelFakeSession` bound to it.
    """

    def __init__(
        self,
        *,
        fake_git: FakeGitClient,
        scripted_events: list[SessionEvent],
        serial_closes: bool = False,
    ) -> None:
        self._fake_git = fake_git
        self._scripted_events = scripted_events
        self._serial_closes = serial_closes
        self.create_calls: list[dict[str, Any]] = []
        self.created: list[_ParallelFakeSession] = []
        self.stop_call_count = 0

    async def create_session(
        self,
        *,
        on_permission_request: Any,
        on_event: Callable[[SessionEvent], None] | None = None,
        on_user_input_request: Any = None,
        model: str | None = None,
        working_directory: str | None = None,
        **_extra: Any,
    ) -> _ParallelFakeSession:
        self.create_calls.append(
            {"working_directory": working_directory, "model": model}
        )
        session = _ParallelFakeSession(
            on_event=on_event,
            working_directory=working_directory,
            fake_git=self._fake_git,
            scripted_events=self._scripted_events,
            serial_closes=self._serial_closes,
        )
        self.created.append(session)
        return session

    async def stop(self) -> None:
        self.stop_call_count += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _usage_event(model: str) -> SessionEvent:
    return SessionEvent(
        data=AssistantUsageData(
            input_tokens=100, output_tokens=50, model=model
        ),
        id=uuid4(),
        timestamp=datetime(2026, 5, 16, tzinfo=timezone.utc),
        type=SessionEventType.ASSISTANT_USAGE,
    )


_AFK_BODY = (
    "## Parent\n#49\n\n## What to build\nthing\n\n## Acceptance criteria\nbar"
)


def _make_issue(
    number: int, *, labels: list[str], body: str = _AFK_BODY
) -> gh_module.Issue:
    return gh_module.Issue(
        number=number,
        title=f"Test issue {number}",
        body=body,
        labels=labels,
        state="OPEN",
        url=f"https://github.com/x/y/issues/{number}",
        comments=(),
    )


def _logged_events(tmp_path: Path) -> list[dict[str, Any]]:
    logs_dir = tmp_path / ".ralph" / "logs"
    lines = (
        next(logs_dir.glob("*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    )
    return [json.loads(raw) for raw in lines]


def _run_id(tmp_path: Path) -> str:
    """Recover the run's ULID from the logged event envelopes.

    Every event carries ``run_id`` (see ``events._envelope``), so the Lane /
    integration branch names a test needs to assert on can be reconstructed via
    ``git.lane_branch_name`` / ``git.integration_branch_name`` without the test
    having to know the run id a priori.
    """
    return _logged_events(tmp_path)[0]["run_id"]


def _wire_repo(
    tmp_path: Path, *, merge_conflicts: Sequence[int] = ()
) -> FakeGitClient:
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text(
        "You are ralph. Implement the AFK-ready issues.\n", encoding="utf-8"
    )
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    return FakeGitClient(
        tmp_path,
        commits=[
            git_module.Commit(
                sha="0000000000000000000000000000000000000001",
                subject="prior commit",
                body="",
                date="2026-05-16",
            )
        ],
        dirty=False,
        untracked=False,
        merge_conflicts=merge_conflicts,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_parallel_run_dispatches_two_lane_wave(tmp_path, monkeypatch) -> None:
    """A two-Lane Wave, then Integration lands + closes both green Lanes.

    Both issues carry ``ready-for-agent`` + ``parallel-safe``, so with
    ``parallel=2`` the round is a Wave. Asserts (observable effects only): one
    worktree + Lane branch per issue created in a sibling directory, each
    session pinned to its Lane's worktree via ``working_directory``, each Lane's
    commit landing on its own branch, the worktrees torn down at the barrier,
    then Integration (#62) merging both green Lanes onto base and closing their
    issues in ascending issue-number order with the integrated branches deleted.
    """
    fake_git = _wire_repo(tmp_path)
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[
            _make_issue(42, labels=["ready-for-agent", "parallel-safe"]),
            _make_issue(43, labels=["ready-for-agent", "parallel-safe"]),
        ],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    fake_client = _ParallelFakeClient(
        fake_git=fake_git,
        scripted_events=[_usage_event("claude-opus-4.8-max")],
    )
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)

    fake_gate = FakeGateRunner()
    monkeypatch.setattr(
        loop_module, "_make_gate_runner", lambda: fake_gate
    )

    cfg = RunConfig(
        model="claude-opus-4.8-max",
        issue_source="github",
        parallel=2,
        max_iterations=1,
        max_nmt_strikes=3,
        verbosity=0,
        render_reasoning=False,
    )

    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 0, f"expected exit 0, got {exit_code}"

    # Two Lanes dispatched concurrently, one session each.
    assert len(fake_client.created) == 2
    assert fake_client.stop_call_count == 1

    # One worktree + branch per Lane, created in a sibling ``.worktrees`` dir
    # OUTSIDE the repo, one directory per issue.
    adds = fake_git.worktree_adds
    assert len(adds) == 2, f"expected two Lane worktrees, got {adds}"
    add_paths = {p for (p, _b, _base) in adds}
    branches = sorted(b for (_p, b, _base) in adds)
    bases = {base for (_p, _b, base) in adds}
    assert bases == {"main"}, "Lanes are cut from the base branch"
    for path in add_paths:
        assert path.parent.parent.name == f"{tmp_path.name}.worktrees"
        assert tmp_path not in path.parents, "worktrees live OUTSIDE the repo"
    # Deterministic ``copiloop/<run_id>/issue-<N>`` branch names, one run_id.
    assert branches[0].startswith("copiloop/")
    assert branches[0].endswith("/issue-42")
    assert branches[1].endswith("/issue-43")
    run_segs = {b.split("/issue-")[0] for b in branches}
    assert len(run_segs) == 1, "all Lanes share one run_id branch prefix"

    # Each session is pinned to its Lane's worktree via working_directory,
    # and the set of pinned dirs equals the set of created worktrees.
    pinned = {c["working_directory"] for c in fake_client.create_calls}
    assert None not in pinned, "every Lane session is worktree-pinned"
    assert {Path(p) for p in pinned} == add_paths

    # Each Lane's commit advanced its OWN branch: two commit.recorded events.
    events = _logged_events(tmp_path)
    commit_events = [e for e in events if e["type"] == "wrapper.commit.recorded"]
    assert len(commit_events) == 2, (
        f"expected one commit per Lane, got {len(commit_events)}"
    )

    # Worktrees torn down at the Wave barrier (before Integration lands the
    # branches), keeping the branches as breadcrumbs for the merge.
    assert len(fake_git.worktree_removes) == 2
    assert set(fake_git.worktree_removes) == add_paths
    assert fake_git.active_worktrees == []

    # Integration (#62) landed both green Lanes on base — base advanced past the
    # prior commit — and closed both issues via the serial closure path, in
    # ascending issue-number order.
    assert fake_git.head_sha() != "0000000000000000000000000000000000000001"
    assert [n for (n, _c) in fake_gh.issue_close_calls] == [42, 43]
    # One wrapper.auto_close event per landed + closed Lane, same order.
    auto_closes = [e for e in events if e["type"] == "wrapper.auto_close"]
    assert [e["issue"] for e in auto_closes] == [42, 43]
    # Both integrated Lane branches deleted (breadcrumbs are for failures only).
    assert sorted(fake_git.branch_deletes) == sorted(branches)


def test_parallel_run_falls_back_to_serial_when_under_two_eligible(
    tmp_path, monkeypatch
) -> None:
    """< 2 eligible parallel-safe issues: the round is one serial Iteration.

    The pool has a single ``parallel-safe`` issue plus a plain
    ``ready-for-agent`` issue. A Wave needs at least two eligible issues, so the
    round falls back to a normal serial Iteration — no worktrees, one
    unpinned session — and neither issue is stranded (eligibility is a human
    assertion, never inferred).
    """
    fake_git = _wire_repo(tmp_path)
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[
            _make_issue(42, labels=["ready-for-agent", "parallel-safe"]),
            _make_issue(43, labels=["ready-for-agent"]),
        ],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    fake_client = _ParallelFakeClient(
        fake_git=fake_git,
        scripted_events=[_usage_event("claude-opus-4.8-max")],
    )
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)
    monkeypatch.setattr(
        loop_module, "_make_gate_runner", lambda: FakeGateRunner()
    )

    cfg = RunConfig(
        model="claude-opus-4.8-max",
        issue_source="github",
        parallel=3,
        max_iterations=1,
        max_nmt_strikes=3,
        verbosity=0,
        render_reasoning=False,
    )

    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 0, f"expected exit 0, got {exit_code}"

    # No Wave: fewer than two eligible parallel-safe issues.
    assert fake_git.worktree_adds == [], "no worktrees when a Wave can't form"
    assert fake_git.worktree_removes == []

    # Exactly one serial session, NOT worktree-pinned (serial path).
    assert len(fake_client.created) == 1
    assert fake_client.create_calls[0]["working_directory"] is None

    # The serial Iteration works the whole pool — both issues appear in the
    # one prompt, so no eligible work is stranded by opting into Parallel mode.
    prompt, _timeout = fake_client.created[0].send_and_wait_calls[0]
    assert "Issue #42" in prompt
    assert "Issue #43" in prompt


def test_parallel_integration_lands_and_closes_in_ascending_issue_order(
    tmp_path, monkeypatch
) -> None:
    """Integration merges + closes green Lanes in ascending issue-number order.

    The pool is seeded in DESCENDING order (43 before 42) to prove Integration
    imposes its own deterministic ascending-issue-number sequence rather than
    inheriting pool / dispatch order: with an all-green gate both Lanes land on
    base and their issues close in ``[42, 43]`` order, and both integrated
    branches are deleted. Assertions are on observable effects, not call order.
    """
    fake_git = _wire_repo(tmp_path)
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[
            _make_issue(43, labels=["ready-for-agent", "parallel-safe"]),
            _make_issue(42, labels=["ready-for-agent", "parallel-safe"]),
        ],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    fake_client = _ParallelFakeClient(
        fake_git=fake_git,
        scripted_events=[_usage_event("claude-opus-4.8-max")],
    )
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)
    # All-green gate: every Lane's feedback loops pass, so every Lane lands.
    monkeypatch.setattr(
        loop_module, "_make_gate_runner", lambda: FakeGateRunner()
    )

    cfg = RunConfig(
        model="claude-opus-4.8-max",
        issue_source="github",
        parallel=2,
        max_iterations=1,
        max_nmt_strikes=3,
        verbosity=0,
        render_reasoning=False,
    )

    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 0, f"expected exit 0, got {exit_code}"

    # Both Lanes dispatched, but Integration closes issues ASCENDING regardless
    # of the descending pool / dispatch order.
    assert len(fake_client.created) == 2
    assert [n for (n, _c) in fake_gh.issue_close_calls] == [42, 43]
    # Serial closure semantics: both issues actually flipped CLOSED in the store.
    assert fake_gh.issue_view(42).state == "CLOSED"
    assert fake_gh.issue_view(43).state == "CLOSED"

    # One wrapper.auto_close event per landed Lane, ascending.
    events = _logged_events(tmp_path)
    auto_closes = [e for e in events if e["type"] == "wrapper.auto_close"]
    assert [e["issue"] for e in auto_closes] == [42, 43]

    # A successful Integration counts as Strike progress: the round landed two
    # Lanes, so the shared Strike machine saw progress and recorded no strike.
    assert [e for e in events if e["type"] == "wrapper.strike"] == []

    # Both green Lanes landed on base (base advanced past the prior commit) and
    # both integrated branches were deleted.
    assert fake_git.head_sha() != "0000000000000000000000000000000000000001"
    deleted = sorted(fake_git.branch_deletes)
    assert len(deleted) == 2
    assert deleted[0].endswith("/issue-42")
    assert deleted[1].endswith("/issue-43")

    # Integration ran after the Wave barrier — no worktrees left live.
    assert fake_git.active_worktrees == []


def test_parallel_integration_red_gate_keeps_branch_and_records_strike(
    tmp_path, monkeypatch
) -> None:
    """Red-throughout Integration: revert keeps base green, falls back to serial.

    Evolves the #62 happy-path contract deliberately (#63, ADR-0009). With every
    Lane's gate red *and* every auto-resolution attempt red, each Lane: merges
    cleanly, gates red, is **reverted** so base stays green, runs the bounded
    K=3 auto-resolution agent (all red), then falls back to a serial Iteration
    with **exactly one** breadcrumb comment — its Lane branch **kept** (only the
    throwaway integration branch is deleted). Nothing lands, so the no-progress
    Wave records exactly one warn strike (a Wave that integrates nothing is not
    progress). Assertions are on observable effects only.
    """
    fake_git = _wire_repo(tmp_path)
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[
            _make_issue(42, labels=["ready-for-agent", "parallel-safe"]),
            _make_issue(43, labels=["ready-for-agent", "parallel-safe"]),
        ],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    fake_client = _ParallelFakeClient(
        fake_git=fake_git,
        scripted_events=[_usage_event("claude-opus-4.8-max")],
    )
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)
    # All-red gate: every Lane's feedback loops fail on the initial landing AND
    # on every auto-resolution attempt, so no Lane ever lands.
    monkeypatch.setattr(
        loop_module, "_make_gate_runner", lambda: FakeGateRunner(default=False)
    )

    cfg = RunConfig(
        model="claude-opus-4.8-max",
        issue_source="github",
        parallel=2,
        max_iterations=1,
        max_nmt_strikes=3,
        verbosity=0,
        render_reasoning=False,
    )

    exit_code = asyncio.run(loop_module.run(cfg))

    # One warn strike (1 < 3) does not abort the run; the iteration cap ends it.
    assert exit_code == 0, f"expected exit 0, got {exit_code}"

    # Nothing landed: no issue closed and both remain OPEN for the serial round.
    assert fake_gh.issue_close_calls == []
    assert fake_gh.issue_view(42).state == "OPEN"
    assert fake_gh.issue_view(43).state == "OPEN"

    # Base stayed green: each clean-but-red landing was reverted (one per Lane).
    assert len(fake_git.reverts) == 2

    # Both Lane branches are KEPT as breadcrumbs; only the two throwaway
    # integration branches are deleted (a fallback deletes no Lane branch).
    assert len(fake_git.branch_deletes) == 2
    assert all("/integrate/" in b for b in fake_git.branch_deletes)

    # Exactly one breadcrumb comment per terminal fallback (one per Lane), and
    # the comment resolves nothing (both issues stay OPEN, asserted above).
    assert sorted(n for n, _ in fake_gh.issue_comment_calls) == [42, 43]

    # The no-progress Wave recorded exactly one warn strike, and Integration
    # closed nothing.
    events = _logged_events(tmp_path)
    strikes = [e for e in events if e["type"] == "wrapper.strike"]
    assert len(strikes) == 1
    assert strikes[0]["outcome"] == "warn"
    assert strikes[0]["strikes"] == 1
    assert [e for e in events if e["type"] == "wrapper.auto_close"] == []


def test_parallel_integration_auto_resolves_red_lane_then_lands(
    tmp_path, monkeypatch
) -> None:
    """A red Lane is reverted, auto-resolved on a later attempt, and lands (#63).

    Issue 42's Lane merges cleanly but its gate goes red on the initial landing
    AND on the first auto-resolution attempt, then passes on the second attempt;
    issue 43 is green throughout. The scripted gate is a global call-ordered
    queue ``[42-postmerge=red, 42-att1=red, 42-att2=green]`` with the default
    (green) covering 43. Asserts (observable effects only): base is reverted
    once (stays green), the K-bounded auto-resolution agent runs exactly twice
    for 42 in its dedicated integration worktree, both issues end CLOSED with one
    ``auto_close`` each, no breadcrumb is posted, and — because two Integrations
    landed — the round records no strike.
    """
    fake_git = _wire_repo(tmp_path)
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[
            _make_issue(42, labels=["ready-for-agent", "parallel-safe"]),
            _make_issue(43, labels=["ready-for-agent", "parallel-safe"]),
        ],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    fake_client = _ParallelFakeClient(
        fake_git=fake_git,
        scripted_events=[_usage_event("claude-opus-4.8-max")],
    )
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)
    # 42 is red on its initial landing and its first auto-resolution attempt,
    # green on the second; 43 (default) is green.
    monkeypatch.setattr(
        loop_module,
        "_make_gate_runner",
        lambda: FakeGateRunner(outcomes=[False, False, True], default=True),
    )

    cfg = RunConfig(
        model="claude-opus-4.8-max",
        issue_source="github",
        parallel=2,
        max_iterations=1,
        max_nmt_strikes=3,
        verbosity=0,
        render_reasoning=False,
    )

    exit_code = asyncio.run(loop_module.run(cfg))
    assert exit_code == 0, f"expected exit 0, got {exit_code}"

    # Base stayed green: the clean-but-red landing of 42 was reverted exactly
    # once (43 was green on its first landing, so it was never reverted).
    assert fake_git.reverts == [git_module.lane_branch_name(_run_id(tmp_path), 42)]

    # The K-bounded auto-resolution agent ran exactly twice for 42, each session
    # pinned to 42's dedicated integration worktree (never 43's).
    resolution_dirs = [
        c["working_directory"]
        for c in fake_client.create_calls
        if c["working_directory"] and "/integrate/" in c["working_directory"]
    ]
    assert len(resolution_dirs) == 2
    assert all(wd.endswith("/issue-42") for wd in resolution_dirs)

    # Both issues landed and closed — 42 via auto-resolution, 43 via the happy
    # path — with exactly one auto_close each and no breadcrumb (no fallback).
    assert sorted(n for (n, _c) in fake_gh.issue_close_calls) == [42, 43]
    assert fake_gh.issue_view(42).state == "CLOSED"
    assert fake_gh.issue_view(43).state == "CLOSED"
    assert fake_gh.issue_comment_calls == []

    events = _logged_events(tmp_path)
    assert [
        e["issue"] for e in events if e["type"] == "wrapper.auto_close"
    ] == [42, 43]
    # Two Integrations landed = progress, so the round records no strike.
    assert [e for e in events if e["type"] == "wrapper.strike"] == []


def test_parallel_integration_aborts_conflicting_merge_then_auto_resolves(
    tmp_path, monkeypatch
) -> None:
    """A conflicting Lane merge is aborted (not reverted), then auto-resolved (#63).

    Issue 42's Lane branch is scripted to *conflict* on merge; issue 43 merges
    cleanly. A conflict leaves no merge to revert, so recovery must
    ``git merge --abort`` (not ``git revert``) to keep base green, then run the
    auto-resolution agent — which passes on its first attempt here (all-green
    gate) and lands 42. Asserts the abort fired exactly once, no revert
    happened, both issues closed, and the round made progress (no strike).
    """
    fake_git = _wire_repo(tmp_path, merge_conflicts=[42])
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[
            _make_issue(42, labels=["ready-for-agent", "parallel-safe"]),
            _make_issue(43, labels=["ready-for-agent", "parallel-safe"]),
        ],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    fake_client = _ParallelFakeClient(
        fake_git=fake_git,
        scripted_events=[_usage_event("claude-opus-4.8-max")],
    )
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)
    # All-green gate: the conflict is a *merge* failure, not a gate failure, so
    # 42's first auto-resolution attempt (and 43's landing) pass.
    monkeypatch.setattr(
        loop_module, "_make_gate_runner", lambda: FakeGateRunner(default=True)
    )

    cfg = RunConfig(
        model="claude-opus-4.8-max",
        issue_source="github",
        parallel=2,
        max_iterations=1,
        max_nmt_strikes=3,
        verbosity=0,
        render_reasoning=False,
    )

    exit_code = asyncio.run(loop_module.run(cfg))
    assert exit_code == 0, f"expected exit 0, got {exit_code}"

    # The conflict path aborts (base stays green) and never reverts.
    assert fake_git.merge_aborts == 1
    assert fake_git.reverts == []

    # 42 was recovered by exactly one auto-resolution attempt in its dedicated
    # integration worktree; 43 landed via the happy path.
    resolution_dirs = [
        c["working_directory"]
        for c in fake_client.create_calls
        if c["working_directory"] and "/integrate/" in c["working_directory"]
    ]
    assert resolution_dirs == [
        str(
            loop_module._integration_worktree_path(
                tmp_path, _run_id(tmp_path), 42
            )
        )
    ]

    # Both issues closed; no breadcrumb (no fallback); the round made progress.
    assert sorted(n for (n, _c) in fake_gh.issue_close_calls) == [42, 43]
    assert fake_gh.issue_view(42).state == "CLOSED"
    assert fake_gh.issue_view(43).state == "CLOSED"
    assert fake_gh.issue_comment_calls == []
    events = _logged_events(tmp_path)
    assert [e for e in events if e["type"] == "wrapper.strike"] == []


def test_parallel_integration_falls_back_to_serial_after_k_attempts(
    tmp_path, monkeypatch
) -> None:
    """K=3 terminal failure falls back to a serial Iteration with one breadcrumb (#63).

    Issue 42's gate is red on its initial landing AND on all K=3 auto-resolution
    attempts (four reds), so it terminally fails Integration; issue 43 is green.
    With ``max_iterations=0`` and ``serial_closes`` the run then drains: the Wave
    lands 43, 42 falls back to a serial Iteration, and a later serial round works
    42 to closure. Asserts base stayed green (reverted), the auto-resolution
    agent ran exactly K=3 times, exactly ONE breadcrumb comment was posted on 42,
    42's Lane branch was KEPT (only its throwaway integration branch deleted),
    and the run drained the pool (both issues CLOSED, ``empty_pool``).
    """
    fake_git = _wire_repo(tmp_path)
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[
            _make_issue(42, labels=["ready-for-agent", "parallel-safe"]),
            _make_issue(43, labels=["ready-for-agent", "parallel-safe"]),
        ],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    fake_client = _ParallelFakeClient(
        fake_git=fake_git,
        scripted_events=[_usage_event("claude-opus-4.8-max")],
        serial_closes=True,
    )
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)
    # 42 red on its landing + all K=3 attempts (four reds); 43 (default) green.
    monkeypatch.setattr(
        loop_module,
        "_make_gate_runner",
        lambda: FakeGateRunner(outcomes=[False, False, False, False], default=True),
    )

    cfg = RunConfig(
        model="claude-opus-4.8-max",
        issue_source="github",
        parallel=2,
        max_iterations=0,  # unlimited: drive until the pool drains
        max_nmt_strikes=3,
        verbosity=0,
        render_reasoning=False,
    )

    exit_code = asyncio.run(loop_module.run(cfg))
    assert exit_code == 0, f"expected a clean drain (exit 0), got {exit_code}"

    # Base stayed green: 42's clean-but-red landing was reverted.
    assert git_module.lane_branch_name(_run_id(tmp_path), 42) in fake_git.reverts

    # The auto-resolution agent ran exactly K=3 times for 42 (the bound holds),
    # each session pinned to 42's dedicated integration worktree.
    resolution_dirs = [
        c["working_directory"]
        for c in fake_client.create_calls
        if c["working_directory"] and "/integrate/" in c["working_directory"]
    ]
    assert len(resolution_dirs) == loop_module._AUTO_RESOLUTION_MAX_ATTEMPTS
    assert all(wd.endswith("/issue-42") for wd in resolution_dirs)

    # Exactly ONE automated breadcrumb was posted on 42 for the fallback.
    assert [n for (n, _b) in fake_gh.issue_comment_calls] == [42]

    # 42's Lane branch is KEPT as a breadcrumb; only its throwaway integration
    # branch was deleted (the fallback deletes no Lane branch).
    lane_42 = git_module.lane_branch_name(_run_id(tmp_path), 42)
    assert lane_42 not in fake_git.branch_deletes
    assert (
        git_module.integration_branch_name(_run_id(tmp_path), 42)
        in fake_git.branch_deletes
    )

    # The run drained the pool: 42 closed via the serial fallback round, 43 via
    # Integration; the run ended on empty_pool, not the iteration cap.
    assert fake_gh.issue_view(42).state == "CLOSED"
    assert fake_gh.issue_view(43).state == "CLOSED"
    events = _logged_events(tmp_path)
    run_end = next(e for e in events if e["type"] == "wrapper.run.end")
    assert run_end["outcome"] == "empty_pool"


def test_parallel_run_drains_waves_then_serial_in_one_run(
    tmp_path, monkeypatch
) -> None:
    """Drain-everything (#67, ADR-0008): Waves for parallel-safe, serial for the rest.

    A Parallel run must never strand eligible work: it interleaves a **Wave**
    for the ``parallel-safe`` issues with normal serial **Iterations** for every
    other ``ready-for-agent`` issue, in one run, until the pool is drained. The
    pool mixes two ``parallel-safe`` issues (42, 43) with one plain
    ``ready-for-agent`` issue (44). Driven through ``run(config)`` with
    ``max_iterations=0`` (run until the pool empties) and an all-green gate, this
    asserts (observable effects only):

    * **Round 1 is a Wave** — only 42 and 43 (the human-asserted ``parallel-safe``
      issues) become Lanes with their own worktree + branch; 44 never does.
    * **A later round is serial** — exactly one unpinned session, whose prompt
      carries the plain issue 44 and no longer carries the already-closed 42/43
      (eligibility is a human assertion, so 44 is worked serially, not dropped).
    * **No stranding** — all three issues close (42/43 via Integration, 44 via the
      serial closure path) and the run terminates by draining the pool
      (``empty_pool``), not by hitting the iteration cap or a strike abort.
    * **Correct round-level Strike accounting across both kinds of round** — the
      Wave landed two Lanes (progress) and the serial Iteration committed + closed
      (progress), so no round records a strike.
    """
    fake_git = _wire_repo(tmp_path)
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[
            _make_issue(42, labels=["ready-for-agent", "parallel-safe"]),
            _make_issue(43, labels=["ready-for-agent", "parallel-safe"]),
            _make_issue(44, labels=["ready-for-agent"]),
        ],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    # serial_closes: the serial-fallback session "picks one" issue and closes it,
    # so a plain ``ready-for-agent`` issue actually drains and the run can reach
    # an empty pool rather than looping until a strike abort.
    fake_client = _ParallelFakeClient(
        fake_git=fake_git,
        scripted_events=[_usage_event("claude-opus-4.8-max")],
        serial_closes=True,
    )
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)
    # All-green gate: every landed Lane's feedback loops pass, so both parallel-
    # safe Lanes land at Integration.
    monkeypatch.setattr(
        loop_module, "_make_gate_runner", lambda: FakeGateRunner()
    )

    cfg = RunConfig(
        model="claude-opus-4.8-max",
        issue_source="github",
        parallel=2,
        max_iterations=0,  # unlimited: drive until the pool drains
        max_nmt_strikes=3,
        verbosity=0,
        render_reasoning=False,
    )

    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 0, f"expected a clean drain (exit 0), got {exit_code}"

    # --- Round 1 was a Wave: only the two parallel-safe issues became Lanes,
    #     each with its own worktree + branch. The plain issue 44 never did —
    #     eligibility is a human assertion, never inferred.
    adds = fake_git.worktree_adds
    assert len(adds) == 2, f"expected exactly two Lane worktrees (42,43), got {adds}"
    waved = sorted(int(b.split("/issue-")[1]) for (_p, b, _base) in adds)
    assert waved == [42, 43], "only the parallel-safe issues become Lanes"

    # --- A later round was serial: exactly one unpinned session; the two Lane
    #     sessions were worktree-pinned. Three sessions total across the run.
    working_dirs = [c["working_directory"] for c in fake_client.create_calls]
    assert working_dirs.count(None) == 1, "exactly one serial (unpinned) session"
    assert sum(wd is not None for wd in working_dirs) == 2, (
        "both Lane sessions were worktree-pinned"
    )

    # --- The serial round worked the plain issue AFTER the Wave closed 42/43:
    #     its prompt carries #44 and no longer carries the closed parallel-safe
    #     issues, so opting into Parallel mode strands nothing.
    serial_idx = working_dirs.index(None)
    serial_session = fake_client.created[serial_idx]
    serial_prompt, _timeout = serial_session.send_and_wait_calls[0]
    assert "=== Issue #44:" in serial_prompt
    assert "=== Issue #42:" not in serial_prompt
    assert "=== Issue #43:" not in serial_prompt

    # --- No stranding: all three issues closed — 42/43 via Integration, 44 via
    #     the serial closure path — and each actually flipped CLOSED in the store.
    assert sorted(n for (n, _c) in fake_gh.issue_close_calls) == [42, 43, 44]
    for n in (42, 43, 44):
        assert fake_gh.issue_view(n).state == "CLOSED", f"#{n} was not closed"

    events = _logged_events(tmp_path)

    # --- Correct round-level Strike accounting across BOTH kinds of round: the
    #     Wave (two landed Lanes) and the serial Iteration (a commit + a closure)
    #     each made progress, so no round recorded a strike.
    assert [e for e in events if e["type"] == "wrapper.strike"] == []

    # --- The run terminated by draining the pool, not by the iteration cap or a
    #     strike abort.
    run_end = next(e for e in events if e["type"] == "wrapper.run.end")
    assert run_end["outcome"] == "empty_pool"

    # --- One auto_close per issue: the parallel-safe pair first (Wave /
    #     Integration, ascending), then the plain issue (serial round).
    auto_closes = [
        e["issue"] for e in events if e["type"] == "wrapper.auto_close"
    ]
    assert auto_closes == [42, 43, 44]
