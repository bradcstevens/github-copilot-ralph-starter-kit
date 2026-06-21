"""``ralph_afk.interactive.driver`` — peer-task orchestration (ADR-0001).

The interactive driver realises the **observer** control model: it launches the
ralph loop and a Textual app as **peer asyncio tasks** (not parent/child) and
waits for whichever finishes first.

* If the **loop** finishes first (the run reached a natural outcome), the app is
  told to exit so the TUI tears down.
* If the **app** finishes first (the user pressed ``q`` / ``Ctrl+C`` — a
  **Stop**), the loop task is cancelled and the run is wound down cleanly.

:func:`ralph_afk.loop.run` holds this object structurally (its ``InteractiveDriver``
Protocol) and calls :meth:`InteractiveDriver.run` with the loop's ``drive``
coroutine-function; it also registers :attr:`InteractiveDriver.state` as the sole
sink. Keeping the orchestration here means :mod:`ralph_afk.loop` never imports
Textual.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Coroutine

from ralph_afk.config import RunConfig
from ralph_afk.interactive.app import RalphApp
from ralph_afk.interactive.state import LiveRunState

__all__ = ["InteractiveDriver", "build_interactive_driver"]

#: Factory for the observing app, injected so tests can swap in a fake app and
#: exercise the peering/Stop logic without a TTY.
AppFactory = Callable[[LiveRunState], "RalphApp"]


class InteractiveDriver:
    """Runs the loop as an observed peer of a Textual app (ADR-0001)."""

    def __init__(
        self,
        state: LiveRunState,
        *,
        app_factory: AppFactory = RalphApp,
    ) -> None:
        self.state = state
        self._app_factory = app_factory

    async def run(self, drive: Callable[[], Coroutine[object, object, int]]) -> int:
        """Launch the app + the loop's ``drive`` as peers; return the exit code.

        On a user **Stop** the loop task is cancelled and ``0`` (clean stop) is
        returned. On natural completion the loop's own exit code is returned and
        the app is closed. A crash inside the loop is re-raised so the caller
        (:func:`ralph_afk.loop.run`) records it as a non-zero outcome.
        """
        app = self._app_factory(self.state)

        loop_task: asyncio.Task[int] = asyncio.create_task(
            drive(), name="ralph-afk-loop"
        )
        app_task: asyncio.Task[None] = asyncio.create_task(
            app.run_async(), name="ralph-afk-tui"
        )

        await asyncio.wait(
            {loop_task, app_task}, return_when=asyncio.FIRST_COMPLETED
        )

        if loop_task.done() and not app_task.done():
            # Run finished naturally → close the TUI.
            app.exit()
        elif app_task.done() and not loop_task.done():
            # User Stopped from the TUI → wind the loop down cleanly.
            self.state.mark_stopped()
            loop_task.cancel()

        results = await asyncio.gather(
            loop_task, app_task, return_exceptions=True
        )

        loop_result = results[0]
        if isinstance(loop_result, asyncio.CancelledError):
            return 0
        if isinstance(loop_result, BaseException):
            raise loop_result
        return loop_result


def build_interactive_driver(config: RunConfig) -> InteractiveDriver:
    """Construct the driver + its :class:`LiveRunState` seeded from ``config``.

    Model id, reasoning effort, and the strike threshold are known up front
    (they come from the frozen :class:`RunConfig`); the rest of the header state
    is learned from events as the loop emits them.
    """
    state = LiveRunState(
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        max_strikes=config.max_nmt_strikes,
    )
    return InteractiveDriver(state)
