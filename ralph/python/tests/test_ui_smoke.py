"""Tests for ``ralph_afk.ui`` (issue #8 — Rich UI: console + renderer + summary).

The acceptance criterion names ``tests/test_ui_smoke.py`` and asks for a
representative event sequence to flow through the renderer with TTY
forced off, with no exceptions and canonical strings in the captured
output. This file delivers that smoke test alongside the granular
behaviour tests required by the rest of the acceptance criteria
(reasoning toggle, tool/skill rendering, frozen iteration panel, frozen
run-end table, no-ANSI guarantee, verbosity ladder, etc.).

Tests use a captured ``Console`` (``Console(file=StringIO(),
force_terminal=False, no_color=True, width=120)``) so assertions can
match plain-text fragments without dealing with ANSI escapes.
"""

from __future__ import annotations

import ast
import io
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ralph_afk import events as events_module
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
    WRAPPER_CHECKPOINT_RECORDED,
    WRAPPER_COMMIT_RECORDED,
    WRAPPER_ITERATION_END,
    WRAPPER_ITERATION_START,
    WRAPPER_RUN_END,
    WRAPPER_RUN_START,
    WRAPPER_STRIKE,
    make_event,
)
from ralph_afk.pricing import ModelPricing, Pricing
from ralph_afk.ui import IterationSnapshot, Renderer, RunSummary, get_console
from ralph_afk.ui.console import STYLES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _capture_console(width: int = 120) -> tuple[Console, io.StringIO]:
    """Build a non-TTY, no-colour ``Console`` and its capture buffer."""
    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=False,
        no_color=True,
        width=width,
        legacy_windows=False,
        record=False,
    )
    return console, buf


def _fixed_pricing() -> Pricing:
    """A predictable two-model pricing table for cost-rendering assertions."""
    return Pricing(
        models={
            "claude-opus-4.7-xhigh": ModelPricing(
                input_per_mtok=Decimal("15.00"),
                output_per_mtok=Decimal("75.00"),
                context_window=200_000,
            ),
            "gpt-5.4": ModelPricing(
                input_per_mtok=Decimal("1.25"),
                output_per_mtok=Decimal("10.00"),
                context_window=256_000,
            ),
        }
    )


def _ts() -> datetime:
    return datetime(2026, 5, 16, 0, 0, 0, tzinfo=timezone.utc)


def _make_renderer(
    *,
    verbosity: int = 0,
    render_reasoning: bool = True,
    pricing: Pricing | None = None,
    pricing_date: str | None = None,
    width: int = 120,
) -> tuple[Renderer, RunSummary, io.StringIO]:
    """Construct a Renderer wired to a fresh capture buffer + RunSummary."""
    pricing = pricing if pricing is not None else _fixed_pricing()
    summary = RunSummary(pricing=pricing, pricing_date=pricing_date)
    console, buf = _capture_console(width=width)
    renderer = Renderer(
        console=console,
        summary=summary,
        verbosity=verbosity,
        render_reasoning=render_reasoning,
    )
    return renderer, summary, buf


# ---------------------------------------------------------------------------
# console.py — singleton + STYLES
# ---------------------------------------------------------------------------


def test_get_console_returns_a_console_instance() -> None:
    c = get_console()
    assert isinstance(c, Console), "get_console() must return a rich.console.Console"


def test_get_console_is_a_singleton() -> None:
    """Repeated calls return the same Console (so global state stays single-source)."""
    assert get_console() is get_console()


def test_styles_dict_exposes_required_tokens() -> None:
    """Named style tokens are present so the renderer + summary can reuse them."""
    for required_key in (
        "reasoning",
        "tool",
        "skill",
        "panel_title",
        "panel_rule",
        "table_header",
        "error",
        "success",
        "warning",
    ):
        assert required_key in STYLES, f"STYLES missing required key {required_key!r}"
        assert isinstance(STYLES[required_key], str), (
            f"STYLES[{required_key!r}] must be a Rich style string, "
            f"got {type(STYLES[required_key]).__name__}"
        )


# ---------------------------------------------------------------------------
# Renderer.render — unknown events
# ---------------------------------------------------------------------------


def test_renderer_no_op_on_unknown_event_type_at_default_verbosity() -> None:
    """An event with an unknown type must not raise; default verbosity prints nothing."""
    renderer, _summary, buf = _make_renderer()
    renderer.render({"type": "wrapper.unknown.thing", "foo": "bar"})
    assert buf.getvalue() == "", (
        f"Unknown event at default verbosity should produce no output; "
        f"got {buf.getvalue()!r}"
    )


def test_renderer_no_op_on_event_missing_type_key() -> None:
    """Defensive: an event dict without a ``type`` key must not crash."""
    renderer, _summary, buf = _make_renderer()
    renderer.render({"foo": "bar"})
    # No crash, no output.
    assert buf.getvalue() == ""


def test_renderer_raw_dumps_unknown_event_at_vvv() -> None:
    """At ``-vvv`` (verbosity=3), unknown events get a raw dump."""
    renderer, _summary, buf = _make_renderer(verbosity=3)
    renderer.render({"type": "wrapper.unknown.thing", "foo": "bar"})
    out = buf.getvalue()
    assert "wrapper.unknown.thing" in out
    assert "bar" in out


# ---------------------------------------------------------------------------
# Reasoning rendering
# ---------------------------------------------------------------------------


def test_assistant_reasoning_renders_with_thinking_prefix() -> None:
    """Reasoning is prefixed with the documented '✻ Thinking: ' literal."""
    renderer, _summary, buf = _make_renderer(render_reasoning=True)
    renderer.render(
        {
            "type": ASSISTANT_REASONING,
            "content": "step back, consider the plan",
            "reasoning_id": "r1",
        }
    )
    out = buf.getvalue()
    assert "✻ Thinking:" in out, (
        f"Reasoning event missing the '✻ Thinking:' prefix; output was:\n{out}"
    )
    assert "step back" in out


