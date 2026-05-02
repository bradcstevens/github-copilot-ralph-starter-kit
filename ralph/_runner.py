"""Shared SDK runner + rich-based event formatter for the ralph scripts.

This module wraps the GitHub Copilot Python SDK (``github-copilot-sdk``) so
the ralph scripts can stream the same fidelity a user would see when
running the ``copilot`` CLI directly: live reasoning, tool calls with
arguments, tool results, compaction notices, and per-turn usage stats.

The CLI scripts (``ralph.afk`` / ``ralph.grill``) own a single
``CopilotClient`` for the entire process lifetime, so we never need to
``resume_session`` — we keep ``CopilotSession`` objects alive across
phases and call ``send_and_wait`` repeatedly on them.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from copilot import CopilotClient, CopilotSession
from copilot.generated.session_events import SessionEvent, SessionEventType
from copilot.session import PermissionHandler
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


DEFAULT_MODEL = os.environ.get("MODEL") or "claude-opus-4.7-1m-internal"
DEFAULT_EFFORT = os.environ.get("EFFORT") or "xhigh"
DEFAULT_TURN_TIMEOUT = float(os.environ.get("RALPH_TURN_TIMEOUT", "1800"))


# ---------------------------------------------------------------------------
# Pretty-printing helpers
# ---------------------------------------------------------------------------


def _short_args(args: Any, *, limit: int = 120) -> str:
    """Render ``args`` (dict/list/scalar) as a compact one-liner for headers."""

    if args is None:
        return ""
    try:
        if isinstance(args, str):
            text = args
        else:
            text = json.dumps(args, ensure_ascii=False, default=str, separators=(", ", ": "))
    except Exception:
        text = str(args)
    text = text.replace("\n", " ").replace("\r", " ")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


_SECRETISH_KEYS = re.compile(
    r"(?:^|[_\-])(?:password|passwd|secret|token|api[_\-]?key|apikey|"
    r"access[_\-]?key|authorization|auth|credential|cookie|session[_\-]?id)",
    re.IGNORECASE,
)


def _redact(args: Any) -> Any:
    """Return a copy of ``args`` with values for sensitive-looking keys masked."""

    if isinstance(args, dict):
        out: dict[str, Any] = {}
        for key, value in args.items():
            if isinstance(key, str) and _SECRETISH_KEYS.search(key):
                out[key] = "<redacted>"
            else:
                out[key] = _redact(value)
        return out
    if isinstance(args, list):
        return [_redact(v) for v in args]
    return args


_CWD = os.getcwd()


def _format_path(value: Any) -> Any:
    """Convert absolute paths under the cwd to short relative paths.

    Keeps relative or non-string values unchanged. Mirrors the way the
    interactive ``copilot`` CLI prefers cwd-relative renderings so the
    column doesn't blow out for nested project trees.
    """

    if not isinstance(value, str) or not value or not os.path.isabs(value):
        return value
    try:
        rel = os.path.relpath(value, _CWD)
    except ValueError:
        return value
    if rel.startswith(".."):
        return value
    return rel


def _arg_preview(tool_name: str, args: Any) -> str:
    """Tool-aware short preview (mirrors the CLI's TUI render)."""

    if not isinstance(args, dict):
        return _short_args(args)
    args = _redact(args)

    # Common patterns: bash/shell commands, file paths, search patterns.
    for key in ("command", "cmd", "shellId", "path", "file_path", "filename", "uri"):
        if key in args and isinstance(args[key], str):
            value = args[key]
            if key in ("path", "file_path", "filename", "uri"):
                value = _format_path(value)
            view_range = args.get("view_range")
            if isinstance(view_range, (list, tuple)) and len(view_range) >= 2:
                value = f"{value}:{view_range[0]}-{view_range[1]}"
            return _short_args(value)
    if "pattern" in args:
        pattern = args.get("pattern")
        glob_pat = args.get("glob") or args.get("type")
        if glob_pat:
            return _short_args(f"{pattern}  in  {glob_pat}")
        return _short_args(pattern)
    if "old_str" in args and "new_str" in args:
        return _short_args(_format_path(args.get("path") or args.get("file_path") or "edit"))
    return _short_args(args)


def _flatten_result(result: Any) -> str:
    """Extract human-readable text from a ToolExecutionComplete ``result``."""

    if result is None:
        return ""
    if isinstance(result, str):
        return result
    # SDK Result dataclass exposes ``content`` / ``detailed_content``
    content = getattr(result, "content", None) or getattr(result, "detailed_content", None)
    if isinstance(content, str):
        return content
    if isinstance(result, dict):
        for key in ("content", "detailed_content", "output", "stdout", "result"):
            value = result.get(key)
            if isinstance(value, str):
                return value
        return _short_args(result, limit=600)
    return str(result)


def _truncate_block(text: str, *, max_lines: int = 6, max_chars: int = 400) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    if truncated:
        text = text.rstrip() + "\n…"
    return text


# ---------------------------------------------------------------------------
# Stats accumulation
# ---------------------------------------------------------------------------


@dataclass
class UsageTotals:
    input_tokens: float = 0.0
    output_tokens: float = 0.0
    cache_read_tokens: float = 0.0
    cache_write_tokens: float = 0.0
    cost: float = 0.0
    api_calls: int = 0
    api_duration_ms: float = 0.0
    premium_requests: float = 0.0
    models: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    last_context_tokens: float | None = None
    last_context_messages: float | None = None

    def add_usage_event(self, data: Any) -> None:
        self.api_calls += 1
        for attr in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cost",
        ):
            value = getattr(data, attr, None)
            if isinstance(value, (int, float)):
                setattr(self, attr, getattr(self, attr) + float(value))
        duration = getattr(data, "duration", None) or getattr(data, "api_duration_ms", None)
        if isinstance(duration, (int, float)):
            self.api_duration_ms += float(duration)
        usage = getattr(data, "copilot_usage", None)
        if usage is not None:
            premium = getattr(usage, "premium_requests", None)
            if isinstance(premium, (int, float)):
                self.premium_requests += float(premium)
        model = getattr(data, "model", None)
        if isinstance(model, str) and model:
            self.models[model] += 1

    def add_context_info(self, data: Any) -> None:
        for attr in ("current_tokens", "currentTokens"):
            value = getattr(data, attr, None)
            if isinstance(value, (int, float)):
                self.last_context_tokens = float(value)
                break
        msgs = getattr(data, "messages_length", None)
        if isinstance(msgs, (int, float)):
            self.last_context_messages = float(msgs)


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


