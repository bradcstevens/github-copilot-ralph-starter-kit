"""Tests for the interactive **driver seam** in :func:`ralph_afk.loop.run`.

Issue #23 / ADR-0001: when a driver is supplied, ``run`` registers the driver's
Textual-agnostic ``state`` (a :class:`~ralph_afk.interactive.state.LiveRunState`)
as the **sole** sink and hands the loop's ``drive`` coroutine to
:meth:`InteractiveDriver.run`, returning its result. With ``driver=None`` the
path is byte-for-byte today's behavior (covered by the rest of the loop suite).

The harness drives the cheap **empty-pool** path (exit 0) so no SDK session or
prompt building is needed — enough to prove the wiring: events fan out to the
interactive sink, and the driver owns driving.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from ralph_afk import gh as gh_module
from ralph_afk import git as git_module
from ralph_afk import loop as loop_module
from ralph_afk.config import RunConfig
from ralph_afk.interactive.state import LiveRunState


class _FakeClient:
    """Minimal CopilotClient stub: the empty-pool path only calls ``stop``."""

    def __init__(self) -> None:
        self.stop_call_count = 0

    async def stop(self) -> None:
        self.stop_call_count += 1


class _DelegatingDriver:
    """Driver that delegates to the real ``loop.drive`` (the common case)."""

    def __init__(self, state: LiveRunState) -> None:
        self.state = state
        self.run_called = False
        self.received_drive: Callable[[], Awaitable[int]] | None = None

    async def run(self, drive: Callable[[], Awaitable[int]]) -> int:
        self.run_called = True
        self.received_drive = drive
        return await drive()


class _SkippingDriver:
    """Driver that returns a sentinel **without** driving the loop.

    Proves ``run`` returns the driver's result and that the driver — not
    ``run`` — owns whether/how the loop is driven.
    """

    def __init__(self, state: LiveRunState, *, result: int) -> None:
        self.state = state
        self._result = result
        self.run_called = False

    async def run(self, drive: Callable[[], Awaitable[int]]) -> int:
        self.run_called = True
        return self._result


def _wire_empty_pool_repo(tmp_path: Any, monkeypatch: Any) -> _FakeClient:
    """Stub git/gh/SDK so ``run`` reaches the empty-pool clean exit."""
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "prompt.md").write_text("ralph prompt\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")

    monkeypatch.setattr(git_module, "repo_root", lambda start=None: tmp_path)
    monkeypatch.setattr(git_module, "is_dirty", lambda start=None: False)

    monkeypatch.setattr(gh_module, "auth_status", lambda: True)
    monkeypatch.setattr(
        gh_module,
        "repo_view",
        lambda: gh_module.Repo(owner="x", name="y", default_branch="main"),
    )
    monkeypatch.setattr(gh_module, "issue_list", lambda label, state="open": [])

    fake_client = _FakeClient()
    monkeypatch.setattr(loop_module, "_make_client", lambda: fake_client)
    return fake_client


def test_driver_state_is_the_sink_and_observes_run(tmp_path, monkeypatch) -> None:
    fake_client = _wire_empty_pool_repo(tmp_path, monkeypatch)
    state = LiveRunState(model="claude-opus-4.8", reasoning_effort="max")
    driver = _DelegatingDriver(state)

    cfg = RunConfig(
        issue_source="github", max_iterations=1, max_nmt_strikes=3, verbosity=0
    )
    exit_code = asyncio.run(loop_module.run(cfg, driver=driver))

    assert exit_code == 0
    assert driver.run_called is True
    # The loop handed the driver a coroutine-function it can drive as a peer
    # task; the delegating driver awaited it, so the run actually executed.
    assert driver.received_drive is not None
    assert asyncio.iscoroutinefunction(driver.received_drive)
    # The LiveRunState was the sole sink, so it observed the run's milestones.
    assert state.run_id != ""
    assert state.iteration == 1
    assert state.max_strikes == 3
    assert state.status == "empty_pool"
    assert state.ended is True
    # SDK client still torn down exactly once.
    assert fake_client.stop_call_count == 1


def test_driver_owns_driving_and_run_returns_its_result(tmp_path, monkeypatch) -> None:
    _wire_empty_pool_repo(tmp_path, monkeypatch)
    state = LiveRunState()
    driver = _SkippingDriver(state, result=7)

    cfg = RunConfig(
        issue_source="github", max_iterations=1, max_nmt_strikes=3, verbosity=0
    )
    exit_code = asyncio.run(loop_module.run(cfg, driver=driver))

    assert driver.run_called is True
    assert exit_code == 7
    # The driver skipped driving, so the loop never emitted run-start.
    assert state.status == "starting"