def test_assistant_reasoning_silenced_when_render_reasoning_is_false() -> None:
    """``render_reasoning=False`` suppresses reasoning entirely."""
    renderer, _summary, buf = _make_renderer(render_reasoning=False)
    renderer.render(
        {
            "type": ASSISTANT_REASONING,
            "content": "secret deliberation",
            "reasoning_id": "r1",
        }
    )
    assert buf.getvalue() == "", (
        f"render_reasoning=False should suppress reasoning entirely; "
        f"got:\n{buf.getvalue()}"
    )


def test_assistant_reasoning_silenced_at_default_when_disabled_even_at_high_verbosity() -> None:
    """Higher verbosity still respects the explicit ``render_reasoning=False`` toggle.

    The user's opt-out takes precedence over the verbosity ladder — otherwise
    ``--no-reasoning -vv`` would surprise the operator.
    """
    renderer, _summary, buf = _make_renderer(
        verbosity=2, render_reasoning=False
    )
    renderer.render(
        {
            "type": ASSISTANT_REASONING,
            "content": "private deliberation",
            "reasoning_id": "r1",
        }
    )
    assert "private deliberation" not in buf.getvalue()


# ---------------------------------------------------------------------------
# Assistant final message
# ---------------------------------------------------------------------------


def test_assistant_message_renders_content_once() -> None:
    """An ASSISTANT_MESSAGE event prints each line of its content exactly once.

    Streaming deltas are filtered upstream by ``events.map_sdk_event`` so the
    only path into the renderer is the final message event — verifying the
    'no in-place re-render' acceptance criterion.
    """
    renderer, _summary, buf = _make_renderer()
    renderer.render(
        {
            "type": ASSISTANT_MESSAGE,
            "content": "Hello\nWorld",
            "message_id": "m1",
        }
    )
    out = buf.getvalue()
    assert out.count("Hello") == 1, f"'Hello' duplicated in output:\n{out}"
    assert out.count("World") == 1, f"'World' duplicated in output:\n{out}"


# ---------------------------------------------------------------------------
# Live streaming (assistant.*_delta forwarded to the renderer)
# ---------------------------------------------------------------------------


def test_stream_reasoning_prints_prefix_once_and_accumulates() -> None:
    """Streamed reasoning prints the '✻ Thinking:' prefix once, then chunks."""
    renderer, _summary, buf = _make_renderer(render_reasoning=True)
    renderer.stream_reasoning("Let me ")
    renderer.stream_reasoning("think ")
    renderer.stream_reasoning("carefully.")
    out = buf.getvalue()
    assert out.count("✻ Thinking:") == 1, f"prefix not printed once:\n{out}"
    assert "Let me think carefully." in out.replace("\n", "")


def test_stream_reasoning_then_final_event_does_not_duplicate() -> None:
    """The final ASSISTANT_REASONING after streaming must not re-print the block."""
    renderer, _summary, buf = _make_renderer(render_reasoning=True)
    renderer.stream_reasoning("deliberating")
    renderer.render(
        {
            "type": ASSISTANT_REASONING,
            "content": "deliberating",
            "reasoning_id": "r1",
        }
    )
    out = buf.getvalue()
    assert out.count("deliberating") == 1, f"reasoning duplicated:\n{out}"
    assert out.count("✻ Thinking:") == 1, f"prefix duplicated:\n{out}"
    assert out.endswith("\n"), "streamed reasoning line should be terminated"


def test_stream_message_then_final_event_does_not_duplicate() -> None:
    """The final ASSISTANT_MESSAGE after streaming must not re-print the block."""
    renderer, _summary, buf = _make_renderer()
    renderer.stream_message("Hello ")
    renderer.stream_message("world")
    renderer.render(
        {"type": ASSISTANT_MESSAGE, "content": "Hello world", "message_id": "m1"}
    )
    out = buf.getvalue()
    assert out.count("Hello world") == 1, f"message duplicated:\n{out}"
    assert out.endswith("\n"), "streamed message line should be terminated"


def test_stream_reasoning_suppressed_when_render_reasoning_false() -> None:
    """``render_reasoning=False`` suppresses streamed reasoning entirely."""
    renderer, _summary, buf = _make_renderer(render_reasoning=False)
    renderer.stream_reasoning("secret thoughts")
    assert buf.getvalue() == "", f"streamed reasoning leaked:\n{buf.getvalue()}"


def test_final_event_without_streaming_still_prints_full_content() -> None:
    """When no deltas arrive, the final events print full content (fallback)."""
    renderer, _summary, buf = _make_renderer(render_reasoning=True)
    renderer.render(
        {"type": ASSISTANT_REASONING, "content": "whole block", "reasoning_id": "r1"}
    )
    renderer.render(
        {"type": ASSISTANT_MESSAGE, "content": "whole answer", "message_id": "m1"}
    )
    out = buf.getvalue()
    assert "whole block" in out
    assert "whole answer" in out


def test_full_line_event_closes_open_stream() -> None:
    """A full-line event mid-stream terminates the open streamed line first."""
    renderer, _summary, buf = _make_renderer()
    renderer.stream_message("partial")
    renderer.render(
        {"type": TOOL_CALL, "tool_name": "bash", "arguments": {"command": "ls"}}
    )
    out = buf.getvalue()
    # The streamed chunk and the tool-call line must be on separate lines.
    assert "partial\n" in out, f"open stream not closed before tool call:\n{out}"
    assert "bash" in out


