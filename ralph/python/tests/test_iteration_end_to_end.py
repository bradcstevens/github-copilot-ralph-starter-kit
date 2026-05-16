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
* ``ralph_afk.gh.auth_status`` / ``repo_view`` / ``issue_list`` /
  ``issue_view`` / ``issue_close`` → stubs.
* ``ralph_afk.git.repo_root`` / ``head_sha`` / ``is_dirty`` /
  ``commits_between`` / ``recent_commits`` → stubs.

After ``loop.run`` returns, the test asserts:

* Return code 0.
* ``.ralph/logs/<stem>.jsonl`` exists and every line is envelope-
  conformant JSON.
* ``.ralph/runs/<stem>.json`` exists and matches the persist schema
  (one iteration row, expected counts).
* ``.gitignore`` contains ``.ralph/`` (the persist factory touches it).
* The ``gh.issue_close`` stub was called exactly once with the right
  arguments (auto-close backstop fired).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import pytest
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
    ) -> None:
        self._on_event = on_event
        self._scripted_events = scripted_events
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

    def __init__(self, scripted_events: list[SessionEvent]) -> None:
        self._scripted_events = scripted_events
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


@dataclass(frozen=True)
class _FakeCommit:
    """Lightweight stand-in for :class:`git.Commit` returned by the stubs.

    Carries the same attributes the loop reads — ``sha``, ``subject``,
    ``body``, ``date``, and the computed ``message`` property.
    """

    sha: str
    subject: str
    body: str = ""
    date: str = "2026-05-16"

    @property
    def message(self) -> str:
        return f"{self.subject}\n{self.body}" if self.body else self.subject


