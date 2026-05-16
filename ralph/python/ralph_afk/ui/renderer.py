"""``ralph_afk.ui.renderer`` — event-driven terminal renderer.

The :class:`Renderer` consumes one event dict at a time and prints (via
its injected :class:`rich.console.Console`) the corresponding terminal
output. Streaming deltas are filtered out upstream by
:func:`ralph_afk.events.map_sdk_event` so each "print" the renderer
issues is final — no in-place re-draw, no scrollback duplication.

Verbosity ladder:

================  ============================================================
Level             Behaviour
================  ============================================================
0 (default)       Reasoning (if ``render_reasoning=True``), tool calls one-line,
                  wrapper outcomes (commits, auto-closures, strikes, denials).
                  Tool results are dropped. ``session.*`` and
                  ``tool.permission_requested`` events are silent.
1 (``-v``)        Adds tool-result lines: ``size`` for successes,
                  ``error.message`` for failures. No tool-result content
                  body is rendered (the events module does NOT carry it;
                  ``tool.result`` payloads only contain ``result_size_chars``
                  and ``error``).
2 (``-vv``)       Reasoning rendered without truncation cues (deltas are
                  already filtered upstream so this is the same as level 0
                  unless ``render_reasoning=False`` — in which case the
                  toggle wins).
3 (``-vvv``)      Every event gets a raw-dump line in addition to its
                  normal handler. Permission and session events that are
                  normally silent surface here.
================  ============================================================

``render_reasoning=False`` is an explicit operator opt-out and always
wins over the verbosity ladder — ``--no-reasoning -vv`` hides reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from rich.console import Console
from rich.text import Text

from ralph_afk.events import (
    ASSISTANT_MESSAGE,
    ASSISTANT_REASONING,
    SESSION_CREATED,
    SESSION_DELETED,
    SESSION_IDLE,
    TOOL_CALL,
    TOOL_PERMISSION_DENIED,
    TOOL_PERMISSION_REQUESTED,
    TOOL_RESULT,
    USAGE_TOKENS,
    WRAPPER_AFK_READY_COLLECTED,
    WRAPPER_ASK_USER_ATTEMPTED,
    WRAPPER_AUTO_CLOSE,
    WRAPPER_COMMIT_RECORDED,
    WRAPPER_ITERATION_END,
    WRAPPER_ITERATION_START,
    WRAPPER_RUN_END,
    WRAPPER_RUN_START,
    WRAPPER_STALE_WORKTREE_ABORTED,
    WRAPPER_STRIKE,
)

from .console import STYLES
from .summary import RunSummary

__all__ = ["Renderer"]


_THINKING_PREFIX: str = "✻ Thinking:"


@dataclass
class Renderer:
    """Event-driven renderer.

    Drives a :class:`RunSummary` for per-iteration state accumulation and
    prints to an injected :class:`rich.console.Console`. The loop slice
    (#10) owns the lifecycle: construct once, call :meth:`render` per
    event, read :attr:`summary.completed` after :data:`WRAPPER_RUN_END`.

    Attributes:
        console: The :class:`Console` to print to. Tests inject a
            :class:`io.StringIO`-backed console with
            ``force_terminal=False``; production uses
            :func:`get_console`.
        summary: The :class:`RunSummary` accumulator. Owned by the
            caller; the renderer is the writer.
        verbosity: 0-3, mapping to the ladder in the module docstring.
        render_reasoning: When ``False``, reasoning is always suppressed
            regardless of verbosity.
    """

    console: Console
    summary: RunSummary
    verbosity: int = 0
    render_reasoning: bool = True

    def render(self, event: dict[str, Any]) -> None:
        """Dispatch ``event`` to its per-type handler.

        Unknown event types are no-ops at default verbosity and raw-dumped
        at ``-vvv``. An event dict missing a ``type`` key is a no-op
        regardless of verbosity.
        """
        et = event.get("type")
        if not isinstance(et, str):
            return
        handler = _HANDLERS.get(et)
        if handler is None:
            if self.verbosity >= 3:
                self._raw_dump(event)
            return
        handler(self, event)
        if self.verbosity >= 3:
            self._raw_dump(event)

    # -- handler bodies ----------------------------------------------------

    def _on_run_start(self, event: dict[str, Any]) -> None:
        run_id = event.get("run_id", "")
        text = Text()
        text.append("▶ ", style=STYLES["success"])
        text.append("ralph-afk run started", style=STYLES["panel_title"])
        if run_id:
            text.append(f"  (run_id: {run_id})", style=STYLES["meta"])
        self.console.print(text)

    def _on_run_end(self, event: dict[str, Any]) -> None:
        # Render the frozen run-end Table.
        self.console.print(self.summary.build_run_table())
        outcome = event.get("outcome")
        if outcome is not None:
            text = Text()
            text.append("✓ ", style=STYLES["success"])
            text.append(f"run end: {outcome}", style=STYLES["meta"])
            self.console.print(text)

    def _on_iteration_start(self, event: dict[str, Any]) -> None:
        iter_num = int(event.get("iter", 0) or 0)
        issue_num_raw = event.get("issue")
        issue_num: Optional[int]
        try:
            issue_num = int(issue_num_raw) if issue_num_raw is not None else None
        except (TypeError, ValueError):
            issue_num = None
        self.summary.on_iteration_start(iter_num=iter_num, issue_num=issue_num)
        text = Text()
        text.append("── ", style=STYLES["panel_rule"])
        text.append(f"Iteration {iter_num}", style=STYLES["panel_title"])
        if issue_num is not None:
            text.append(f"  •  Issue #{issue_num}", style=STYLES["meta"])
        text.append(" ──", style=STYLES["panel_rule"])
        self.console.print(text)

    def _on_iteration_end(self, event: dict[str, Any]) -> None:
        snap = self.summary.on_iteration_end()
        if snap is None:
            return
        self.console.print(self.summary.build_iteration_panel(snap))

    def _on_afk_ready_collected(self, event: dict[str, Any]) -> None:
        issues = event.get("issues") or []
        count = len(issues) if hasattr(issues, "__len__") else 0
        text = Text()
        text.append("ⓘ ", style=STYLES["meta"])
        text.append("AFK-ready pool: ", style=STYLES["meta"])
        text.append(f"{count} issue", style=STYLES["panel_title"])
        if count != 1:
            text.append("s", style=STYLES["panel_title"])
        if count > 0:
            text.append(
                "  (" + ", ".join(f"#{i}" for i in issues) + ")",
                style=STYLES["meta"],
            )
        self.console.print(text)

    def _on_stale_worktree_aborted(self, event: dict[str, Any]) -> None:
        text = Text()
        text.append("✗ ", style=STYLES["error"])
        text.append(
            "stale worktree — aborting (commit or stash before re-running)",
            style=STYLES["error"],
        )
        self.console.print(text)

    def _on_commit_recorded(self, event: dict[str, Any]) -> None:
        sha = event.get("sha", "")
        subject = event.get("subject", "")
        short = sha[:10] if isinstance(sha, str) else ""
        text = Text()
        text.append("✓ ", style=STYLES["success"])
        text.append("commit ", style=STYLES["meta"])
        text.append(short, style=STYLES["success"])
        if subject:
            # Single-line: collapse any newlines in the subject for the
            # rendered line. The full message is intact in git/log.
            subject_line = str(subject).splitlines()[0] if str(subject).splitlines() else str(subject)
            text.append(f"  {subject_line}", style=STYLES["meta"])
        self.console.print(text)
        self.summary.record_commit()

    def _on_auto_close(self, event: dict[str, Any]) -> None:
        issue = event.get("issue")
        sha = event.get("sha", "")
        short = sha[:10] if isinstance(sha, str) else ""
        text = Text()
        text.append("✓ ", style=STYLES["success"])
        text.append("auto-closed ", style=STYLES["meta"])
        if issue is not None:
            text.append(f"#{issue}", style=STYLES["success"])
        if short:
            text.append(f"  ({short})", style=STYLES["meta"])
        self.console.print(text)
        self.summary.record_auto_close()

    def _on_strike(self, event: dict[str, Any]) -> None:
        strikes_raw = event.get("strikes")
        max_strikes = event.get("max_strikes")
        try:
            strikes_value: Optional[int] = (
                int(strikes_raw) if strikes_raw is not None else None
            )
        except (TypeError, ValueError):
            strikes_value = None
        self.summary.record_strike(strikes=strikes_value)
        snap = self.summary.current
        current_strikes = snap.strikes if snap is not None else (strikes_value or 0)
        text = Text()
        text.append("⚠ ", style=STYLES["warning"])
        text.append("strike ", style=STYLES["warning"])
        if max_strikes is not None:
            text.append(f"{current_strikes}/{max_strikes}", style=STYLES["warning"])
        else:
            text.append(str(current_strikes), style=STYLES["warning"])
        self.console.print(text)

    def _on_ask_user_attempted(self, event: dict[str, Any]) -> None:
        text = Text()
        text.append("⚠ ", style=STYLES["warning"])
        text.append(
            "agent attempted ask_user (disabled in AFK runs)",
            style=STYLES["warning"],
        )
        self.console.print(text)

    def _on_assistant_reasoning(self, event: dict[str, Any]) -> None:
        if not self.render_reasoning:
            return
        content = event.get("content", "")
        if not isinstance(content, str) or not content:
            return
        text = Text()
        text.append(f"{_THINKING_PREFIX} ", style=STYLES["reasoning"])
        text.append(content, style=STYLES["reasoning"])
        self.console.print(text)

    def _on_assistant_message(self, event: dict[str, Any]) -> None:
        content = event.get("content", "")
        if not isinstance(content, str):
            return
        # No in-place re-render: events.map_sdk_event filters deltas out
        # upstream so this is the one and only print for the final message.
        self.console.print(content)

    def _on_tool_call(self, event: dict[str, Any]) -> None:
        tool_name = event.get("tool_name", "")
        arguments = event.get("arguments")
        self.summary.record_tool_call(tool_name=str(tool_name))

        if tool_name == "skill":
            # Magenta highlight; pull the skill name out of arguments.
            skill_name = ""
            if isinstance(arguments, dict):
                raw = arguments.get("skill")
                if isinstance(raw, str):
                    skill_name = raw
            text = Text()
            text.append("◇ ", style=STYLES["skill"])
            text.append("skill ", style=STYLES["meta"])
            if skill_name:
                text.append(skill_name, style=STYLES["skill"])
            else:
                text.append("(unknown)", style=STYLES["meta"])
            self.console.print(text)
            return

        # Default tool-call: cyan one-liner with name + args.
        text = Text()
        text.append("» ", style=STYLES["tool"])
        text.append(str(tool_name), style=STYLES["tool"])
        text.append("  ", style=STYLES["meta"])
        text.append(_format_arguments(arguments), style=STYLES["meta"])
        self.console.print(text)

    def _on_tool_result(self, event: dict[str, Any]) -> None:
        if self.verbosity < 1:
            return  # Silent at default verbosity.
        success = bool(event.get("success", False))
        size = event.get("result_size_chars")
        err = event.get("error")
        text = Text()
        if success:
            text.append("← ", style=STYLES["success"])
            text.append("result", style=STYLES["meta"])
            if size is not None:
                text.append(f"  ({size} chars)", style=STYLES["meta"])
        else:
            text.append("← ", style=STYLES["error"])
            text.append("error", style=STYLES["error"])
            if isinstance(err, dict):
                msg = err.get("message")
                if msg:
                    text.append(f"  {msg}", style=STYLES["error"])
        self.console.print(text)

    def _on_usage_tokens(self, event: dict[str, Any]) -> None:
        model = event.get("model")
        tokens_in = int(event.get("input", 0) or 0)
        tokens_out = int(event.get("output", 0) or 0)
        self.summary.record_usage(
            model=model if isinstance(model, str) else None,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        # No live ticker — accumulated silently. The frozen iteration
        # Panel surfaces the totals at iteration end.

    def _on_tool_permission_requested(self, event: dict[str, Any]) -> None:
        # Silent at default; raw-dumped at -vvv via the main dispatcher.
        return

    def _on_tool_permission_denied(self, event: dict[str, Any]) -> None:
        tool_name = event.get("tool_name", "")
        reason = event.get("reason", "")
        text = Text()
        text.append("⊘ ", style=STYLES["warning"])
        text.append("denied ", style=STYLES["warning"])
        text.append(str(tool_name), style=STYLES["warning"])
        if reason:
            text.append(f"  ({reason})", style=STYLES["meta"])
        self.console.print(text)

    def _on_session_created(self, event: dict[str, Any]) -> None:
        # Silent at default; raw-dumped at -vvv.
        return

    def _on_session_idle(self, event: dict[str, Any]) -> None:
        # Silent at default; raw-dumped at -vvv.
        return

    def _on_session_deleted(self, event: dict[str, Any]) -> None:
        # Silent at default; raw-dumped at -vvv.
        return

    # -- internal helpers --------------------------------------------------

    def _raw_dump(self, event: dict[str, Any]) -> None:
        """Render an event as a single dim line for ``-vvv`` mode."""
        text = Text()
        et = event.get("type", "?")
        text.append("· ", style=STYLES["meta"])
        text.append(str(et), style=STYLES["meta"])
        # Append a compact representation of the remaining fields.
        remainder = {k: v for k, v in event.items() if k != "type"}
        if remainder:
            text.append("  ", style=STYLES["meta"])
            text.append(repr(remainder), style=STYLES["meta"])
        self.console.print(text)


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


_MAX_ARG_DISPLAY: int = 200


def _format_arguments(arguments: Any) -> str:
    """Format ``arguments`` for the cyan tool-call one-liner.

    The scrubber has already truncated oversize argument bundles to the
    literal ``<truncated: N chars>`` string; the renderer just prints
    whatever it receives. A dict is rendered as ``key=value`` pairs
    (compact, scrollback-friendly). Strings, lists, and other types are
    coerced to ``str()``.
    """
    if isinstance(arguments, dict):
        if not arguments:
            return "(no args)"
        parts = [f"{k}={_short_repr(v)}" for k, v in arguments.items()]
        joined = "  ".join(parts)
        if len(joined) > _MAX_ARG_DISPLAY:
            joined = joined[: _MAX_ARG_DISPLAY] + "…"
        return joined
    if arguments is None:
        return "(no args)"
    return str(arguments)


def _short_repr(value: Any) -> str:
    if isinstance(value, str):
        return value
    return repr(value)


# ---------------------------------------------------------------------------
# Dispatch table — keyed on event-type literals from ``ralph_afk.events``
# ---------------------------------------------------------------------------


# A plain dict beats ``match/case`` for forward-compat: unknown types
# slip through to the ``_raw_dump`` path at high verbosity.
_HANDLERS: dict[str, Callable[[Renderer, dict[str, Any]], None]] = {
    WRAPPER_RUN_START: Renderer._on_run_start,
    WRAPPER_RUN_END: Renderer._on_run_end,
    WRAPPER_ITERATION_START: Renderer._on_iteration_start,
    WRAPPER_ITERATION_END: Renderer._on_iteration_end,
    WRAPPER_AFK_READY_COLLECTED: Renderer._on_afk_ready_collected,
    WRAPPER_STALE_WORKTREE_ABORTED: Renderer._on_stale_worktree_aborted,
    WRAPPER_COMMIT_RECORDED: Renderer._on_commit_recorded,
    WRAPPER_AUTO_CLOSE: Renderer._on_auto_close,
    WRAPPER_STRIKE: Renderer._on_strike,
    WRAPPER_ASK_USER_ATTEMPTED: Renderer._on_ask_user_attempted,
    ASSISTANT_REASONING: Renderer._on_assistant_reasoning,
    ASSISTANT_MESSAGE: Renderer._on_assistant_message,
    TOOL_CALL: Renderer._on_tool_call,
    TOOL_RESULT: Renderer._on_tool_result,
    USAGE_TOKENS: Renderer._on_usage_tokens,
    TOOL_PERMISSION_REQUESTED: Renderer._on_tool_permission_requested,
    TOOL_PERMISSION_DENIED: Renderer._on_tool_permission_denied,
    SESSION_CREATED: Renderer._on_session_created,
    SESSION_IDLE: Renderer._on_session_idle,
    SESSION_DELETED: Renderer._on_session_deleted,
}