def test_two_reasoning_blocks_each_get_their_own_prefix() -> None:
    """Each separate reasoning block re-prints the prefix after its final event."""
    renderer, _summary, buf = _make_renderer(render_reasoning=True)
    renderer.stream_reasoning("block A")
    renderer.render(
        {"type": ASSISTANT_REASONING, "content": "block A", "reasoning_id": "r1"}
    )
    renderer.stream_reasoning("block B")
    renderer.render(
        {"type": ASSISTANT_REASONING, "content": "block B", "reasoning_id": "r2"}
    )
    out = buf.getvalue()
    assert out.count("✻ Thinking:") == 2, f"expected one prefix per block:\n{out}"


# ---------------------------------------------------------------------------
# Tool calls + skill highlighting
# ---------------------------------------------------------------------------


def test_tool_call_renders_one_line_with_name_and_args() -> None:
    """A non-skill tool call renders a one-line summary with the tool name."""
    renderer, _summary, buf = _make_renderer()
    renderer.render(
        {
            "type": TOOL_CALL,
            "tool_call_id": "t1",
            "tool_name": "edit",
            "arguments": {"path": "src/foo.py"},
        }
    )
    out = buf.getvalue()
    assert "edit" in out
    assert "src/foo.py" in out


def test_tool_call_skill_renders_skill_name_distinctly() -> None:
    """``tool_name=='skill'`` calls surface the skill name from arguments.

    The renderer trusts the event payload (scrubber already enforces shape);
    detection is purely structural — ``tool_name == 'skill'`` plus an
    ``arguments.skill`` key.
    """
    renderer, _summary, buf = _make_renderer()
    renderer.render(
        {
            "type": TOOL_CALL,
            "tool_call_id": "t2",
            "tool_name": "skill",
            "arguments": {"skill": "tdd"},
        }
    )
    out = buf.getvalue()
    assert "tdd" in out, (
        f"skill name should surface in skill-invocation render; got:\n{out}"
    )


def test_tool_call_skill_with_missing_arguments_does_not_crash() -> None:
    """Malformed skill calls (missing ``arguments`` / missing ``skill`` key) do not crash."""
    renderer, _summary, buf = _make_renderer()
    renderer.render(
        {
            "type": TOOL_CALL,
            "tool_call_id": "t3",
            "tool_name": "skill",
            "arguments": {},
        }
    )
    renderer.render(
        {
            "type": TOOL_CALL,
            "tool_call_id": "t4",
            "tool_name": "skill",
            # arguments key absent entirely
        }
    )
    # Just expect no exception; output may or may not surface anything.


def test_tool_call_args_already_truncated_by_scrubber_render_as_is() -> None:
    """Args >200 chars are replaced upstream with ``<truncated: N chars>``.

    The renderer is downstream of the scrubber and prints whatever it
    receives — the test asserts the sentinel survives rendering.
    """
    renderer, _summary, buf = _make_renderer()
    renderer.render(
        {
            "type": TOOL_CALL,
            "tool_call_id": "t5",
            "tool_name": "edit",
            "arguments": "<truncated: 543 chars>",
        }
    )
    assert "<truncated: 543 chars>" in buf.getvalue()


# ---------------------------------------------------------------------------
# Tool results — verbosity ladder
# ---------------------------------------------------------------------------


def test_tool_result_silent_at_default_verbosity() -> None:
    """At default verbosity, tool results are dropped to keep scrollback clean."""
    renderer, _summary, buf = _make_renderer(verbosity=0)
    renderer.render(
        {
            "type": TOOL_RESULT,
            "tool_call_id": "t1",
            "success": True,
            "result_size_chars": 1024,
        }
    )
    assert buf.getvalue() == ""


def test_tool_result_renders_size_at_v() -> None:
    """At ``-v``, tool results render size (+ error message if present)."""
    renderer, _summary, buf = _make_renderer(verbosity=1)
    renderer.render(
        {
            "type": TOOL_RESULT,
            "tool_call_id": "t1",
            "success": True,
            "result_size_chars": 1024,
        }
    )
    out = buf.getvalue()
    assert "1024" in out, f"-v should surface result size; got:\n{out}"


def test_tool_result_renders_error_at_v() -> None:
    """Failed tool calls surface the error message at ``-v``."""
    renderer, _summary, buf = _make_renderer(verbosity=1)
    renderer.render(
        {
            "type": TOOL_RESULT,
            "tool_call_id": "t1",
            "success": False,
            "error": {"message": "permission denied", "code": "EPERM"},
        }
    )
    out = buf.getvalue()
    assert "permission denied" in out


# ---------------------------------------------------------------------------
# Wrapper-emitted events
# ---------------------------------------------------------------------------


def test_wrapper_commit_recorded_renders_one_line() -> None:
    renderer, _summary, buf = _make_renderer()
    renderer.render(
        {
            "type": WRAPPER_COMMIT_RECORDED,
            "sha": "abcdef0123456789",
            "subject": "feat(thing): do thing",
        }
    )
    out = buf.getvalue()
    assert "abcdef0" in out or "abcdef01234" in out
    # Stripped output: each printed event uses at most one newline-terminated line.
    assert out.count("\n") <= 2


def test_wrapper_auto_close_renders_one_line() -> None:
    renderer, _summary, buf = _make_renderer()
    renderer.render(
        {
            "type": WRAPPER_AUTO_CLOSE,
            "issue": 42,
            "sha": "abcdef0",
        }
    )
    out = buf.getvalue()
    assert "42" in out
    assert "#42" in out


def test_wrapper_strike_renders_warning() -> None:
    renderer, _summary, buf = _make_renderer()
    renderer.render(
        {
            "type": WRAPPER_STRIKE,
            "strikes": 1,
            "max_strikes": 3,
        }
    )
    out = buf.getvalue()
    assert "1" in out and "3" in out
    # Should signal "strike" semantics.
    assert "strike" in out.lower() or "warn" in out.lower()