def _make_issue(
    number: int,
    *,
    body: str = "## Parent\nfoo\n\n## Acceptance criteria\nbar",
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
    monkeypatch.setattr(git_module, "repo_root", lambda start=None: tmp_path)
    monkeypatch.setattr(git_module, "is_dirty", lambda start=None: False)

    head_sequence = iter(["pre-sha-abc", "post-sha-xyz"])

    def fake_head_sha(start: Any = None) -> str:
        return next(head_sequence)

    monkeypatch.setattr(git_module, "head_sha", fake_head_sha)
    monkeypatch.setattr(
        git_module,
        "recent_commits",
        lambda n, start=None: [
            _FakeCommit(sha="0000000000000000000000000000000000000001", subject="prior commit")
        ],
    )

    new_commit = _FakeCommit(
        sha="abcdef1234567890abcdef1234567890abcdef12",
        subject="feat(thing): implement",
        body="Closes #42",
    )
    monkeypatch.setattr(
        git_module,
        "commits_between",
        lambda pre, head, start=None: [new_commit],
    )

    # -- 3) gh stubs -------------------------------------------------------
    issue_42 = _make_issue(42)
    issue_43_no_discrim = _make_issue(
        43, body="no parent here, no AC here, just words"
    )

    monkeypatch.setattr(gh_module, "auth_status", lambda: True)
    monkeypatch.setattr(
        gh_module,
        "repo_view",
        lambda: gh_module.Repo(owner="x", name="y", default_branch="main"),
    )
    monkeypatch.setattr(
        gh_module,
        "issue_list",
        lambda label, state="open": [issue_42, issue_43_no_discrim],
    )

    issue_view_calls: list[int] = []
    close_calls: list[tuple[int, str]] = []

    # State sequence for #42: still OPEN at auto-close time, then CLOSED
    # after issue_close returns. issue_view is called during pool
    # enrichment AND during the auto-close re-verify.
    issue_42_state = {"value": "OPEN"}

    def fake_issue_view(number: int) -> gh_module.Issue:
        issue_view_calls.append(number)
        if number == 42:
            return gh_module.Issue(
                number=42,
                title=issue_42.title,
                body=issue_42.body,
                labels=issue_42.labels,
                state=issue_42_state["value"],
                url=issue_42.url,
                comments=(),
            )
        if number == 43:
            return issue_43_no_discrim
        raise gh_module.GhError(["gh", "issue", "view", str(number)], 1, "not found")

    def fake_issue_close(number: int, comment: str) -> None:
        close_calls.append((number, comment))
        if number == 42:
            issue_42_state["value"] = "CLOSED"

    monkeypatch.setattr(gh_module, "issue_view", fake_issue_view)
    monkeypatch.setattr(gh_module, "issue_close", fake_issue_close)

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
    assert close_calls == [(42, close_calls[0][1])] if close_calls else False, (
        f"expected exactly one close call for #42; got {close_calls}"
    )
    assert close_calls[0][0] == 42
    assert "abcdef1234" in close_calls[0][1], (
        f"close comment should reference the closing commit SHA; "
        f"got {close_calls[0][1]!r}"
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

    monkeypatch.setattr(git_module, "repo_root", lambda start=None: tmp_path)
    monkeypatch.setattr(git_module, "is_dirty", lambda start=None: False)
    monkeypatch.setattr(git_module, "head_sha", lambda start=None: "deadbeef")
    monkeypatch.setattr(git_module, "recent_commits", lambda n, start=None: [])

    monkeypatch.setattr(gh_module, "auth_status", lambda: True)
    monkeypatch.setattr(
        gh_module,
        "repo_view",
        lambda: gh_module.Repo(owner="x", name="y", default_branch="main"),
    )
    monkeypatch.setattr(gh_module, "issue_list", lambda label, state="open": [])

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


def test_loop_stale_worktree_exits_one(tmp_path, monkeypatch) -> None:
    """A dirty worktree on iteration 1 aborts with exit code 1."""
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("be ralph", encoding="utf-8")

    monkeypatch.setattr(git_module, "repo_root", lambda start=None: tmp_path)
    monkeypatch.setattr(git_module, "is_dirty", lambda start=None: True)
    monkeypatch.setattr(git_module, "head_sha", lambda start=None: "deadbeef")
    monkeypatch.setattr(git_module, "recent_commits", lambda n, start=None: [])

    monkeypatch.setattr(gh_module, "auth_status", lambda: True)
    monkeypatch.setattr(
        gh_module,
        "repo_view",
        lambda: gh_module.Repo(owner="x", name="y", default_branch="main"),
    )

    fake_client = FakeCopilotClient(scripted_events=[])
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)

    cfg = RunConfig(issue_source="github", max_iterations=1)
    exit_code = asyncio.run(loop_module.run(cfg))

    assert exit_code == 1
    assert len(fake_client.created) == 0


def test_loop_prds_returns_two_with_clear_stderr(tmp_path, capsys) -> None:
    """``ISSUE_SOURCE='prds'`` short-circuits with exit 2 and a clear stderr."""
    cfg = RunConfig(issue_source="prds")
    exit_code = asyncio.run(loop_module.run(cfg))
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "prds" in captured.err


def test_loop_preflight_failure_when_gh_not_authed(tmp_path, monkeypatch) -> None:
    """If ``gh auth status`` is not authenticated, the loop aborts with exit 1."""
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("be ralph", encoding="utf-8")

    monkeypatch.setattr(git_module, "repo_root", lambda start=None: tmp_path)
    monkeypatch.setattr(gh_module, "auth_status", lambda: False)

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

    monkeypatch.setattr(git_module, "repo_root", lambda start=None: tmp_path)
    monkeypatch.setattr(git_module, "is_dirty", lambda start=None: False)
    monkeypatch.setattr(git_module, "head_sha", lambda start=None: "deadbeef")
    monkeypatch.setattr(git_module, "recent_commits", lambda n, start=None: [])
    monkeypatch.setattr(
        git_module, "commits_between", lambda pre, head, start=None: []
    )

    monkeypatch.setattr(gh_module, "auth_status", lambda: True)
    monkeypatch.setattr(
        gh_module,
        "repo_view",
        lambda: gh_module.Repo(owner="x", name="y", default_branch="main"),
    )
    monkeypatch.setattr(
        gh_module,
        "issue_list",
        lambda label, state="open": [_make_issue(42)],
    )
    monkeypatch.setattr(
        gh_module,
        "issue_view",
        lambda n: _make_issue(n),
    )

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
    SDK failure is contained to "no progress" semantics matching bash
    parity at ``ralph/afk.sh:365-367``.
    """
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("be ralph", encoding="utf-8")

    monkeypatch.setattr(git_module, "repo_root", lambda start=None: tmp_path)
    monkeypatch.setattr(git_module, "is_dirty", lambda start=None: False)
    monkeypatch.setattr(git_module, "head_sha", lambda start=None: "deadbeef")
    monkeypatch.setattr(git_module, "recent_commits", lambda n, start=None: [])
    monkeypatch.setattr(
        git_module, "commits_between", lambda pre, head, start=None: []
    )

    monkeypatch.setattr(gh_module, "auth_status", lambda: True)
    monkeypatch.setattr(
        gh_module,
        "repo_view",
        lambda: gh_module.Repo(owner="x", name="y", default_branch="main"),
    )
    monkeypatch.setattr(
        gh_module, "issue_list", lambda label, state="open": [_make_issue(42)]
    )
    monkeypatch.setattr(gh_module, "issue_view", lambda n: _make_issue(n))

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

    monkeypatch.setattr(git_module, "repo_root", lambda start=None: tmp_path)
    monkeypatch.setattr(git_module, "is_dirty", lambda start=None: False)
    head_sequence = iter(["pre", "post"])
    monkeypatch.setattr(git_module, "head_sha", lambda start=None: next(head_sequence))
    monkeypatch.setattr(git_module, "recent_commits", lambda n, start=None: [])
    monkeypatch.setattr(
        git_module,
        "commits_between",
        lambda pre, head, start=None: [
            _FakeCommit(sha="deadbeef", subject="x", body="Closes #42"),
        ],
    )

    monkeypatch.setattr(gh_module, "auth_status", lambda: True)
    monkeypatch.setattr(
        gh_module,
        "repo_view",
        lambda: gh_module.Repo(owner="x", name="y", default_branch="main"),
    )
    monkeypatch.setattr(
        gh_module, "issue_list", lambda label, state="open": [_make_issue(42)]
    )
    monkeypatch.setattr(gh_module, "issue_view", lambda n: _make_issue(n))

    def raising_close(number: int, comment: str) -> None:
        raise gh_module.GhError(
            ["gh", "issue", "close", str(number)], 1, "simulated close failure"
        )

    monkeypatch.setattr(gh_module, "issue_close", raising_close)

    fake_client = FakeCopilotClient(scripted_events=[])
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

    monkeypatch.setattr(git_module, "repo_root", lambda start=None: tmp_path)
    monkeypatch.setattr(gh_module, "auth_status", lambda: True)
    monkeypatch.setattr(
        gh_module,
        "repo_view",
        lambda: gh_module.Repo(owner="x", name="y", default_branch="main"),
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

    monkeypatch.setattr(git_module, "repo_root", lambda start=None: tmp_path)

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

    monkeypatch.setattr(git_module, "repo_root", lambda start=None: tmp_path)
    monkeypatch.setattr(git_module, "is_dirty", lambda start=None: False)
    head_counter = {"i": 0}

    def fake_head_sha(start: Any = None) -> str:
        head_counter["i"] += 1
        return f"sha-{head_counter['i']}"

    monkeypatch.setattr(git_module, "head_sha", fake_head_sha)
    monkeypatch.setattr(git_module, "recent_commits", lambda n, start=None: [])
    monkeypatch.setattr(
        git_module,
        "commits_between",
        lambda pre, head, start=None: [
            _FakeCommit(
                sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                subject="progress",
            )
        ],
    )

    monkeypatch.setattr(gh_module, "auth_status", lambda: True)
    monkeypatch.setattr(
        gh_module,
        "repo_view",
        lambda: gh_module.Repo(owner="x", name="y", default_branch="main"),
    )
    monkeypatch.setattr(
        gh_module, "issue_list", lambda label, state="open": [_make_issue(99)]
    )
    monkeypatch.setattr(gh_module, "issue_view", lambda n: _make_issue(n))

    fake_client = FakeCopilotClient(scripted_events=[])
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