_NONE = ""
_BLOCK_REASONING = "reasoning"
_BLOCK_ASSISTANT = "assistant"
_BLOCK_TOOL = "tool"


class Formatter:
    """Stateful event handler that prints rich-formatted output to a Console.

    Branches on ``event.type`` (``SessionEventType`` enum) and reads the
    appropriate optional fields off ``event.data``. Tracks the "current
    block" so consecutive deltas of the same kind don't repeat headers.
    """

    def __init__(
        self,
        console: Console,
        *,
        totals: UsageTotals | None = None,
        verbose: bool = False,
    ) -> None:
        self.console = console
        self.totals = totals if totals is not None else UsageTotals()
        self.verbose = verbose
        self._block: str = _NONE
        self._tool_calls: dict[str, dict[str, Any]] = {}
        self._tool_started_at: dict[str, float] = {}
        self._reasoning_started: bool = False
        self._assistant_started: bool = False
        self._last_assistant_message: str = ""

    # -- public --------------------------------------------------------------

    def on_event(self, event: SessionEvent) -> None:
        try:
            self._dispatch(event)
        except Exception as exc:  # pragma: no cover - defensive
            self._end_block()
            self.console.print(f"[red]formatter error:[/red] {escape(str(exc))}")

    @property
    def last_assistant_message(self) -> str:
        return self._last_assistant_message

    def render_final_stats(self, *, title: str = "Session stats") -> None:
        self._end_block()
        totals = self.totals
        if totals.api_calls == 0 and totals.last_context_tokens is None:
            return

        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim")
        table.add_column()

        def fmt_int(value: float) -> str:
            return f"{int(round(value)):,}"

        if totals.api_calls:
            table.add_row("API calls", fmt_int(totals.api_calls))
            table.add_row(
                "Input tokens",
                f"{fmt_int(totals.input_tokens)}"
                + (
                    f"  (cache read {fmt_int(totals.cache_read_tokens)},"
                    f" write {fmt_int(totals.cache_write_tokens)})"
                    if totals.cache_read_tokens or totals.cache_write_tokens
                    else ""
                ),
            )
            table.add_row("Output tokens", fmt_int(totals.output_tokens))
            if totals.api_duration_ms:
                table.add_row("API duration", f"{totals.api_duration_ms / 1000:.1f}s")
            if totals.premium_requests:
                table.add_row("Premium requests", f"{totals.premium_requests:.2f}")
            if totals.cost:
                table.add_row("Cost (USD)", f"${totals.cost:.4f}")
            if totals.models:
                models = ", ".join(f"{m}×{c}" for m, c in totals.models.items())
                table.add_row("Models", models)
        if totals.last_context_tokens is not None:
            table.add_row("Context window", fmt_int(totals.last_context_tokens))
        if totals.last_context_messages is not None:
            table.add_row("Messages in context", fmt_int(totals.last_context_messages))

        self.console.print(Panel(table, title=title, border_style="dim", expand=False))

    # -- dispatch ------------------------------------------------------------

    def _dispatch(self, event: SessionEvent) -> None:  # noqa: C901 - intentional fan-out
        etype = event.type
        data = event.data

        # Reasoning ----------------------------------------------------------
        if etype is SessionEventType.ASSISTANT_REASONING_DELTA:
            self._begin_reasoning()
            chunk = getattr(data, "delta_content", None)
            if isinstance(chunk, str) and chunk:
                self.console.out(chunk, style="grey62", highlight=False, end="")
            return
        if etype is SessionEventType.ASSISTANT_REASONING:
            content = getattr(data, "content", None)
            if isinstance(content, str) and content and not self._reasoning_started:
                self._begin_reasoning()
                self.console.out(content, style="grey62", highlight=False, end="")
            self._reasoning_started = False
            return
        if etype is SessionEventType.ASSISTANT_INTENT:
            intent = getattr(data, "intent", None)
            if isinstance(intent, str) and intent:
                self._end_block()
                self.console.print(Text(f"› {intent}", style="dim italic"))
            return

        # Assistant message --------------------------------------------------
        if etype is SessionEventType.ASSISTANT_MESSAGE_DELTA:
            self._begin_assistant()
            chunk = getattr(data, "delta_content", None)
            if isinstance(chunk, str) and chunk:
                self.console.out(chunk, highlight=False, end="")
                self._last_assistant_message += chunk
            return
        if etype is SessionEventType.ASSISTANT_MESSAGE:
            content = getattr(data, "content", None)
            if isinstance(content, str):
                if not self._assistant_started:
                    if content:
                        self._begin_assistant()
                        self.console.out(content, highlight=False, end="")
                    self._last_assistant_message = content
                else:
                    # We already streamed deltas; trust the final aggregated content.
                    if content and content != self._last_assistant_message:
                        self._last_assistant_message = content
            self._assistant_started = False
            return

        # Tools --------------------------------------------------------------
        if etype is SessionEventType.TOOL_EXECUTION_START:
            self._handle_tool_start(data)
            return
        if etype is SessionEventType.TOOL_EXECUTION_PARTIAL_RESULT:
            partial = getattr(data, "partial_output", None)
            if isinstance(partial, str) and partial:
                for raw in partial.splitlines() or [partial]:
                    self.console.print(Text(f"  {raw}", style="dim"))
            return
        if etype is SessionEventType.TOOL_EXECUTION_PROGRESS:
            self._handle_tool_progress(data)
            return
        if etype is SessionEventType.TOOL_EXECUTION_COMPLETE:
            self._handle_tool_complete(data)
            return

        # Permissions --------------------------------------------------------
        if etype is SessionEventType.PERMISSION_REQUESTED:
            # We always auto-approve via PermissionHandler.approve_all, so
            # surfacing this event would be pure noise. Drop it.
            return

        # Session lifecycle / compaction / usage -----------------------------
        if etype is SessionEventType.ASSISTANT_USAGE:
            self.totals.add_usage_event(data)
            return
        if etype is SessionEventType.SESSION_USAGE_INFO:
            self.totals.add_context_info(data)
            return
        if etype is SessionEventType.SESSION_COMPACTION_START:
            pre = getattr(data, "pre_compaction_tokens", None)
            self._end_block()
            text = "▼ Compacting conversation…"
            if isinstance(pre, (int, float)):
                text += f"  ({int(pre):,} tokens)"
            self.console.print(Text(text, style="dim"))
            return
        if etype is SessionEventType.SESSION_COMPACTION_COMPLETE:
            success = getattr(data, "success", None)
            removed = getattr(data, "tokens_removed", None)
            self._end_block()
            if success is False:
                self.console.print(Text("▲ Compaction failed", style="yellow"))
            else:
                msg = "▲ Compaction complete"
                if isinstance(removed, (int, float)) and removed:
                    msg += f"  (-{int(removed):,} tokens)"
                self.console.print(Text(msg, style="dim"))
            return
        if etype is SessionEventType.SESSION_INFO:
            message = getattr(data, "message", None)
            if isinstance(message, str) and message:
                self._end_block()
                self.console.print(Text(f"· {message}", style="dim"))
            return
        if etype is SessionEventType.SESSION_WARNING:
            message = getattr(data, "message", None)
            if isinstance(message, str) and message:
                self._end_block()
                self.console.print(Text(f"⚠ {message}", style="yellow"))
            return
        if etype is SessionEventType.SESSION_ERROR:
            message = getattr(data, "message", None) or "session error"
            self._end_block()
            self.console.print(Text(f"✗ {message}", style="bold red"))
            return
        if etype is SessionEventType.ABORT:
            reason = getattr(data, "reason", None) or "aborted"
            self._end_block()
            self.console.print(Text(f"⚠ turn aborted: {reason}", style="yellow"))
            return
        if etype is SessionEventType.SESSION_IDLE:
            background = getattr(data, "background_tasks", None)
            self._end_block()
            if background:
                self.console.print(
                    Text(f"  (idle; background tasks pending)", style="dim")
                )
            return

        # Sub-agents ---------------------------------------------------------
        if etype is SessionEventType.SUBAGENT_STARTED:
            name = getattr(data, "name", None) or "sub-agent"
            self._end_block()
            self.console.print(Text(f"  ↳ start sub-agent: {name}", style="cyan dim"))
            return
        if etype is SessionEventType.SUBAGENT_COMPLETED:
            self._end_block()
            self.console.print(Text("  ↳ sub-agent complete", style="cyan dim"))
            return
        if etype is SessionEventType.SUBAGENT_FAILED:
            error = getattr(data, "error", None) or "failed"
            self._end_block()
            self.console.print(Text(f"  ↳ sub-agent failed: {error}", style="red"))
            return

        # Other events are intentionally swallowed; in verbose mode log them
        # so users can see exactly which SDK events the formatter is dropping.
        if self.verbose:
            self._end_block()
            type_name = getattr(etype, "name", str(etype))
            self.console.print(Text(f"  [event] {type_name}", style="dim"))
            self._render_event_fields(data, indent="    ")

    # -- block transitions ---------------------------------------------------

    def _begin_reasoning(self) -> None:
        if self._block != _BLOCK_REASONING:
            self._end_block()
            self.console.print(Text("● Thinking", style="bold yellow"))
            self._block = _BLOCK_REASONING
            self._reasoning_started = True

    def _begin_assistant(self) -> None:
        if self._block != _BLOCK_ASSISTANT:
            self._end_block()
            if not self._assistant_started:
                self._last_assistant_message = ""
            self.console.print(Text("● Assistant", style="bold green"))
            self._block = _BLOCK_ASSISTANT
            self._assistant_started = True

    def _end_block(self) -> None:
        if self._block in (_BLOCK_REASONING, _BLOCK_ASSISTANT):
            self.console.print()
        self._block = _NONE

    # -- tool helpers --------------------------------------------------------

    def _handle_tool_start(self, data: Any) -> None:
        call_id = getattr(data, "tool_call_id", None) or ""
        tool_name = getattr(data, "tool_name", None) or "(tool)"
        args = getattr(data, "arguments", None)
        mcp_server = getattr(data, "mcp_server_name", None)
        mcp_tool = getattr(data, "mcp_tool_name", None)
        description = getattr(data, "description", None)

        self._tool_calls[call_id] = {
            "tool_name": tool_name,
            "arguments": args,
            "mcp_server_name": mcp_server,
            "mcp_tool_name": mcp_tool,
            "description": description,
        }
        self._tool_started_at[call_id] = time.monotonic()

        self._end_block()
        self._block = _BLOCK_TOOL
        label = tool_name
        if mcp_server and mcp_tool:
            label = f"{mcp_server}::{mcp_tool}"
        preview = _arg_preview(tool_name, args)
        header = Text("● ", style="bold cyan")
        header.append(label, style="bold cyan")
        if preview:
            header.append(f"({preview})", style="cyan")
        self.console.print(header)
        if isinstance(description, str) and description:
            self.console.print(Text(f"  {description}", style="dim"))
        if self.verbose and args is not None:
            try:
                pretty = json.dumps(
                    _redact(args), indent=2, ensure_ascii=False, default=str
                )
            except Exception:
                pretty = repr(_redact(args))
            for line in pretty.splitlines():
                self.console.print(Text(f"    {line}", style="dim"))

    def _handle_tool_progress(self, data: Any) -> None:
        message = getattr(data, "progress_message", None)
        if isinstance(message, str) and message:
            self.console.print(Text(f"  · {message}", style="dim"))

    def _handle_tool_complete(self, data: Any) -> None:
        call_id = getattr(data, "tool_call_id", None) or ""
        success = getattr(data, "success", None)
        info = self._tool_calls.pop(call_id, {})
        started_at = self._tool_started_at.pop(call_id, None)
        duration = (time.monotonic() - started_at) if started_at else None
        tool_name = info.get("tool_name") or "(tool)"

        glyph_style = "green" if success is not False else "red"
        glyph = "✓" if success is not False else "✗"

        result_text = _flatten_result(getattr(data, "result", None))
        error_text = ""
        if success is False:
            err = getattr(data, "error", None) or getattr(data, "message", None)
            if isinstance(err, str):
                error_text = err

        # Header with duration
        suffix = ""
        if duration is not None:
            suffix = f"  ({duration:.1f}s)"
        line = Text(f"  {glyph} {tool_name}{suffix}", style=glyph_style)
        self.console.print(line)

        # On success, the agent already has the result; the human watching
        # the loop just needs to see what action ran, not the file/grep/bash
        # contents that produced it. On failure, surface whatever context the
        # SDK provided; if nothing, say so explicitly so the failure isn't
        # silent. Verbose mode adds a full event-data field dump.
        if success is False:
            body = error_text or result_text
            if body:
                body = _truncate_block(body)
                for raw in body.splitlines():
                    self.console.print(Text(f"    {raw}", style="red"))
            else:
                self.console.print(
                    Text(
                        "    (SDK reported failure with no error message/result)",
                        style="red dim",
                    )
                )
            if self.verbose:
                self._render_event_fields(
                    data,
                    indent="    ",
                    style="red dim",
                    skip={"error", "message", "result", "content", "detailed_content"},
                )
        elif self.verbose and result_text:
            self.console.print(
                Text(f"    → {len(result_text):,} chars in result", style="dim")
            )
        self._block = _NONE

    # -- verbose helpers -----------------------------------------------------

    def _render_event_fields(
        self,
        data: Any,
        *,
        indent: str = "    ",
        style: str = "dim",
        skip: set[str] | None = None,
    ) -> None:
        """Dump readable fields on an SDK event-data object (verbose mode).

        Uses ``vars()`` only — avoids ``dir()``-based enumeration which can
        trigger property side effects or expose noisy internals. Values are
        passed through ``_redact()`` so secret-named keys never print.
        """

        skip = skip or set()
        try:
            fields = dict(vars(data))
        except TypeError:
            self.console.print(
                Text(
                    f"{indent}(no introspectable fields on {type(data).__name__})",
                    style=style,
                )
            )
            return
        if not fields:
            return
        redacted = _redact(fields)
        for key in sorted(redacted):
            if key.startswith("_") or key in skip:
                continue
            value = redacted[key]
            if callable(value):
                continue
            try:
                rendered = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                rendered = repr(value)
            if len(rendered) > 200:
                rendered = rendered[:199] + "…"
            self.console.print(Text(f"{indent}{key}={rendered}", style=style))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class TurnResult:
    """Result of a single ``send_and_wait`` turn."""

    text: str
    """The aggregated final assistant message text (post-stream)."""