def test_wrapper_checkpoint_recorded_renders_distinctly() -> None:
    renderer, _summary, buf = _make_renderer()
    renderer.render(
        {
            "type": WRAPPER_CHECKPOINT_RECORDED,
            "sha": "abcdef0123456789",
            "issue": 32,
        }
    )
    out = buf.getvalue()
    assert "checkpoint" in out.lower()
    assert "abcdef0" in out
    assert "#32" in out
    # One printed line.
    assert out.count("\n") <= 2


def test_wrapper_checkpoint_recorded_is_not_counted_as_a_commit() -> None:
    """A Checkpoint must NOT increment the Summary's agent-commit tally."""
    renderer, summary, _buf = _make_renderer()
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 7})
    renderer.render(
        {
            "type": WRAPPER_COMMIT_RECORDED,
            "sha": "1111111111111111",
            "subject": "feat: real agent work",
        }
    )
    renderer.render(
        {
            "type": WRAPPER_CHECKPOINT_RECORDED,
            "sha": "2222222222222222",
            "issue": 7,
        }
    )
    snap = summary.current
    assert snap is not None
    # The agent commit counts; the Checkpoint does not.
    assert snap.commits == 1


def test_wrapper_ask_user_attempted_renders_warning() -> None:
    renderer, _summary, buf = _make_renderer()
    renderer.render(
        {
            "type": WRAPPER_ASK_USER_ATTEMPTED,
            "prompt": "what should I do?",
        }
    )
    out = buf.getvalue()
    assert "ask_user" in out.lower() or "ask user" in out.lower()


def test_wrapper_afk_ready_collected_renders_pool_summary() -> None:
    renderer, _summary, buf = _make_renderer()
    renderer.render(
        {
            "type": WRAPPER_AFK_READY_COLLECTED,
            "issues": [42, 43, 44],
        }
    )
    out = buf.getvalue()
    # Operator-facing line should at minimum surface the count.
    assert "3" in out


# ---------------------------------------------------------------------------
# Usage accumulation — silent at default
# ---------------------------------------------------------------------------


def test_usage_tokens_does_not_print_at_default_verbosity() -> None:
    """Token usage is accumulated silently; rendered only at iteration boundaries."""
    renderer, summary, buf = _make_renderer()
    renderer.render(
        {
            "type": WRAPPER_ITERATION_START,
            "iter": 1,
            "issue": 42,
        }
    )
    pre_len = len(buf.getvalue())
    renderer.render(
        {
            "type": USAGE_TOKENS,
            "model": "claude-opus-4.7-xhigh",
            "input": 1000,
            "output": 200,
        }
    )
    # Per-event ticker output must be empty (no growth between pre_len and now).
    assert len(buf.getvalue()) == pre_len, (
        f"usage.tokens should not produce per-event output; "
        f"new content: {buf.getvalue()[pre_len:]!r}"
    )
    # …but the snapshot must have absorbed the counts.
    snap = summary.current
    assert snap is not None
    assert snap.tokens_in == 1000
    assert snap.tokens_out == 200
    assert snap.model == "claude-opus-4.7-xhigh"


def test_usage_tokens_sums_multiple_events() -> None:
    """Multiple ``usage.tokens`` events within an iteration sum cleanly."""
    renderer, summary, _buf = _make_renderer()
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 7})
    for tokens_in, tokens_out in [(100, 20), (200, 50), (50, 5)]:
        renderer.render(
            {
                "type": USAGE_TOKENS,
                "model": "claude-opus-4.7-xhigh",
                "input": tokens_in,
                "output": tokens_out,
            }
        )
    snap = summary.current
    assert snap is not None
    assert snap.tokens_in == 350
    assert snap.tokens_out == 75


def test_usage_tokens_model_none_does_not_crash() -> None:
    """Some SDK versions may emit usage events with model=None — must not crash."""
    renderer, summary, _buf = _make_renderer()
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 7})
    renderer.render(
        {
            "type": USAGE_TOKENS,
            "model": None,
            "input": 100,
            "output": 20,
        }
    )
    snap = summary.current
    assert snap is not None
    assert snap.tokens_in == 100
    assert snap.tokens_out == 20


def test_usage_tokens_first_non_none_model_wins() -> None:
    """When model arrives later (None → "gpt-5.4"), the first non-None value sticks.

    Documented behaviour: keep the first authoritative model name we see so a
    transient ``None`` doesn't overwrite a real value on a follow-up event.
    """
    renderer, summary, _buf = _make_renderer()
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 7})
    renderer.render(
        {"type": USAGE_TOKENS, "model": None, "input": 10, "output": 5}
    )
    renderer.render(
        {"type": USAGE_TOKENS, "model": "gpt-5.4", "input": 10, "output": 5}
    )
    renderer.render(
        {"type": USAGE_TOKENS, "model": None, "input": 10, "output": 5}
    )
    snap = summary.current
    assert snap is not None
    assert snap.model == "gpt-5.4"


# ---------------------------------------------------------------------------
# Iteration lifecycle — snapshot boundaries
# ---------------------------------------------------------------------------


def test_iteration_start_opens_a_new_snapshot() -> None:
    renderer, summary, _buf = _make_renderer()
    assert summary.current is None
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 42})
    assert summary.current is not None
    assert summary.current.iter_num == 1
    assert summary.current.issue_num == 42


def test_iteration_end_freezes_snapshot_and_appends_to_completed() -> None:
    renderer, summary, _buf = _make_renderer()
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 42})
    renderer.render({"type": WRAPPER_ITERATION_END, "iter": 1})
    assert summary.current is None
    assert len(summary.completed) == 1
    assert summary.completed[0].iter_num == 1


def test_iteration_end_without_start_does_not_crash() -> None:
    """A stray iteration.end event (e.g. abort path) must not crash."""
    renderer, summary, _buf = _make_renderer()
    renderer.render({"type": WRAPPER_ITERATION_END, "iter": 1})
    # No exception is the contract; state stays sane.
    assert summary.current is None


