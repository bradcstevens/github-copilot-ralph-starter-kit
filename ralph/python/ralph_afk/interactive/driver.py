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
coroutine-function; it also registers :attr:`InteractiveDriver.state` as the
primary sink and, for #26, attaches the loop-owned Summary/Log pane sources via
:meth:`InteractiveDriver.attach_panes`. Keeping the orchestration here means
:mod:`ralph_afk.loop` never imports Textual.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable, Coroutine

from rich.console import Console

from ralph_afk.config import RunConfig
from ralph_afk.interactive.app import RalphApp
from ralph_afk.interactive.state import LiveRunState
from ralph_afk.sinks import EventSink, SinkFanout

if TYPE_CHECKING:
    from ralph_afk.ui.summary import RunSummary

__all__ = ["InteractiveDriver", "build_interactive_driver"]

#: Factory for the observing app, injected so tests can swap in a fake app and
#: exercise the peering/Stop logic without a TTY. Accepts the state plus the
#: optional loop-owned panes (``summary`` / ``log_source``) attached for #26.
AppFactory = Callable[..., "RalphApp"]


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
        #: Loop-owned panes attached by :func:`ralph_afk.loop.run` (issue #26)
        #: before :meth:`run`: the live run-summary table source and the
        #: captured line-printer log text source. ``None`` until attached.
        self.summary: "RunSummary | None" = None
        self.log_source: Callable[[], str] | None = None
        #: Exit-model handoff attached by :func:`ralph_afk.loop.run` (issue #28)
        #: before :meth:`run`: the swappable :class:`SinkFanout`, the parked
        #: line-printer :class:`~ralph_afk.ui.renderer.Renderer` to swap in on a
        #: **Detach**, and the real stdout console for the **Stop** scrollback
        #: record. ``None`` until attached.
        self._sinks: SinkFanout | None = None
        self._line_printer: EventSink | None = None
        self._console: Console | None = None

    def attach_panes(
        self,
        *,
        summary: "RunSummary | None",
        log_source: Callable[[], str] | None,
    ) -> None:
        """Receive the loop-owned Summary/Log pane sources (issue #26).

        Called by :func:`ralph_afk.loop.run` after it constructs the shared
        :class:`~ralph_afk.ui.summary.RunSummary` and the buffer-backed capture
        renderer, so the app's Summary and Log tabs render the same data the
        line printer would. The loop owns these objects (it also reads
        ``summary`` for persistence); the driver only forwards them to the app.
        """
        self.summary = summary
        self.log_source = log_source

    def attach_detach(
        self,
        *,
        sinks: SinkFanout,
        line_printer: EventSink,
        console: Console,
    ) -> None:
        """Receive the exit-model handoff seam (issue #28).

        Called by :func:`ralph_afk.loop.run` on the interactive path with the
        run's swappable :class:`~ralph_afk.sinks.SinkFanout`, the parked
        line-printer :class:`~ralph_afk.ui.renderer.Renderer` (kept out of the
        sink list while the TUI owns the terminal), and the real stdout console.

        * **Detach** (``d``) swaps ``sinks`` wholesale to ``[line_printer]`` so
          the remainder of the run prints to normal scrollback.
        * **Stop** (``q`` / ``Ctrl+C``) and natural completion print the run-end
          summary table to ``console`` so the terminal is never left blank after
          the TUI tears down.
        """
        self._sinks = sinks
        self._line_printer = line_printer
        self._console = console

    async def run(self, drive: Callable[[], Coroutine[object, object, int]]) -> int:
        """Launch the app + the loop's ``drive`` as peers; return the exit code.

        Three app-exit-first outcomes (issue #28):

        * **Detach** (``app.detach_requested``): swap the live sink back to the
          line printer (:meth:`SinkFanout.set_sinks`) and let the loop run to
          its natural outcome — it keeps printing to scrollback. The loop's own
          run-end table is the scrollback record, so the driver prints nothing.
        * **Stop**: cancel the loop task, mark the state stopped, and (after the
          wind-down) print the run-end summary table to scrollback.
        * natural completion / crash also leave a scrollback record (the table),
          so the TUI never tears down to a blank screen.

        A ``KeyboardInterrupt`` (the *second* ``Ctrl+C``, a real signal once the
        TUI has restored the terminal) is never swallowed — it propagates out of
        the ``gather`` below for an immediate exit. On a user **Stop** the loop
        task is cancelled and ``0`` (clean stop) is returned; on natural
        completion the loop's own exit code is returned; a crash inside the loop
        is re-raised so the caller records it as a non-zero outcome.
        """
        app = self._app_factory(
            self.state, summary=self.summary, log_source=self.log_source
        )

        loop_task: asyncio.Task[int] = asyncio.create_task(
            drive(), name="ralph-afk-loop"
        )
        app_task: asyncio.Task[None] = asyncio.create_task(
            app.run_async(), name="ralph-afk-tui"
        )

        await asyncio.wait(
            {loop_task, app_task}, return_when=asyncio.FIRST_COMPLETED
        )

        detached = False
        if loop_task.done() and not app_task.done():
            # Run finished naturally → close the TUI.
            app.exit()
        elif app_task.done() and not loop_task.done():
            if getattr(app, "detach_requested", False):
                # Detach → swap to the line printer; the loop runs on. The swap
                # is atomic w.r.t. the single-threaded loop's synchronous event
                # dispatch (the loop is suspended at an await here), so no event
                # is dropped or duplicated across the handoff.
                self._detach()
                detached = True
            else:
                # User Stopped from the TUI → wind the loop down cleanly.
                self.state.mark_stopped()
                loop_task.cancel()

        results = await asyncio.gather(
            loop_task, app_task, return_exceptions=True
        )

        # Scrollback-on-exit: unless we detached (the line printer already
        # printed the run, including its own run-end table), echo the run-end
        # summary table so the terminal keeps a permanent textual record.
        if not detached:
            self._print_scrollback_summary()

        loop_result = results[0]
        if isinstance(loop_result, asyncio.CancelledError):
            return 0
        if isinstance(loop_result, BaseException):
            raise loop_result
        return loop_result

    def _detach(self) -> None:
        """Swap the live sink list back to the parked line printer (Detach)."""
        if self._sinks is not None and self._line_printer is not None:
            self._sinks.set_sinks([self._line_printer])

    def _print_scrollback_summary(self) -> None:
        """Write the run-end summary table to normal scrollback (Stop / done)."""
        if self._console is not None and self.summary is not None:
            self._console.print(self.summary.build_run_table())


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