class Runner:
    """Wraps ``CopilotClient`` lifecycle and creates formatter-attached sessions.

    Designed to be used inside ``async with Runner(...) as runner:`` so the
    underlying ``copilot`` subprocess is always cleaned up.
    """

    def __init__(
        self,
        *,
        console: Console | None = None,
        model: str = DEFAULT_MODEL,
        effort: str | None = DEFAULT_EFFORT,
        client_name: str = "ralph",
        turn_timeout: float = DEFAULT_TURN_TIMEOUT,
        totals: UsageTotals | None = None,
        verbose: bool = False,
    ) -> None:
        self.console = console or Console(highlight=False, soft_wrap=True)
        self.model = model
        self.effort = effort
        self.client_name = client_name
        self.turn_timeout = turn_timeout
        self.totals = totals or UsageTotals()
        self.verbose = verbose
        self._client: CopilotClient | None = None
        self._effort_for_model: dict[str, str | None] = {}

    async def __aenter__(self) -> "Runner":
        self.console.print(Text("· Connecting to Copilot CLI…", style="dim"))
        self._client = CopilotClient()
        await self._client.start()
        # Pre-flight: figure out which effort (if any) is valid for the model.
        try:
            models = await self._client.list_models()
        except Exception:
            models = []
        for info in models:
            allowed = info.supported_reasoning_efforts or []
            if not allowed:
                self._effort_for_model[info.id] = None
                continue
            requested = (self.effort or "").lower() or None
            if requested and requested in allowed:
                self._effort_for_model[info.id] = requested
            else:
                self._effort_for_model[info.id] = info.default_reasoning_effort

        effective_effort = self._effort_for_model.get(self.model, self.effort)
        if self.effort and effective_effort != self.effort:
            note = f"  (requested '{self.effort}', model accepts '{effective_effort or 'none'}')"
        else:
            note = ""
        effort_str = effective_effort or "default"
        model_count = len(models)
        self.console.print(
            Text(
                f"· Connected. {model_count} model(s) available. "
                f"Using {self.model} (effort: {effort_str}){note}",
                style="dim",
            )
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await asyncio.shield(asyncio.wait_for(client.stop(), timeout=10.0))
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            try:
                force_stop = getattr(client, "force_stop", None)
                if callable(force_stop):
                    await asyncio.shield(asyncio.wait_for(force_stop(), timeout=5.0))
            except Exception:
                pass

    # -- session management --------------------------------------------------

    @asynccontextmanager
    async def new_session(
        self,
        *,
        session_id: str | None = None,
        title: str | None = None,
    ) -> AsyncIterator[tuple[CopilotSession, Formatter]]:
        """Create a fresh session, attach a formatter, yield ``(session, formatter)``.

        On exit, disconnects the session. The formatter accumulates into
        ``self.totals`` so multi-session phases share running stats.
        """

        if self._client is None:
            raise RuntimeError("Runner is not started; use 'async with Runner()'")
        client = self._client

        formatter = Formatter(self.console, totals=self.totals, verbose=self.verbose)
        kwargs: dict[str, Any] = {
            "on_permission_request": PermissionHandler.approve_all,
            "model": self.model,
            "client_name": self.client_name,
            "streaming": True,
            "on_event": formatter.on_event,
        }
        effort = self._effort_for_model.get(self.model, self.effort)
        if effort:
            kwargs["reasoning_effort"] = effort
        if session_id:
            kwargs["session_id"] = session_id

        if title:
            self.console.rule(f"[bold]{escape(title)}[/bold]", style="cyan")

        session: CopilotSession | None = None
        try:
            session = await client.create_session(**kwargs)
            yield session, formatter
        finally:
            if session is not None:
                try:
                    await session.disconnect()
                except Exception:
                    pass

    async def run_turn(
        self,
        session: CopilotSession,
        formatter: Formatter,
        prompt: str,
        *,
        timeout: float | None = None,
    ) -> TurnResult:
        """Send ``prompt`` and wait until the session is idle.

        Returns the final assistant message text (preferring the
        formatter's streamed accumulation, falling back to the SDK's
        ``send_and_wait`` return value).
        """

        formatter._last_assistant_message = ""
        timeout_s = timeout if timeout is not None else self.turn_timeout
        final_event = await session.send_and_wait(prompt, timeout=timeout_s)
        text = formatter.last_assistant_message
        if not text and final_event is not None:
            content = getattr(final_event.data, "content", None)
            if isinstance(content, str):
                text = content
        return TurnResult(text=text or "")