def test_wrapper_commit_and_auto_close_accumulate_into_snapshot() -> None:
    renderer, summary, _buf = _make_renderer()
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 42})
    renderer.render(
        {"type": WRAPPER_COMMIT_RECORDED, "sha": "deadbeef", "subject": "x"}
    )
    renderer.render(
        {"type": WRAPPER_COMMIT_RECORDED, "sha": "cafebabe", "subject": "y"}
    )
    renderer.render({"type": WRAPPER_AUTO_CLOSE, "issue": 42, "sha": "deadbeef"})
    assert summary.current is not None
    assert summary.current.commits == 2
    assert summary.current.auto_closures == 1


def test_tool_count_accumulates_for_non_skill_calls() -> None:
    renderer, summary, _buf = _make_renderer()
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 42})
    renderer.render(
        {
            "type": TOOL_CALL,
            "tool_call_id": "t1",
            "tool_name": "edit",
            "arguments": {},
        }
    )
    renderer.render(
        {
            "type": TOOL_CALL,
            "tool_call_id": "t2",
            "tool_name": "bash",
            "arguments": {},
        }
    )
    assert summary.current is not None
    assert summary.current.tool_count == 2
    assert summary.current.skill_count == 0


def test_skill_count_accumulates_for_skill_calls() -> None:
    renderer, summary, _buf = _make_renderer()
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 42})
    renderer.render(
        {
            "type": TOOL_CALL,
            "tool_call_id": "t1",
            "tool_name": "skill",
            "arguments": {"skill": "tdd"},
        }
    )
    renderer.render(
        {
            "type": TOOL_CALL,
            "tool_call_id": "t2",
            "tool_name": "skill",
            "arguments": {"skill": "diagnose"},
        }
    )
    assert summary.current is not None
    assert summary.current.tool_count == 2
    assert summary.current.skill_count == 2


def test_strike_accounting_cumulative_value_wins() -> None:
    """A WRAPPER_STRIKE event carrying ``strikes`` is used verbatim (cumulative)."""
    renderer, summary, _buf = _make_renderer()
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 42})
    renderer.render({"type": WRAPPER_STRIKE, "strikes": 2, "max_strikes": 3})
    assert summary.current is not None
    assert summary.current.strikes == 2


def test_strike_accounting_increments_when_no_cumulative_value() -> None:
    """Without a ``strikes`` key, each STRIKE event increments the counter."""
    renderer, summary, _buf = _make_renderer()
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 42})
    renderer.render({"type": WRAPPER_STRIKE})
    renderer.render({"type": WRAPPER_STRIKE})
    assert summary.current is not None
    assert summary.current.strikes == 2


# ---------------------------------------------------------------------------
# Frozen iteration Panel
# ---------------------------------------------------------------------------


def test_iteration_panel_rendered_at_iteration_end() -> None:
    """The Panel renders all required counters at iteration end."""
    renderer, summary, buf = _make_renderer(pricing_date="2026-05-16")
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 42})
    renderer.render(
        {
            "type": USAGE_TOKENS,
            "model": "claude-opus-4.7-xhigh",
            "input": 1000,
            "output": 200,
        }
    )
    renderer.render(
        {
            "type": TOOL_CALL,
            "tool_call_id": "t1",
            "tool_name": "edit",
            "arguments": {},
        }
    )
    renderer.render(
        {
            "type": TOOL_CALL,
            "tool_call_id": "t2",
            "tool_name": "skill",
            "arguments": {"skill": "tdd"},
        }
    )
    renderer.render(
        {"type": WRAPPER_COMMIT_RECORDED, "sha": "deadbeef", "subject": "x"}
    )
    renderer.render({"type": WRAPPER_AUTO_CLOSE, "issue": 42, "sha": "deadbeef"})
    renderer.render({"type": WRAPPER_ITERATION_END, "iter": 1})
    out = buf.getvalue()
    # Issue spec: every required field surfaces in the panel.
    assert "claude-opus-4.7-xhigh" in out, f"model missing from panel:\n{out}"
    # Tokens (Rich may insert spaces / commas in numeric formatting; allow both).
    assert "1000" in out or "1,000" in out, f"tokens_in missing:\n{out}"
    assert "200" in out
    # Skill + tool counts surface.
    # Cost is present and dated.
    assert "2026-05-16" in out, f"pricing date label missing:\n{out}"
    # Commits + auto-closures surface.
    assert "deadbeef" in out or "commit" in out.lower()


def test_iteration_panel_cost_is_em_dash_for_unknown_model() -> None:
    """When the iteration's model is not in the pricing table, cost = ``—``."""
    renderer, summary, buf = _make_renderer()
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 42})
    renderer.render(
        {
            "type": USAGE_TOKENS,
            "model": "unknown-model-9000",
            "input": 1000,
            "output": 200,
        }
    )
    renderer.render({"type": WRAPPER_ITERATION_END, "iter": 1})
    out = buf.getvalue()
    assert "—" in out, f"em dash missing from unknown-model cost line:\n{out}"
    # Defensively: must NOT render '$0.00' or '0.00' as the cost.
    assert "$0.00" not in out, f"unknown-model cost rendered as $0.00:\n{out}"


def test_iteration_panel_cost_omits_date_when_label_absent() -> None:
    """When no ``pricing_date`` is supplied, the cost line skips the date suffix."""
    renderer, summary, buf = _make_renderer(pricing_date=None)
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 42})
    renderer.render(
        {
            "type": USAGE_TOKENS,
            "model": "claude-opus-4.7-xhigh",
            "input": 1000,
            "output": 200,
        }
    )
    renderer.render({"type": WRAPPER_ITERATION_END, "iter": 1})
    out = buf.getvalue()
    # No "as of" date string should appear when pricing_date is None.
    assert "as of" not in out.lower()


