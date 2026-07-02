"""End-to-end integration test for :mod:`ralph_afk.loop`.

Drives one full ``ralph-afk`` iteration with the SDK and git/gh seams
mocked, asserts the canonical artefacts land on disk with the
documented schema, and exercises the auto-close backstop (commit
referencing ``Closes #42`` triggers ``gh.issue_close(42, ...)``).

The test does **not** spin up a real Copilot session or hit the GitHub
API; it monkeypatches:

* ``ralph_afk.loop._make_client`` → a :class:`FakeCopilotClient`
  (reused from :mod:`tests.test_session`) that scripts a single
  iteration's SDK event flow.
* ``ralph_afk.loop._make_github_client`` → a single
  :class:`~tests.fakes.FakeGitHubClient` (issue #47) injected through the
  loop's ``gh`` seam, replacing the old per-function ``ralph_afk.gh.*``
  monkeypatches. Its issue store keeps ``issue_list`` / ``issue_view``
  consistent, and its ``issue_close`` records the call AND flips the issue to
  ``CLOSED`` by construction (modelling the auto-close backstop's re-verify).
* ``ralph_afk.loop._make_git_client`` → a single
  :class:`~tests.fakes.FakeGitClient` (issue #46) injected through the loop's
  git seam, replacing the old per-function ``ralph_afk.git.*`` monkeypatches.
  Its stateful linear commit log keeps ``head_sha`` / ``commits_between`` /
  ``recent_commits`` consistent, and :meth:`FakeGitClient.simulate_agent_commit`
  (driven from the SDK stub's ``on_send`` hook) models the agent's commit
  landing between the pre- and post-iteration head reads.

After ``loop.run`` returns, the test asserts:

* Return code 0.
* ``.ralph/logs/<stem>.jsonl`` exists and every line is envelope-
  conformant JSON.
* ``.ralph/runs/<stem>.json`` exists and matches the persist schema
  (one iteration row, expected counts).
* ``.gitignore`` contains ``.ralph/`` (the persist factory touches it).
* The fake client's ``issue_close`` was called exactly once with the right
  arguments (auto-close backstop fired).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast
from uuid import uuid4

import pytest
from copilot import CopilotClient
from copilot.generated.session_events import (
    AssistantMessageData,
    AssistantUsageData,
    SessionEvent,
    SessionEventType,
    ToolExecutionStartData,
)

from ralph_afk import gh as gh_module
from ralph_afk import git as git_module
from ralph_afk import loop as loop_module
from ralph_afk.config import RunConfig
from ralph_afk.emit import EventEmitter
from ralph_afk.events import REDACTED_SECRET
from ralph_afk.persist import WritersBundle, create_writers
from ralph_afk.pricing import Pricing
from ralph_afk.sinks import SinkFanout
from ralph_afk.ui import RunSummary
from ralph_afk.wrapper import is_checkpoint_message
from tests.fakes import FakeGitClient, FakeGitHubClient


# ---------------------------------------------------------------------------
# Fakes — minimal stand-ins for the SDK + git/gh surface the loop touches.
# ---------------------------------------------------------------------------


class FakeCopilotSession:
    """Stub for :class:`copilot.CopilotSession`.

    Holds the registered ``on_event`` callback. ``send_and_wait`` drives
    a scripted SDK event flow against the callback then returns.
    """

    def __init__(
        self,
        *,
        on_event: Callable[[SessionEvent], None] | None,
        scripted_events: list[SessionEvent],
        on_send: Callable[[], None] | None = None,
    ) -> None:
        self._on_event = on_event
        self._scripted_events = scripted_events
        self._on_send = on_send
        self.session_id = "fake-session-id"
        self.send_and_wait_calls: list[tuple[str, float]] = []

    async def send_and_wait(
        self,
        prompt: str,
        *,
        timeout: float = 60.0,
        **_extra: Any,
    ) -> SessionEvent | None:
        self.send_and_wait_calls.append((prompt, timeout))
        # Model the agent doing its work *during* the session — between the
        # loop's pre- and post-iteration ``head_sha`` reads — so an injected
        # commit advances the fake git log while the SDK "runs".
        if self._on_send is not None:
            self._on_send()
        last: SessionEvent | None = None
        for evt in self._scripted_events:
            if self._on_event is not None:
                self._on_event(evt)
            last = evt
        return last

    async def disconnect(self) -> None:
        return None


class FakeCopilotClient:
    """Stub for :class:`copilot.CopilotClient` shaped for the loop.

    The loop calls ``create_session(...)`` per iteration and ``stop()``
    once at the end. ``create_session`` returns a :class:`FakeCopilotSession`
    pre-loaded with the test's scripted events.
    """

    def __init__(
        self,
        scripted_events: list[SessionEvent],
        *,
        on_send: Callable[[], None] | None = None,
    ) -> None:
        self._scripted_events = scripted_events
        self.on_send = on_send
        self.created: list[FakeCopilotSession] = []
        self.stop_call_count = 0

    async def create_session(
        self,
        *,
        on_permission_request: Any,
        on_event: Callable[[SessionEvent], None] | None = None,
        on_user_input_request: Any = None,
        model: str | None = None,
        **_extra: Any,
    ) -> FakeCopilotSession:
        session = FakeCopilotSession(
            on_event=on_event,
            scripted_events=self._scripted_events,
            on_send=self.on_send,
        )
        self.created.append(session)
        return session

    async def stop(self) -> None:
        self.stop_call_count += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sdk_event(
    et: SessionEventType,
    data: Any,
    *,
    ts: datetime | None = None,
) -> SessionEvent:
    return SessionEvent(
        data=data,
        id=uuid4(),
        timestamp=ts if ts is not None else datetime(2026, 5, 16, 0, 0, 0, tzinfo=timezone.utc),
        type=et,
    )


def _make_issue(
    number: int,
    *,
    body: str = "## Parent\nfoo\n\n## What to build\nthing\n\n## Acceptance criteria\nbar",
    state: str = "OPEN",
) -> gh_module.Issue:
    return gh_module.Issue(
        number=number,
        title=f"Test issue {number}",
        body=body,
        labels=["ready-for-agent"],
        state=state,
        url=f"https://github.com/x/y/issues/{number}",
        comments=(),
    )


def _logged_types(tmp_path: Path) -> list[str]:
    """Return the ordered ``type`` of every JSONL event the run logged."""
    logs_dir = tmp_path / ".ralph" / "logs"
    lines = next(logs_dir.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
    return [json.loads(raw)["type"] for raw in lines]


# ---------------------------------------------------------------------------
# The end-to-end test
# ---------------------------------------------------------------------------


def test_loop_runs_one_iteration_end_to_end(tmp_path, monkeypatch) -> None:
    """One iteration: SDK fires events; loop persists JSONL + run summary; auto-close fires.

    Wires:

    * tmp_path as the repo root with a ``ralph/prompt.md`` and a
      pre-existing ``.gitignore``.
    * Two issues in the AFK-ready pool (#42 OPEN with discriminator;
      #43 OPEN without — should be filtered out at the body-discriminator
      step).
    * A scripted SDK event flow: ``session.created`` → ``tool.execution.start``
      → ``assistant.message`` → ``assistant.usage`` →
      ``session.idle``.
    * Mocked git: one new commit between pre and post HEAD; commit
      message references ``Closes #42`` so the auto-close backstop
      should fire.
    """
    # -- 1) Fake repo on disk ---------------------------------------------
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text(
        "You are ralph. Implement the AFK-ready issues.\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")

    # -- 2) git stubs ------------------------------------------------------
    # -- 2) git seam: FakeGitClient seeded with the prior commit ----------
    # The agent's commit (with ``Closes #42``) is appended *during* the SDK
    # session via the client's ``on_send`` hook (wired below), so the
    # post-iteration head advances past the pre-iteration head and
    # ``commits_between`` yields exactly that agent commit.
    fake_git = FakeGitClient(
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
    )
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    # -- 3) gh seam: FakeGitHubClient seeded with the AFK-ready pool ------
    # #42 is AFK-ready (carries the discriminator); #43 lacks it and is
    # filtered at the list stage before any ``issue_view``. The fake's
    # ``issue_close`` records the call AND flips #42 OPEN -> CLOSED by
    # construction, modelling the transition the auto-close re-verify relies
    # on (no ``issue_42_state`` bookkeeping needed).
    issue_42 = _make_issue(42)
    issue_43_no_discrim = _make_issue(
        43, body="no parent here, no AC here, just words"
    )
    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[issue_42, issue_43_no_discrim],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    # -- 4) SDK stub: one tool call + one assistant message + usage -------
    scripted = [
        _sdk_event(
            SessionEventType.TOOL_EXECUTION_START,
            ToolExecutionStartData(
                tool_call_id="call-1",
                tool_name="edit",
                arguments={"path": "foo.py"},
            ),
        ),
        _sdk_event(
            SessionEventType.ASSISTANT_MESSAGE,
            AssistantMessageData(
                content="Implementing #42.",
                message_id="m1",
            ),
        ),
        _sdk_event(
            SessionEventType.ASSISTANT_USAGE,
            AssistantUsageData(
                input_tokens=1234,
                output_tokens=567,
                model="claude-opus-4.7-xhigh",
            ),
        ),
    ]

    fake_client = FakeCopilotClient(scripted_events=scripted)
    # The agent authors its commit (referencing ``Closes #42``) mid-session.
    fake_client.on_send = lambda: fake_git.simulate_agent_commit(
        sha="abcdef1234567890abcdef1234567890abcdef12",
        subject="feat(thing): implement",
        body="Closes #42",
    )
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)

    # -- 5) Run loop with max_iterations=1 -------------------------------
    cfg = RunConfig(
        model="claude-opus-4.7-xhigh",
        issue_source="github",
        max_iterations=1,
        max_nmt_strikes=3,
        verbosity=0,
        render_reasoning=False,
    )

    exit_code = asyncio.run(loop_module.run(cfg))

    # -- 6) Assertions ---------------------------------------------------
    assert exit_code == 0, f"expected exit 0, got {exit_code}"

    # SDK lifecycle.
    assert len(fake_client.created) == 1, "expected exactly one SDK session"
    assert fake_client.stop_call_count == 1, "client.stop() must be called once at end"

    # send_and_wait got the prompt.
    sdk_session = fake_client.created[0]
    assert len(sdk_session.send_and_wait_calls) == 1
    prompt, timeout = sdk_session.send_and_wait_calls[0]
    assert "Previous commits:" in prompt
    assert "Issue #42" in prompt
    # #43 lacks the discriminator and must be filtered out.
    assert "Issue #43" not in prompt
    assert "You are ralph" in prompt
    assert timeout > 60.0, f"send_and_wait timeout must exceed SDK default; got {timeout}"

    # Auto-close fired for #42, not for any other issue.
    assert len(fake_gh.issue_close_calls) == 1, (
        f"expected exactly one close call for #42; got {fake_gh.issue_close_calls}"
    )
    assert fake_gh.issue_close_calls[0][0] == 42
    assert "abcdef1234" in fake_gh.issue_close_calls[0][1], (
        f"close comment should reference the closing commit SHA; "
        f"got {fake_gh.issue_close_calls[0][1]!r}"
    )

    # .gitignore touched.
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".ralph/" in gitignore.splitlines()

    # JSONL log present + envelope conformant.
    logs_dir = tmp_path / ".ralph" / "logs"
    jsonl_files = list(logs_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1, (
        f"expected exactly one JSONL log; got {jsonl_files}"
    )
    log_lines = jsonl_files[0].read_text(encoding="utf-8").splitlines()
    assert log_lines, "JSONL log must not be empty"
    types_seen: list[str] = []
    for raw in log_lines:
        evt = json.loads(raw)
        assert set(evt.keys()) >= {"ts", "run_id", "iter", "type"}, (
            f"event missing envelope keys: {evt!r}"
        )
        types_seen.append(evt["type"])
    # Must have seen at minimum: run.start, iteration.start, afk_ready,
    # iteration.end, commit.recorded, auto_close, run.end.
    for expected_type in (
        "wrapper.run.start",
        "wrapper.iteration.start",
        "wrapper.afk_ready.collected",
        "wrapper.commit.recorded",
        "wrapper.auto_close",
        "wrapper.iteration.end",
        "wrapper.run.end",
    ):
        assert expected_type in types_seen, (
            f"expected to see {expected_type} in JSONL log; "
            f"saw types: {types_seen}"
        )

    # run-summary JSON present with documented schema.
    runs_dir = tmp_path / ".ralph" / "runs"
    json_files = list(runs_dir.glob("*.json"))
    assert len(json_files) == 1
    payload = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert "run_id" in payload
    assert "started_at" in payload
    assert "iterations" in payload
    assert len(payload["iterations"]) == 1
    iter_row = payload["iterations"][0]
    assert iter_row["iter"] == 1
    assert iter_row["commits"] == 1
    assert iter_row["auto_closures"] == 1
    assert iter_row["model"] == "claude-opus-4.7-xhigh"
    assert iter_row["tokens_in"] == 1234
    assert iter_row["tokens_out"] == 567
    assert iter_row["tool_count"] == 1
    assert iter_row["strikes"] == 0  # progress was made (1 commit + 1 close)


def test_loop_empty_pool_exits_zero(tmp_path, monkeypatch) -> None:
    """An empty AFK-ready pool short-circuits with exit code 0 — no SDK call."""
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("be ralph", encoding="utf-8")

    fake_git = FakeGitClient(tmp_path)
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"), issues=[]
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    fake_client = FakeCopilotClient(scripted_events=[])
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)

    cfg = RunConfig(issue_source="github", max_iterations=1)
    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 0
    # No SDK session created on empty-pool fast path.
    assert len(fake_client.created) == 0, (
        f"expected no SDK session on empty pool; got {len(fake_client.created)}"
    )
    # client.stop() still ran in the loop's finally.
    assert fake_client.stop_call_count == 1


def _wire_single_issue_github(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    issue_number: int = 42,
    dirty: bool = False,
    untracked: bool = False,
    commit_error: git_module.GitError | None = None,
    push_error: git_module.GitError | None = None,
) -> tuple[FakeCopilotClient, FakeGitClient]:
    """Minimal github wiring for a one-issue run with no agent commits.

    Sets up the repo on disk, injects a :class:`~tests.fakes.FakeGitHubClient`
    (one AFK-ready issue that is never closed) through the loop's ``gh`` seam,
    and injects a single :class:`~tests.fakes.FakeGitClient` through the loop's
    git seam. By default the worktree is clean and the agent makes no commit, so
    ``head_sha`` is constant across the iteration (``commits_between`` is empty);
    pass ``dirty=True`` / ``untracked=True`` / ``commit_error`` / ``push_error``
    to script the Checkpoint / push path a test wants. Returns
    ``(fake_client, fake_git)`` so the caller can drive the SDK ``on_send`` hook
    and inspect the ``add_all`` / ``commit`` / ``push`` spies.
    """
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("be ralph", encoding="utf-8")

    issue = _make_issue(issue_number)
    fake_git = FakeGitClient(
        tmp_path,
        dirty=dirty,
        untracked=untracked,
        commit_error=commit_error,
        push_error=push_error,
    )
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[issue],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    fake_client = FakeCopilotClient(scripted_events=[])
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)
    return fake_client, fake_git


def test_loop_dirty_worktree_checkpoints_and_continues(
    tmp_path, monkeypatch
) -> None:
    """A dirty worktree no longer aborts: it produces one Checkpoint and runs on.

    The stale_worktree abort (ADR-0004) is gone. At the iteration boundary a
    dirty tree is staged (``add_all``) and captured in a single
    close-keyword-free Checkpoint commit attributed to the Active issue, then
    the run completes normally (exit 0). The Checkpoint emits
    ``wrapper.checkpoint.recorded`` — NOT ``wrapper.commit.recorded`` — so it is
    not counted as agent progress.
    """
    fake_client, fake_git = _wire_single_issue_github(
        tmp_path, monkeypatch, dirty=True, untracked=False
    )

    cfg = RunConfig(issue_source="github", max_iterations=1, max_nmt_strikes=3)
    exit_code = asyncio.run(loop_module.run(cfg))

    # The dirty tree did NOT abort the run.
    assert exit_code == 0
    assert len(fake_client.created) == 1, "the SDK session still ran"

    # Exactly one Checkpoint: stage everything, then one commit.
    assert fake_git.add_all_calls, (
        "the worktree must be staged before the Checkpoint"
    )
    assert len(fake_git.commit_messages) == 1, (
        f"expected exactly one Checkpoint commit; got {fake_git.commit_messages}"
    )
    msg = fake_git.commit_messages[0]
    assert is_checkpoint_message(msg), "Checkpoint must carry the trailer"
    assert "42" in msg, "Checkpoint is attributed to the Active issue #42"

    # The Checkpoint surfaced as wrapper.checkpoint.recorded, never as a commit.
    logs_dir = tmp_path / ".ralph" / "logs"
    log_lines = next(logs_dir.glob("*.jsonl")).read_text(
        encoding="utf-8"
    ).splitlines()
    types_seen = [json.loads(raw)["type"] for raw in log_lines]
    assert "wrapper.checkpoint.recorded" in types_seen
    assert "wrapper.commit.recorded" not in types_seen
    assert "wrapper.stale_worktree.aborted" not in types_seen

    # The persisted iteration counts no agent commits for the Checkpoint.
    runs_dir = tmp_path / ".ralph" / "runs"
    payload = json.loads(next(runs_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert payload["iterations"][0]["commits"] == 0


def test_loop_clean_worktree_makes_no_checkpoint(tmp_path, monkeypatch) -> None:
    """A clean (neither dirty nor untracked) worktree never authors a Checkpoint."""
    _, fake_git = _wire_single_issue_github(
        tmp_path, monkeypatch, dirty=False, untracked=False
    )

    cfg = RunConfig(issue_source="github", max_iterations=1)
    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 0
    assert fake_git.commit_messages == [], (
        "a clean worktree must not be checkpointed"
    )
    logs_dir = tmp_path / ".ralph" / "logs"
    log_lines = next(logs_dir.glob("*.jsonl")).read_text(
        encoding="utf-8"
    ).splitlines()
    types_seen = [json.loads(raw)["type"] for raw in log_lines]
    assert "wrapper.checkpoint.recorded" not in types_seen


def test_checkpoint_is_excluded_from_strikes_abort_after_n_still_fires(
    tmp_path, monkeypatch
) -> None:
    """Checkpoints never reset strikes: a stuck agent still aborts after N.

    Every iteration the agent makes no commit (no progress) but leaves a dirty
    tree, so the runner Checkpoints each time. Because Checkpoints are excluded
    from Strike progress, the no-progress strikes still accumulate and the
    abort-after-N protection fires (exit 1) — the durability net did not mask a
    genuinely stuck agent.
    """
    fake_client, fake_git = _wire_single_issue_github(
        tmp_path, monkeypatch, dirty=True, untracked=False
    )

    cfg = RunConfig(
        issue_source="github", max_iterations=10, max_nmt_strikes=2
    )
    exit_code = asyncio.run(loop_module.run(cfg))

    # Abort-after-N still fires despite every iteration being Checkpointed.
    assert exit_code == 1
    # Two iterations to reach the 2-strike threshold, one Checkpoint each.
    assert len(fake_git.commit_messages) == 2, (
        f"expected one Checkpoint per stuck iteration; "
        f"got {fake_git.commit_messages}"
    )
    assert len(fake_client.created) == 2


def test_checkpoint_failure_is_non_fatal(tmp_path, monkeypatch) -> None:
    """A Checkpoint commit failure warns but never aborts the run.

    A local-only repo (no remote) or a transient git error during the
    Checkpoint must not take down the loop — the iteration completes and the
    run exits normally.
    """
    _wire_single_issue_github(
        tmp_path,
        monkeypatch,
        dirty=True,
        untracked=False,
        commit_error=git_module.GitError(
            ["git", "commit"], 1, "nothing to commit"
        ),
    )

    cfg = RunConfig(issue_source="github", max_iterations=1, max_nmt_strikes=3)
    exit_code = asyncio.run(loop_module.run(cfg))

    # The failed Checkpoint did not abort the run.
    assert exit_code == 0
    logs_dir = tmp_path / ".ralph" / "logs"
    log_lines = next(logs_dir.glob("*.jsonl")).read_text(
        encoding="utf-8"
    ).splitlines()
    types_seen = [json.loads(raw)["type"] for raw in log_lines]
    # No checkpoint event was emitted (the commit failed before emit).
    assert "wrapper.checkpoint.recorded" not in types_seen
    # And crucially the run reached its clean end.
    assert "wrapper.run.end" in types_seen


# ---------------------------------------------------------------------------
# Auto-push (issue #35 — ADR-0004 durability net, second half)
# ---------------------------------------------------------------------------


def test_loop_pushes_after_agent_commit(tmp_path, monkeypatch) -> None:
    """A new agent commit triggers the auto-push; ``wrapper.push.recorded`` is logged.

    The clean-tree, one-agent-commit case: no Checkpoint is made, but the
    iteration still produced a new commit, so the current branch is pushed to
    its upstream after accounting.
    """
    fake_client, fake_git = _wire_single_issue_github(
        tmp_path, monkeypatch, dirty=False, untracked=False
    )
    # One agent commit, no close keyword -> pure progress, no auto-closure.
    fake_client.on_send = lambda: fake_git.simulate_agent_commit(
        sha="a" * 40, subject="feat: real work", body="Refs #42"
    )

    cfg = RunConfig(issue_source="github", max_iterations=1, max_nmt_strikes=3)
    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 0
    assert fake_git.push_calls == 1, (
        "a new agent commit must trigger exactly one push"
    )
    types_seen = _logged_types(tmp_path)
    assert "wrapper.commit.recorded" in types_seen
    assert "wrapper.push.recorded" in types_seen


def test_loop_pushes_after_checkpoint(tmp_path, monkeypatch) -> None:
    """A Checkpoint (no agent commit) still triggers the auto-push.

    The dirty-tree, zero-agent-commit case: the only new commit this iteration
    is the runner Checkpoint, which is enough to push the branch to the remote.
    """
    _, fake_git = _wire_single_issue_github(
        tmp_path, monkeypatch, dirty=True, untracked=False
    )

    cfg = RunConfig(issue_source="github", max_iterations=1, max_nmt_strikes=3)
    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 0
    assert fake_git.push_calls == 1, "the Checkpoint must trigger exactly one push"
    types_seen = _logged_types(tmp_path)
    assert "wrapper.checkpoint.recorded" in types_seen
    assert "wrapper.push.recorded" in types_seen


def test_loop_no_push_when_clean_and_no_new_commits(tmp_path, monkeypatch) -> None:
    """A clean tree with no agent commit and no Checkpoint never pushes."""
    _, fake_git = _wire_single_issue_github(
        tmp_path, monkeypatch, dirty=False, untracked=False
    )

    cfg = RunConfig(issue_source="github", max_iterations=1, max_nmt_strikes=3)
    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 0
    assert fake_git.push_calls == 0, "nothing new to push -> no push attempt"
    assert "wrapper.push.recorded" not in _logged_types(tmp_path)


def test_loop_push_failure_is_non_fatal(tmp_path, monkeypatch) -> None:
    """A push failure (no remote / auth / non-fast-forward) warns but never aborts.

    A local-only repo (no upstream) must keep working: the push raises
    :exc:`git.GitError`, the loop swallows it with a warning, the run exits 0,
    and — mirroring the failed-Checkpoint path — no ``wrapper.push.recorded``
    event is emitted (the push never landed).
    """
    _wire_single_issue_github(
        tmp_path,
        monkeypatch,
        dirty=True,
        untracked=False,
        push_error=git_module.GitError(
            ["git", "push"], 128, "no upstream configured"
        ),
    )

    cfg = RunConfig(issue_source="github", max_iterations=1, max_nmt_strikes=3)
    exit_code = asyncio.run(loop_module.run(cfg))

    # The failed push did not abort the run.
    assert exit_code == 0
    types_seen = _logged_types(tmp_path)
    # The Checkpoint landed, but the push failed -> no push event, clean end.
    assert "wrapper.checkpoint.recorded" in types_seen
    assert "wrapper.push.recorded" not in types_seen
    assert "wrapper.run.end" in types_seen


def test_loop_prds_end_to_end_one_iteration(tmp_path, monkeypatch) -> None:
    """One PRDs iteration end-to-end: discovery → SDK → commit → no auto-close.

    Drives the local-markdown collector against a fixture tree:

    * ``prds/featA/001-ready.md`` — AFK-ready discriminator present.
    * ``prds/featA/002-not-ready.md`` — missing discriminator (filter).
    * ``prds/featA/done/000-archived.md`` — under ``done/`` (filter).
    * ``prds/featA/prd.md`` — no NNN prefix (filter).

    Asserts:

    * Exit code 0.
    * SDK saw a prompt containing the ready file's path + body but NOT
      the filtered files.
    * One commit recorded, ``auto_closures == 0`` (PRDs is detection-only).
    * Run-summary JSON shape matches the github variant.
    * ``gh.issue_close`` was never called (gh isn't touched in PRDs mode).
    * The worktree (``prds/`` tree) is unchanged after the run —
      detection-only completion semantics.
    """
    # -- 1) Fake repo on disk with a PRDs fixture tree --------------------
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("be ralph", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")

    ready_md = tmp_path / "prds" / "featA" / "001-ready.md"
    not_ready_md = tmp_path / "prds" / "featA" / "002-not-ready.md"
    archived_md = tmp_path / "prds" / "featA" / "done" / "000-archived.md"
    prd_md = tmp_path / "prds" / "featA" / "prd.md"
    for p in (ready_md, not_ready_md, archived_md, prd_md):
        p.parent.mkdir(parents=True, exist_ok=True)

    afk_body = (
        "# 001 — Ready\n\n## Parent\nfeatA\n\n## What to build\nthing\n\n"
        "## Acceptance criteria\n- impl\n"
    )
    ready_md.write_text(afk_body, encoding="utf-8")
    not_ready_md.write_text("Just words, no sections.\n", encoding="utf-8")
    archived_md.write_text(afk_body, encoding="utf-8")
    prd_md.write_text(afk_body, encoding="utf-8")

    # Snapshot the prds tree BEFORE the run for the post-run no-mutation
    # assertion (detection-only semantics).
    files_before = {
        p.relative_to(tmp_path).as_posix()
        for p in (tmp_path / "prds").rglob("*")
        if p.is_file()
    }

    # -- 2) git seam: FakeGitClient (agent commit appended mid-session) ---
    fake_git = FakeGitClient(
        tmp_path,
        commits=[
            git_module.Commit(
                sha="0" * 40, subject="prior", body="", date="2026-05-16"
            )
        ],
        dirty=False,
        untracked=False,
    )
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    # -- 3) gh MUST NOT be reached in PRDs mode ---------------------------
    # The loop only touches GitHub through the client from
    # ``_make_github_client``; PRDs mode uses PrdsIssueSource and must never
    # construct one. Record any attempt to build the client so a regression
    # that reaches for gh in PRDs mode fails loudly.
    gh_calls: list[str] = []

    def _forbidden_github_client() -> gh_module.GitHubClient:
        gh_calls.append("_make_github_client")
        raise AssertionError("gh must not be constructed in PRDs mode")

    monkeypatch.setattr(loop_module, "_make_github_client", _forbidden_github_client)

    # -- 4) SDK stub: minimal scripted flow --------------------------------
    scripted = [
        _sdk_event(
            SessionEventType.ASSISTANT_MESSAGE,
            AssistantMessageData(content="working on it", message_id="m1"),
        ),
        _sdk_event(
            SessionEventType.ASSISTANT_USAGE,
            AssistantUsageData(
                input_tokens=100,
                output_tokens=50,
                model="claude-opus-4.7-xhigh",
            ),
        ),
    ]
    fake_client = FakeCopilotClient(scripted_events=scripted)
    # The agent authors one commit mid-session (PRDs mode never auto-closes:
    # PrdsIssueSource.handle_completions returns [] — the agent owns the git mv).
    fake_client.on_send = lambda: fake_git.simulate_agent_commit(
        sha="a" * 40,
        subject="feat(featA/001): implement",
        body="Refs prds/featA/001-ready.md",
    )
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)

    # -- 5) Run loop with issue_source=prds --------------------------------
    cfg = RunConfig(
        model="claude-opus-4.7-xhigh",
        issue_source="prds",
        max_iterations=1,
        max_nmt_strikes=3,
        verbosity=0,
        render_reasoning=False,
    )
    exit_code = asyncio.run(loop_module.run(cfg))

    # -- 6) Assertions -----------------------------------------------------
    assert exit_code == 0, f"expected exit 0; got {exit_code}"
    assert gh_calls == [], (
        f"PRDs mode must not touch gh; got calls: {gh_calls}"
    )

    # SDK lifecycle: one session, one prompt, no further calls.
    assert len(fake_client.created) == 1, (
        "expected exactly one SDK session in PRDs mode"
    )
    sdk_session = fake_client.created[0]
    assert len(sdk_session.send_and_wait_calls) == 1
    prompt, _timeout = sdk_session.send_and_wait_calls[0]

    # The ready file must be in the prompt; the filtered files must NOT.
    assert "prds/featA/001-ready.md" in prompt, (
        "AFK-ready PRDs file should appear in the prompt"
    )
    assert "002-not-ready" not in prompt, (
        "non-AFK PRDs file should be filtered out"
    )
    assert "000-archived" not in prompt, (
        "done/* PRDs files should be filtered out"
    )
    assert "prd.md" not in prompt or "001-ready" in prompt, (
        "loose prd.md should be filtered out by NNN-prefix discriminator"
    )

    # Worktree untouched (detection-only semantics).
    files_after = {
        p.relative_to(tmp_path).as_posix()
        for p in (tmp_path / "prds").rglob("*")
        if p.is_file()
    }
    assert files_before == files_after, (
        "PRDs handle_completions must not move/delete files; "
        f"before={files_before} after={files_after}"
    )

    # JSONL log contains the expected wrapper events but NO auto_close.
    logs_dir = tmp_path / ".ralph" / "logs"
    log_files = list(logs_dir.glob("*.jsonl"))
    assert len(log_files) == 1
    types_seen = [
        json.loads(line)["type"]
        for line in log_files[0].read_text(encoding="utf-8").splitlines()
    ]
    for expected in (
        "wrapper.run.start",
        "wrapper.iteration.start",
        "wrapper.afk_ready.collected",
        "wrapper.commit.recorded",
        "wrapper.iteration.end",
        "wrapper.run.end",
    ):
        assert expected in types_seen, (
            f"expected {expected} in JSONL; saw: {types_seen}"
        )
    assert "wrapper.auto_close" not in types_seen, (
        "PRDs mode must not emit wrapper.auto_close — handle_completions returns []"
    )

    # Run-summary JSON.
    json_files = list((tmp_path / ".ralph" / "runs").glob("*.json"))
    assert len(json_files) == 1
    payload = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert len(payload["iterations"]) == 1
    iter_row = payload["iterations"][0]
    assert iter_row["iter"] == 1
    assert iter_row["commits"] == 1
    assert iter_row["auto_closures"] == 0, (
        "PRDs mode must report zero auto-closures"
    )


def test_loop_prds_empty_pool_exits_zero(tmp_path, monkeypatch) -> None:
    """An absent ``prds/`` directory short-circuits with exit 0 — no SDK call."""
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("be ralph", encoding="utf-8")
    # NB: no `prds/` directory created.

    fake_git = FakeGitClient(tmp_path)
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    # PRDs mode must not construct a GitHubClient.
    def _forbidden_github_client() -> Any:
        raise AssertionError("gh must not be constructed in PRDs mode")

    monkeypatch.setattr(loop_module, "_make_github_client", _forbidden_github_client)

    fake_client = FakeCopilotClient(scripted_events=[])
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)

    cfg = RunConfig(issue_source="prds", max_iterations=1)
    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 0
    # No SDK session created on empty-pool fast path.
    assert len(fake_client.created) == 0
    assert fake_client.stop_call_count == 1


def test_loop_preflight_failure_when_gh_not_authed(tmp_path, monkeypatch) -> None:
    """If ``gh auth status`` is not authenticated, the loop aborts with exit 1."""
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("be ralph", encoding="utf-8")

    fake_git = FakeGitClient(tmp_path)
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)
    monkeypatch.setattr(
        loop_module, "_make_github_client", lambda: FakeGitHubClient(authed=False)
    )

    fake_client = FakeCopilotClient(scripted_events=[])
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)

    cfg = RunConfig(issue_source="github", max_iterations=1)
    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 1
    assert len(fake_client.created) == 0


def test_loop_aborts_after_max_nmt_strikes(tmp_path, monkeypatch) -> None:
    """Three consecutive no-progress iterations abort the loop with exit 1.

    The SDK is mocked to produce no commits and no auto-closures, so
    every iteration is a strike. With ``max_nmt_strikes=3`` the loop
    aborts on iteration 3.
    """
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("be ralph", encoding="utf-8")

    fake_git = FakeGitClient(tmp_path)
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[_make_issue(42)],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    fake_client = FakeCopilotClient(scripted_events=[])
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)

    cfg = RunConfig(
        issue_source="github", max_iterations=0, max_nmt_strikes=3
    )
    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 1, "loop must abort after max strikes"
    # 3 sessions = 3 iterations until strike machine fires.
    assert len(fake_client.created) == 3, (
        f"expected 3 SDK sessions before abort; got {len(fake_client.created)}"
    )


# ---------------------------------------------------------------------------
# Additional rubber-duck-recommended coverage
# ---------------------------------------------------------------------------


def test_loop_send_and_wait_exception_is_no_progress(tmp_path, monkeypatch) -> None:
    """If ``send_and_wait`` raises, the iteration is treated as no-progress.

    The post-iteration accounting (commits_between, auto-close backstop,
    strike tick, iteration.end emit, counters persist) still runs — the
    SDK failure is contained to "no progress" semantics.
    """
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("be ralph", encoding="utf-8")

    fake_git = FakeGitClient(tmp_path)
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[_make_issue(42)],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    class RaisingSession(FakeCopilotSession):
        async def send_and_wait(self, prompt: str, *, timeout: float = 60.0, **_: Any) -> SessionEvent | None:
            raise RuntimeError("simulated SDK exception")

    class RaisingClient(FakeCopilotClient):
        async def create_session(self, **kwargs: Any) -> FakeCopilotSession:
            session = RaisingSession(on_event=None, scripted_events=[])
            self.created.append(session)
            return session

    fake_client = RaisingClient(scripted_events=[])
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)

    cfg = RunConfig(issue_source="github", max_iterations=1)
    exit_code = asyncio.run(loop_module.run(cfg))

    # No progress + 1 iteration cap = clean exit 0 (we didn't hit the
    # strike threshold).
    assert exit_code == 0
    # Post-iteration accounting still ran -> JSONL still includes
    # iteration.start, iteration.end, strike, run.end.
    log_files = list((tmp_path / ".ralph" / "logs").glob("*.jsonl"))
    assert len(log_files) == 1
    types_seen = {
        json.loads(line)["type"]
        for line in log_files[0].read_text(encoding="utf-8").splitlines()
    }
    assert "wrapper.iteration.end" in types_seen
    assert "wrapper.strike" in types_seen
    assert "wrapper.run.end" in types_seen


def test_loop_auto_close_failure_does_not_abort_iteration(tmp_path, monkeypatch) -> None:
    """A failing ``gh issue close`` is logged and the iteration continues.

    Verifies the per-issue try/except inside ``_try_auto_close``: one
    failing close must not prevent commits from being recorded or the
    strike machine from running.
    """
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("be ralph", encoding="utf-8")

    fake_git = FakeGitClient(tmp_path)
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    # The auto-close attempt fails for #42, but the iteration must not abort.
    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[_make_issue(42)],
        issue_close_errors={
            42: gh_module.GhError(
                ["gh", "issue", "close", "42"], 1, "simulated close failure"
            )
        },
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    fake_client = FakeCopilotClient(scripted_events=[])
    # The agent authors a commit referencing ``Closes #42`` mid-session; the
    # subsequent auto-close attempt fails (issue_close_errors) but must not abort.
    fake_client.on_send = lambda: fake_git.simulate_agent_commit(
        sha="deadbeef", subject="x", body="Closes #42"
    )
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)

    cfg = RunConfig(issue_source="github", max_iterations=1)
    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 0  # one iteration cap, no abort
    # JSONL should still contain commit.recorded for the one commit, but
    # NO auto_close events.
    log_files = list((tmp_path / ".ralph" / "logs").glob("*.jsonl"))
    types_seen = [
        json.loads(line)["type"]
        for line in log_files[0].read_text(encoding="utf-8").splitlines()
    ]
    assert "wrapper.commit.recorded" in types_seen
    assert "wrapper.auto_close" not in types_seen


def test_loop_make_client_failure_returns_exit_one(tmp_path, monkeypatch) -> None:
    """If ``_make_client()`` raises, ``run()`` returns 1 with no traceback escape."""
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("be ralph", encoding="utf-8")

    monkeypatch.setattr(
        loop_module, "_make_git_client", lambda: FakeGitClient(tmp_path)
    )
    monkeypatch.setattr(
        loop_module, "_make_github_client", lambda: FakeGitHubClient()
    )

    def _exploding_factory() -> Any:
        raise RuntimeError("simulated CopilotClient construction failure")

    monkeypatch.setattr(loop_module, "_make_client", _exploding_factory)

    cfg = RunConfig(issue_source="github", max_iterations=1)
    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 1


def test_loop_bad_pricing_file_returns_exit_one(tmp_path, monkeypatch) -> None:
    """A malformed ``RALPH_PRICING_FILE`` override surfaces as exit 1.

    Rubber-duck-confirmed acceptance: do NOT silently fall back to the
    packaged default, because that hides the operator's intent.
    """
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("be ralph", encoding="utf-8")

    # Write a broken TOML file to point pricing_file at.
    bad_pricing = tmp_path / "bad-pricing.toml"
    bad_pricing.write_text("this is not = valid [toml", encoding="utf-8")

    monkeypatch.setattr(
        loop_module, "_make_git_client", lambda: FakeGitClient(tmp_path)
    )

    fake_client = FakeCopilotClient(scripted_events=[])
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)

    cfg = RunConfig(
        issue_source="github", max_iterations=1, pricing_file=bad_pricing
    )
    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 1
    # No SDK session created — we bailed before constructing the client.
    assert len(fake_client.created) == 0


def test_loop_multiple_iterations_until_cap(tmp_path, monkeypatch) -> None:
    """Loop runs ``max_iterations`` iterations and exits 0 at the cap.

    Each iteration is mocked to produce one commit (progress -> no
    strikes), so the cap is the only stopping condition.
    """
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("be ralph", encoding="utf-8")

    fake_git = FakeGitClient(tmp_path)
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[_make_issue(99)],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    fake_client = FakeCopilotClient(scripted_events=[])
    # Each iteration the agent lands one fresh commit (progress -> no strikes),
    # so head advances every session and the only stop condition is the cap.
    fake_client.on_send = lambda: fake_git.simulate_agent_commit(
        subject="progress"
    )
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)

    cfg = RunConfig(issue_source="github", max_iterations=3, max_nmt_strikes=3)
    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 0
    assert len(fake_client.created) == 3, (
        f"expected exactly 3 SDK sessions; got {len(fake_client.created)}"
    )
    # Run-summary JSON should carry 3 iteration rows.
    json_files = list((tmp_path / ".ralph" / "runs").glob("*.json"))
    payload = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert len(payload["iterations"]) == 3


# ---------------------------------------------------------------------------
# OpenTelemetry span tree (issue #12)
# ---------------------------------------------------------------------------


def test_loop_emits_otel_span_tree_when_enabled(tmp_path, monkeypatch) -> None:
    """OTel-on: one iteration emits the documented span tree.

    Expected shape::

        ralph_afk.run
        └─ ralph_afk.iteration  (attrs: iter, issue, issues)
           ├─ ralph_afk.collect_issues
           ├─ ralph_afk.session
           └─ ralph_afk.enforce_closures

    Skips if the ``[otel]`` extra is not installed so the suite stays
    green on the base install.
    """
    # -- 0) Install OTel in-memory exporter BEFORE the loop opens any
    #       spans. The seam reuses an externally-installed
    #       TracerProvider on first init (see telemetry.otel docstring).
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
        from opentelemetry.util._once import Once
    except ImportError:  # pragma: no cover
        pytest.skip("opentelemetry not installed (run with --extra otel)")

    from ralph_afk.telemetry import otel as telemetry

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Bypass `set_tracer_provider`'s set-once guard (same pattern as
    # the in_memory_exporter fixture in test_telemetry_otel.py).
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider, raising=False)
    monkeypatch.setattr(
        trace, "_TRACER_PROVIDER_SET_ONCE", Once(), raising=False
    )

    # Wipe sticky-enable cache + flip RALPH_OTEL_ENABLED so the seam
    # picks up the externally-installed provider on its first init().
    telemetry.reset_for_tests()
    monkeypatch.setenv("RALPH_OTEL_ENABLED", "1")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    # -- 1) Fake repo on disk ---------------------------------------------
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text(
        "You are ralph.\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")

    # -- 2) git stubs ------------------------------------------------------
    # -- 2) git seam: FakeGitClient (agent commit appended mid-session) ---
    fake_git = FakeGitClient(
        tmp_path,
        commits=[
            git_module.Commit(
                sha="0" * 40, subject="prior commit", body="", date="2026-05-16"
            )
        ],
        dirty=False,
        untracked=False,
    )
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    # -- 3) gh seam: FakeGitHubClient (issue_close flips #42 to CLOSED) ----
    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[_make_issue(42)],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    # -- 4) SDK stub (minimal: empty event flow) ---------------------------
    fake_client = FakeCopilotClient(scripted_events=[])
    # The agent authors its ``Closes #42`` commit mid-session so the closure
    # (and its ralph_afk.enforce_closures span) fires as before.
    fake_client.on_send = lambda: fake_git.simulate_agent_commit(
        sha="abcdef1234567890abcdef1234567890abcdef12",
        subject="feat: stuff",
        body="Closes #42",
    )
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)

    # -- 5) Run loop with max_iterations=1 ---------------------------------
    cfg = RunConfig(
        issue_source="github",
        max_iterations=1,
        max_nmt_strikes=3,
        otel_enabled=True,
    )

    exit_code = asyncio.run(loop_module.run(cfg))
    assert exit_code == 0, f"expected exit 0, got {exit_code}"

    # -- 6) Drain & inspect spans -----------------------------------------
    telemetry.force_flush()
    spans = exporter.get_finished_spans()

    by_name: dict[str, list[Any]] = {}
    for s in spans:
        by_name.setdefault(s.name, []).append(s)

    # Expected five spans, one of each documented name.
    expected_names = {
        "ralph_afk.run",
        "ralph_afk.iteration",
        "ralph_afk.collect_issues",
        "ralph_afk.session",
        "ralph_afk.enforce_closures",
    }
    seen_names = set(by_name)
    assert expected_names <= seen_names, (
        f"missing expected spans; "
        f"expected {expected_names}, got {seen_names}"
    )

    # Exactly one of each.
    for name in expected_names:
        assert len(by_name[name]) == 1, (
            f"expected exactly one {name!r} span; got {len(by_name[name])}"
        )

    run_span = by_name["ralph_afk.run"][0]
    iter_span = by_name["ralph_afk.iteration"][0]
    collect_span = by_name["ralph_afk.collect_issues"][0]
    session_span = by_name["ralph_afk.session"][0]
    closures_span = by_name["ralph_afk.enforce_closures"][0]

    # Parent relationships: run → iteration → {collect, session, closures}.
    assert run_span.parent is None, "ralph_afk.run is the root span"
    assert iter_span.parent is not None
    assert iter_span.parent.span_id == run_span.context.span_id, (
        "ralph_afk.iteration must nest under ralph_afk.run"
    )
    for child in (collect_span, session_span, closures_span):
        assert child.parent is not None
        assert child.parent.span_id == iter_span.context.span_id, (
            f"{child.name!r} must nest under ralph_afk.iteration; "
            f"saw parent span_id {child.parent.span_id!r}"
        )

    # Iteration attrs: iter + issue + issues set after pool collect.
    attrs = dict(iter_span.attributes or {})
    assert attrs.get("iter") == 1, f"iter attr: {attrs!r}"
    assert attrs.get("issue") == 42, f"issue attr: {attrs!r}"
    issues_attr = attrs.get("issues")
    assert issues_attr is not None, f"issues attr missing: {attrs!r}"
    # `issues` is stored as a tuple/list of ints — OTel normalises to
    # a sequence.
    assert list(issues_attr) == [42], f"issues attr: {issues_attr!r}"


# ---------------------------------------------------------------------------
# PR-advance integration test (include_prs=True)
# ---------------------------------------------------------------------------


def _make_pr_view(
    number: int,
    *,
    head_sha: str,
    state: str = "OPEN",
    head_branch: str = "feature/pr-work",
    comments: tuple[gh_module.Comment, ...] = (),
) -> gh_module.PullRequest:
    return gh_module.PullRequest(
        number=number,
        title=f"Test PR {number}",
        body="",
        labels=["ready-for-agent"],
        state=state,
        url=f"https://github.com/x/y/pull/{number}",
        head_sha=head_sha,
        head_branch=head_branch,
        comments=comments,
    )


def test_loop_pr_advance_emits_pr_advanced_event(tmp_path, monkeypatch) -> None:
    """With include_prs=True, a PR whose head SHA advances emits wrapper.pr.advanced.

    No base-branch commit lands (PR work happens on the PR branch), so the
    only progress signal is the head-SHA advance. Asserts:

    * exit 0,
    * the PR block reaches the prompt,
    * ``wrapper.pr.advanced`` is logged and ``wrapper.auto_close`` is not,
    * the iteration row counts the advance as an auto-closure (progress),
      with 0 commits and 0 strikes,
    * the base branch is never switched (HEAD already on base).
    """
    # -- repo on disk -----------------------------------------------------
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text(
        "You are ralph. Advance the AFK-ready PRs.\n", encoding="utf-8"
    )
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")

    # -- git seam: clean tree on the base branch, no base-branch commit ---
    # (PR work happens on the PR branch; the only progress signal is the PR
    # head-SHA advance.) HEAD stays on the base branch, so no switch/restore.
    fake_git = FakeGitClient(tmp_path, dirty=False, untracked=False, branch="main")
    monkeypatch.setattr(loop_module, "_make_git_client", lambda: fake_git)

    # -- gh seam: FakeGitHubClient (PR head advances mid-session) ---------
    brief = gh_module.Comment(
        author="triage-bot",
        body="## Agent Brief\nFinish the caching change.",
        created_at="2026-05-16T00:00:00Z",
    )
    fake_gh = FakeGitHubClient(
        repo=gh_module.Repo(owner="x", name="y", default_branch="main"),
        issues=[],
        prs=[_make_pr_view(7, head_sha="prsha-old", comments=(brief,))],
    )
    monkeypatch.setattr(loop_module, "_make_github_client", lambda: fake_gh)

    # -- SDK stub ---------------------------------------------------------
    scripted = [
        _sdk_event(
            SessionEventType.TOOL_EXECUTION_START,
            ToolExecutionStartData(
                tool_call_id="call-1",
                tool_name="edit",
                arguments={"path": "cache.py"},
            ),
        ),
        _sdk_event(
            SessionEventType.ASSISTANT_MESSAGE,
            AssistantMessageData(content="Advancing PR #7.", message_id="m1"),
        ),
        _sdk_event(
            SessionEventType.ASSISTANT_USAGE,
            AssistantUsageData(
                input_tokens=100, output_tokens=50, model="claude-opus-4.7-xhigh"
            ),
        ),
    ]
    fake_client = FakeCopilotClient(scripted_events=scripted)
    # The agent pushes to the PR branch mid-session: the head advances between
    # the collection-time pr_view (baseline "prsha-old") and the post-iteration
    # advance-check pr_view, so _detect_pr_advances records the advance.
    fake_client.on_send = lambda: fake_gh.set_pr_head(7, "prsha-new")
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)

    # -- run --------------------------------------------------------------
    cfg = RunConfig(
        model="claude-opus-4.7-xhigh",
        issue_source="github",
        include_prs=True,
        max_iterations=1,
        max_nmt_strikes=3,
        verbosity=0,
        render_reasoning=False,
    )
    exit_code = asyncio.run(loop_module.run(cfg))

    # -- assertions -------------------------------------------------------
    assert exit_code == 0, f"expected exit 0, got {exit_code}"
    assert fake_git.switch_calls == [], (
        "base branch must not be switched when HEAD is on base"
    )

    # PR block reached the prompt.
    sdk_session = fake_client.created[0]
    prompt, _timeout = sdk_session.send_and_wait_calls[0]
    assert "PR #7" in prompt
    assert "(branch: feature/pr-work)" in prompt

    # Event log: pr.advanced present, auto_close absent.
    jsonl_files = list((tmp_path / ".ralph" / "logs").glob("*.jsonl"))
    assert len(jsonl_files) == 1
    types_seen: list[str] = []
    pr_advanced_payloads: list[dict[str, Any]] = []
    for raw in jsonl_files[0].read_text(encoding="utf-8").splitlines():
        evt = json.loads(raw)
        types_seen.append(evt["type"])
        if evt["type"] == "wrapper.pr.advanced":
            pr_advanced_payloads.append(evt)
    assert "wrapper.pr.advanced" in types_seen, f"saw: {types_seen}"
    assert "wrapper.auto_close" not in types_seen, f"saw: {types_seen}"
    assert "wrapper.commit.recorded" not in types_seen, (
        "no base-branch commit landed this iteration"
    )
    assert pr_advanced_payloads[0].get("pr") == 7

    # Run-summary: the advance counts as progress (auto_closure), 0 commits, 0 strikes.
    json_files = list((tmp_path / ".ralph" / "runs").glob("*.json"))
    payload = json.loads(json_files[0].read_text(encoding="utf-8"))
    iter_row = payload["iterations"][0]
    assert iter_row["commits"] == 0
    assert iter_row["auto_closures"] == 1
    assert iter_row["strikes"] == 0


# ---------------------------------------------------------------------------
# Loop event fan-out through the shared EventEmitter (issue #45)
# ---------------------------------------------------------------------------


class _NoopSource:
    """Inert :class:`~ralph_afk.sources.IssueSource` stand-in.

    ``_Loop.__init__`` merely stores the source; ``_emit`` never reaches it, so
    a no-op is enough to construct a ``_Loop`` in isolation for a focused
    fan-out test.
    """

    def preflight(self) -> int | None:
        return None

    def collect_afk_ready(self) -> list[Any]:
        return []

    def handle_completions(
        self, *, pool: list[Any], new_commits: list[Any]
    ) -> list[Any]:
        return []

    def comment(self, ref: int | str, body: str) -> None:
        return None


class _RecordingSink:
    """Records each envelope handed to ``render`` (the sink contract surface)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def render(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def stream_reasoning(self, delta: str) -> None:  # pragma: no cover
        pass

    def stream_message(self, delta: str) -> None:  # pragma: no cover
        pass


def _make_loop(
    repo_root: Path, sinks: SinkFanout
) -> tuple[loop_module._Loop, WritersBundle]:
    """Construct a real ``_Loop`` wired to ``sinks``.

    Only the collaborators ``_emit`` reaches — ``writers`` (its ``run_id`` /
    ``event_log``), ``sinks``, and ``diag`` — are meaningful here; the rest are
    inert stand-ins the constructor merely stores.
    """
    writers = create_writers(repo_root)
    pricing = Pricing(models={})
    loop = loop_module._Loop(
        config=RunConfig(),
        git=FakeGitClient(repo_root),
        prompt_text="",
        pricing=pricing,
        writers=writers,
        sinks=sinks,
        summary=RunSummary(pricing=pricing),
        client=cast(CopilotClient, None),
        source=_NoopSource(),
        diag=writers.diagnostics,
    )
    return loop, writers


def test_loop_emit_fans_scrubbed_envelope_to_sink_via_emitter(tmp_path) -> None:
    """``_Loop._emit`` fans the *scrubbed* envelope out to the sinks (issue #45).

    #45 shrinks ``_emit`` to ``self._emitter.emit(...)`` with the emitter built
    in ``__init__`` (``diag=self._diag``). This pins two things the pre-#45
    inline ``_emit`` violated:

    * the loop composes its fan-out on a shared :class:`EventEmitter` — the
      ``_emitter`` assertion fails against the pre-#45 ``_Loop`` (which had no
      emitter);
    * the sink contract that ``render`` only ever sees an *already-scrubbed*
      envelope — a secret on a wrapper event reaches the sink **redacted**,
      closing the loop's scrub gap (the pre-#45 ``_emit`` fanned the *unscrubbed*
      envelope out to the sinks). ``emit`` still returns the *pre-scrub* envelope
      the loop reads its SHA / subject off, and the JSONL writer + sink agree on
      the same scrubbed bytes.
    """
    secret = "ghp_" + "A" * 36
    sink = _RecordingSink()
    loop, writers = _make_loop(tmp_path, SinkFanout([sink]))

    # The loop composes its fan-out on the shared EventEmitter (diag=self._diag).
    assert isinstance(loop._emitter, EventEmitter)

    with writers.event_log:
        returned = loop._emit(
            "wrapper.commit.recorded", iter_num=1, subject=f"landed {secret}"
        )

    # The sink saw the *scrubbed* envelope — the loop's scrub gap is closed.
    assert sink.events, "sink never received the emitted envelope"
    received = sink.events[0]
    assert secret not in json.dumps(received)
    assert REDACTED_SECRET in received["subject"]
    # ``emit`` returns the pre-scrub envelope the loop inspects (SHA / subject).
    assert returned["subject"] == f"landed {secret}"
    assert returned is not received
    # Writer and sink agree — both got the same scrubbed bytes.
    log_lines = [
        json.loads(ln)
        for ln in writers.event_log.path.read_text(encoding="utf-8")
        .strip()
        .splitlines()
    ]
    assert log_lines[-1] == received
