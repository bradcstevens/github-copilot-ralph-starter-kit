"""Tests for ``ralph_afk.interactive.driver`` (issue #23 — peer orchestration).

Exercises the observer control model (ADR-0001) **without a TTY** by injecting a
fake app: the loop and app run as peers; Stop cancels the loop; natural
completion closes the app; a loop crash propagates.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Coroutine

import pytest

from ralph_afk.config import RunConfig
from ralph_afk.interactive.driver import (
    InteractiveDriver,
    build_interactive_driver,
)
from ralph_afk.interactive.state import LiveRunState


class _FakeApp:
    """Stand-in for ``RalphApp``: ``run_async`` blocks until ``exit``."""

    def __init__(self, state: LiveRunState) -> None:
        self.state = state
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

    def factory(s: LiveRunState) -> _SelfStoppingApp:
        app = _SelfStoppingApp(s)
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

    def factory(s: LiveRunState) -> _FakeApp:
        app = _FakeApp(s)
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

    def factory(s: LiveRunState) -> _FakeApp:
        app = _FakeApp(s)
        captured.append(app)
        return app

    driver = InteractiveDriver(state, app_factory=factory)  # type: ignore[arg-type]
    boom = RuntimeError("loop exploded")

    with pytest.raises(RuntimeError, match="loop exploded"):
        asyncio.run(driver.run(_drive_raising(boom)))

    assert captured and captured[0].exited is True


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