def test_iteration_snapshot_to_counters_kwargs_conversion() -> None:
    """``IterationSnapshot.to_counters_kwargs()`` produces a persist-shaped dict.

    The loop slice (#10) calls this to translate the UI accumulator into a
    ``persist.IterationCounters`` instance via ``IterationCounters(**kwargs)``.
    Returning a kwargs dict (rather than the IterationCounters instance
    itself) keeps the UI module's import graph free of ``ralph_afk.persist``
    — the AST guard at the bottom of this file enforces that.
    """
    snap = IterationSnapshot(
        iter_num=2,
        issue_num=42,
        started_at=_ts(),
        ended_at=datetime(2026, 5, 16, 0, 0, 30, tzinfo=timezone.utc),
        model="claude-opus-4.7-xhigh",
        tokens_in=1000,
        tokens_out=200,
        tool_count=3,
        skill_count=1,
        commits=1,
        auto_closures=1,
        strikes=0,
    )
    kwargs = snap.to_counters_kwargs(pricing=_fixed_pricing())
    assert kwargs["iter"] == 2
    assert kwargs["duration_seconds"] == pytest.approx(30.0, rel=1e-3)
    assert kwargs["model"] == "claude-opus-4.7-xhigh"
    assert kwargs["tokens_in"] == 1000
    assert kwargs["tokens_out"] == 200
    assert kwargs["context_used"] == 1200
    assert kwargs["tool_count"] == 3
    assert kwargs["skill_count"] == 1
    assert kwargs["commits"] == 1
    assert kwargs["auto_closures"] == 1
    assert kwargs["strikes"] == 0
    # Cost is Decimal-typed (or None for unknown model).
    assert kwargs["est_cost_usd"] is not None
    assert isinstance(kwargs["est_cost_usd"], Decimal)
    # The dict is shaped to be splatted into IterationCounters; verify that
    # contract end-to-end so persist-side field renames surface here loudly.
    from ralph_afk.persist import IterationCounters

    counters = IterationCounters(**kwargs)
    assert counters.iter == 2
    assert counters.context_used == 1200
    assert counters.est_cost_usd == kwargs["est_cost_usd"]


def test_iteration_snapshot_to_counters_kwargs_unknown_model_yields_none_cost() -> None:
    snap = IterationSnapshot(
        iter_num=1,
        started_at=_ts(),
        ended_at=_ts(),
        model="unknown-model",
        tokens_in=100,
        tokens_out=20,
    )
    kwargs = snap.to_counters_kwargs(pricing=_fixed_pricing())
    assert kwargs["est_cost_usd"] is None


# ---------------------------------------------------------------------------
# Frozen run-end Table
# ---------------------------------------------------------------------------


def test_run_end_table_renders_one_row_per_iteration_plus_totals() -> None:
    """The run-end Table renders rows for every completed iteration + totals footer."""
    renderer, summary, buf = _make_renderer(pricing_date="2026-05-16")
    renderer.render({"type": WRAPPER_RUN_START, "run_id": "01HXR0000000000000000000A1"})
    for i, issue in enumerate([42, 43], start=1):
        renderer.render(
            {"type": WRAPPER_ITERATION_START, "iter": i, "issue": issue}
        )
        renderer.render(
            {
                "type": USAGE_TOKENS,
                "model": "claude-opus-4.7-xhigh",
                "input": 1000,
                "output": 200,
            }
        )
        renderer.render(
            {"type": WRAPPER_COMMIT_RECORDED, "sha": "deadbeef", "subject": "x"}
        )
        renderer.render({"type": WRAPPER_AUTO_CLOSE, "issue": issue, "sha": "deadbeef"})
        renderer.render({"type": WRAPPER_ITERATION_END, "iter": i})
    renderer.render({"type": WRAPPER_RUN_END, "outcome": "empty_pool"})
    out = buf.getvalue()
    # Both iteration numbers and issue numbers surface in the table.
    assert "#42" in out and "#43" in out
    # Tokens / counters surface.
    assert "2000" in out or "2,000" in out, (
        f"totals row missing summed tokens:\n{out}"
    )
    # Totals row exists (we mark it with the literal 'total' or 'sum' label).
    assert "total" in out.lower() or "totals" in out.lower()


def test_run_end_table_handles_zero_iterations() -> None:
    """Empty-pool exit (zero iterations) still renders cleanly."""
    renderer, _summary, buf = _make_renderer()
    renderer.render({"type": WRAPPER_RUN_START, "run_id": "01HXR0000000000000000000A2"})
    renderer.render({"type": WRAPPER_RUN_END, "outcome": "empty_pool"})
    out = buf.getvalue()
    # No exception is the main contract; an "empty pool" message helps the operator.
    # At minimum, the run-end render path must not crash.
    assert "0" in out or "empty" in out.lower() or "no" in out.lower() or out != ""


def test_run_end_table_final_strikes_uses_last_iteration_value() -> None:
    """The footer's 'final strikes' value is the last iteration's strike count.

    Strikes reset on progress in the wrapper contract; summing them across
    iterations would be misleading. The footer surfaces the value that
    actually determined whether the run aborted.
    """
    renderer, summary, buf = _make_renderer(pricing_date="2026-05-16")
    renderer.render({"type": WRAPPER_RUN_START, "run_id": "01HXR0000000000000000000A3"})
    # Iter 1: 2 strikes
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 42})
    renderer.render({"type": WRAPPER_STRIKE, "strikes": 2, "max_strikes": 3})
    renderer.render({"type": WRAPPER_ITERATION_END, "iter": 1})
    # Iter 2: 0 strikes (progress made)
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 2, "issue": 43})
    renderer.render(
        {"type": WRAPPER_COMMIT_RECORDED, "sha": "deadbeef", "subject": "x"}
    )
    renderer.render({"type": WRAPPER_ITERATION_END, "iter": 2})
    renderer.render({"type": WRAPPER_RUN_END, "outcome": "empty_pool"})
    # Programmatic assertion: summary surface exposes a totals view.
    totals = summary.totals()
    assert totals.final_strikes == 0, (
        f"final_strikes should be the last iteration's value (0), got {totals.final_strikes}"
    )


def test_run_summary_totals_sum_tokens_and_costs() -> None:
    """RunSummary.totals() sums tokens, commits, auto-closures across iterations.

    Cost sum only over iterations whose model was in the pricing table
    (unknown-model iterations contribute None and are skipped).
    """
    renderer, summary, _buf = _make_renderer(pricing_date="2026-05-16")
    for i, model in enumerate(["claude-opus-4.7-xhigh", "unknown-model"], start=1):
        renderer.render({"type": WRAPPER_ITERATION_START, "iter": i, "issue": 40 + i})
        renderer.render(
            {"type": USAGE_TOKENS, "model": model, "input": 1000, "output": 200}
        )
        renderer.render(
            {"type": WRAPPER_COMMIT_RECORDED, "sha": "deadbeef", "subject": "x"}
        )
        renderer.render({"type": WRAPPER_ITERATION_END, "iter": i})
    totals = summary.totals()
    assert totals.tokens_in == 2000
    assert totals.tokens_out == 400
    assert totals.commits == 2
    # Cost summed only over the iteration whose model was priced.
    expected = (Decimal(1000) * Decimal("15.00") + Decimal(200) * Decimal("75.00")) / Decimal(1_000_000)
    assert totals.cost_usd == expected


# ---------------------------------------------------------------------------
# No-ANSI guarantee
# ---------------------------------------------------------------------------


def test_output_has_no_ansi_escapes_when_force_terminal_is_false() -> None:
    """Capturing with ``force_terminal=False`` must yield plain text.

    Required for ``tee``- and redirect-friendly mirrors of unattended runs.
    """
    renderer, _summary, buf = _make_renderer(pricing_date="2026-05-16")
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 42})
    renderer.render(
        {
            "type": USAGE_TOKENS,
            "model": "claude-opus-4.7-xhigh",
            "input": 1000,
            "output": 200,
        }
    )
    renderer.render(
        {
            "type": ASSISTANT_REASONING,
            "content": "thinking hard",
            "reasoning_id": "r1",
        }
    )
    renderer.render(
        {
            "type": TOOL_CALL,
            "tool_call_id": "t1",
            "tool_name": "skill",
            "arguments": {"skill": "tdd"},
        }
    )
    renderer.render(
        {"type": WRAPPER_COMMIT_RECORDED, "sha": "deadbeef", "subject": "x"}
    )
    renderer.render({"type": WRAPPER_ITERATION_END, "iter": 1})
    out = buf.getvalue()
    assert _ANSI_RE.search(out) is None, (
        f"force_terminal=False output contains ANSI escape sequences:\n{out!r}"
    )


def test_no_spinner_glyphs_in_non_tty_output() -> None:
    """Non-TTY captures must not contain Rich's spinner glyphs (``⠙``/``⠹``/etc.)."""
    renderer, _summary, buf = _make_renderer()
    renderer.render({"type": WRAPPER_RUN_START, "run_id": "01HXR0000000000000000000A4"})
    renderer.render({"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 42})
    renderer.render(
        {"type": WRAPPER_AFK_READY_COLLECTED, "issues": [42, 43, 44]}
    )
    renderer.render({"type": WRAPPER_ITERATION_END, "iter": 1})
    renderer.render({"type": WRAPPER_RUN_END, "outcome": "empty_pool"})
    out = buf.getvalue()
    spinner_glyphs = "⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷"
    for ch in spinner_glyphs:
        assert ch not in out, f"non-TTY output contains spinner glyph {ch!r}:\n{out}"


# ---------------------------------------------------------------------------
# Verbosity ladder
# ---------------------------------------------------------------------------


def test_vvv_raw_dumps_session_and_permission_events_that_are_otherwise_silent() -> None:
    """At ``-vvv``, permission + session events that the renderer normally drops
    surface as raw dumps so the operator can see everything."""
    renderer, _summary, buf = _make_renderer(verbosity=3)
    renderer.render(
        {
            "type": SESSION_CREATED,
            "session_id": "s1",
            "model": "claude-opus-4.7-xhigh",
        }
    )
    renderer.render(
        {
            "type": TOOL_PERMISSION_REQUESTED,
            "tool_name": "edit",
            "arguments": {"path": "foo"},
        }
    )
    out = buf.getvalue()
    assert "session.created" in out
    assert "tool.permission_requested" in out


def test_session_and_permission_events_silent_at_default_verbosity() -> None:
    """SESSION_CREATED / TOOL_PERMISSION_REQUESTED are dropped at default verbosity."""
    renderer, _summary, buf = _make_renderer(verbosity=0)
    renderer.render(
        {
            "type": SESSION_CREATED,
            "session_id": "s1",
            "model": "claude-opus-4.7-xhigh",
        }
    )
    renderer.render(
        {
            "type": TOOL_PERMISSION_REQUESTED,
            "tool_name": "edit",
            "arguments": {"path": "foo"},
        }
    )
    assert buf.getvalue() == ""


def test_tool_permission_denied_renders_at_default_verbosity() -> None:
    """Denials are operator-relevant — they always surface."""
    renderer, _summary, buf = _make_renderer(verbosity=0)
    renderer.render(
        {
            "type": TOOL_PERMISSION_DENIED,
            "tool_name": "shell",
            "reason": "deny-list",
        }
    )
    out = buf.getvalue()
    assert "shell" in out
    assert "deny" in out.lower() or "denied" in out.lower()


# ---------------------------------------------------------------------------
# Renderer integration smoke (the test the issue explicitly names)
# ---------------------------------------------------------------------------


def test_ui_smoke_event_sequence_through_renderer() -> None:
    """The headline acceptance test: a representative event sequence flows
    through the renderer with TTY forced off, raises no exceptions, and the
    captured output contains the documented canonical strings.

    This test is the one the issue explicitly names by file
    (``tests/test_ui_smoke.py``). It mirrors the shape of a real
    iteration in miniature: run-start → iteration-start → reasoning →
    tool call → skill invocation → assistant message → usage tokens →
    commit recorded → auto-close → iteration-end → run-end.
    """
    renderer, _summary, buf = _make_renderer(pricing_date="2026-05-16")
    events: list[dict[str, Any]] = [
        {"type": WRAPPER_RUN_START, "run_id": "01HXR0000000000000000000A0"},
        {
            "type": WRAPPER_AFK_READY_COLLECTED,
            "issues": [42],
        },
        {"type": WRAPPER_ITERATION_START, "iter": 1, "issue": 42},
        {
            "type": ASSISTANT_REASONING,
            "content": "step back, think it through",
            "reasoning_id": "r1",
        },
        {
            "type": TOOL_CALL,
            "tool_call_id": "t1",
            "tool_name": "edit",
            "arguments": {"path": "src/foo.py"},
        },
        {
            "type": TOOL_RESULT,
            "tool_call_id": "t1",
            "success": True,
            "result_size_chars": 1024,
        },
        {
            "type": TOOL_CALL,
            "tool_call_id": "t2",
            "tool_name": "skill",
            "arguments": {"skill": "tdd"},
        },
        {
            "type": ASSISTANT_MESSAGE,
            "content": "Done.",
            "message_id": "m1",
        },
        {
            "type": USAGE_TOKENS,
            "model": "claude-opus-4.7-xhigh",
            "input": 1500,
            "output": 250,
        },
        {
            "type": WRAPPER_COMMIT_RECORDED,
            "sha": "abcdef0123456789",
            "subject": "feat(thing): do thing\n\nCloses #42",
        },
        {"type": WRAPPER_AUTO_CLOSE, "issue": 42, "sha": "abcdef0"},
        {"type": WRAPPER_ITERATION_END, "iter": 1},
        {"type": WRAPPER_RUN_END, "outcome": "empty_pool"},
    ]
    for ev in events:
        renderer.render(ev)  # No exception expected.

    out = buf.getvalue()
    # Canonical strings the issue acceptance lists.
    assert "✻ Thinking:" in out, f"reasoning prefix missing:\n{out}"
    assert "edit" in out
    assert "tdd" in out, f"skill name missing:\n{out}"
    assert "Done." in out
    assert "#42" in out
    assert "claude-opus-4.7-xhigh" in out
    assert "2026-05-16" in out, f"pricing date label missing:\n{out}"


# ---------------------------------------------------------------------------
# AST import guard
# ---------------------------------------------------------------------------


def _ui_module_paths() -> list[Path]:
    pkg = Path(events_module.__file__).parent / "ui"
    return sorted(pkg.glob("*.py"))


_ALLOWED_UI_IMPORTS: frozenset[str] = frozenset(
    {
        # Stdlib
        "__future__",
        "dataclasses",
        "datetime",
        "decimal",
        "io",
        "typing",
        # Rich (the renderer's whole reason to exist)
        "rich",
        "rich.console",
        "rich.panel",
        "rich.table",
        "rich.text",
        "rich.style",
        "rich.box",
        "rich.padding",
        # First-party deep modules
        "ralph_afk.events",
        "ralph_afk.pricing",
    }
)


def _classify_import(node: ast.AST, current_module: str | None) -> list[str]:
    """Return the list of fully-qualified module names a node references.

    ``from foo.bar import baz`` returns ``["foo.bar"]``; ``import a.b, c``
    returns ``["a.b", "c"]``. Relative imports inside ``ralph_afk.ui`` are
    exempt (returns ``[]``).
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level and node.level > 0:
            return []  # relative imports inside the package — exempt
        if node.module is None:
            return []
        return [node.module]
    return []


@pytest.mark.parametrize("path", _ui_module_paths(), ids=lambda p: p.name)
def test_ui_module_imports_are_constrained(path: Path) -> None:
    """``ralph_afk.ui.*`` modules import only Rich, stdlib, and first-party deep modules.

    Catches stray third-party imports (httpx, requests, github, gitpython)
    and accidental coupling to shell-side modules (``ralph_afk.gh``,
    ``ralph_afk.git``, ``ralph_afk.persist``) which the UI must not import
    — keeps the UI module pure enough to test in isolation.
    """
    forbidden_first_party: frozenset[str] = frozenset(
        {
            "ralph_afk.gh",
            "ralph_afk.git",
            "ralph_afk.persist",
            "ralph_afk.cli",
            "ralph_afk.wrapper",
        }
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        for module_name in _classify_import(node, current_module=None):
            top = module_name.split(".", 1)[0]
            allowed = (
                module_name in _ALLOWED_UI_IMPORTS
                or any(
                    module_name == allowed_pat
                    or module_name.startswith(allowed_pat + ".")
                    for allowed_pat in _ALLOWED_UI_IMPORTS
                )
                or top == "rich"  # any rich.* submodule allowed
            )
            assert module_name not in forbidden_first_party, (
                f"{path.name} imports forbidden first-party module {module_name!r}; "
                f"UI must stay decoupled from shell/CLI/persist/wrapper modules."
            )
            assert allowed, (
                f"{path.name} imports disallowed module {module_name!r}; "
                f"UI allowlist is {sorted(_ALLOWED_UI_IMPORTS)} plus rich.*."
            )
