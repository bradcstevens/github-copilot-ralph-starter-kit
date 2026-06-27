"""Tests for ``ralph_afk.events`` (issue #5).

Covers the deep, pure events module:

* Envelope shape (``ts`` / ``run_id`` / ``iter`` / ``type``) and the
  millisecond-precision ``ts`` formatter.
* :func:`make_event` payload merging + collision detection.
* :func:`to_jsonl_line` deterministic key ordering, trailing newline,
  ASCII handling.
* :func:`scrub` secret-redaction regexes (GitHub tokens, JWT-shaped
  strings, AWS keys), edit/create content stripping, ``gh issue close``
  comment body replacement, and >200-char tool-args truncation.
* :func:`scrub` idempotency.
* :func:`map_sdk_event` — one example mapping per SDK event the runner
  subscribes to, plus the explicit ``None`` cases (deltas, permission
  lifecycle, user-input-requested, abort).
* Event-type constants — every literal listed in the issue spec.
* Module imports only stdlib + the SDK's typed event package
  (AST-enforced).
"""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from copilot.generated.session_events import (
    AbortData,
    AssistantMessageData,
    AssistantReasoningData,
    AssistantReasoningDeltaData,
    AssistantUsageData,
    PermissionApproved,
    PermissionCompletedData,
    SessionEvent,
    SessionEventType,
    SessionIdleData,
    SessionShutdownData,
    SessionStartData,
    ShutdownCodeChanges,
    ShutdownType,
    ToolExecutionCompleteData,
    ToolExecutionCompleteError,
    ToolExecutionCompleteResult,
    ToolExecutionStartData,
    UserInputRequestedData,
)

from ralph_afk import events as events_module
from ralph_afk.events import (
    ASSISTANT_MESSAGE,
    ASSISTANT_REASONING,
    MAX_TOOL_ARGS_CHARS,
    REDACTED_SECRET,
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
    map_sdk_event,
    scrub,
    to_jsonl_line,
)

# ---------------------------------------------------------------------------
# Event-type constants
# ---------------------------------------------------------------------------


def test_wrapper_event_constants_are_literal_strings() -> None:
    """Every wrapper event-type literal listed in the PRD must exist."""
    assert WRAPPER_RUN_START == "wrapper.run.start"
    assert WRAPPER_RUN_END == "wrapper.run.end"
    assert WRAPPER_ITERATION_START == "wrapper.iteration.start"
    assert WRAPPER_ITERATION_END == "wrapper.iteration.end"
    assert WRAPPER_AFK_READY_COLLECTED == "wrapper.afk_ready.collected"
    assert WRAPPER_CHECKPOINT_RECORDED == "wrapper.checkpoint.recorded"
    assert WRAPPER_COMMIT_RECORDED == "wrapper.commit.recorded"
    assert WRAPPER_AUTO_CLOSE == "wrapper.auto_close"
    assert WRAPPER_STRIKE == "wrapper.strike"
    assert WRAPPER_ASK_USER_ATTEMPTED == "wrapper.ask_user.attempted"


def test_sdk_mapped_event_constants_are_literal_strings() -> None:
    """Every SDK-mapped event-type literal listed in the PRD must exist."""
    assert SESSION_CREATED == "session.created"
    assert SESSION_IDLE == "session.idle"
    assert SESSION_DELETED == "session.deleted"
    assert ASSISTANT_MESSAGE == "assistant.message"
    assert ASSISTANT_REASONING == "assistant.reasoning"
    assert TOOL_CALL == "tool.call"
    assert TOOL_RESULT == "tool.result"
    assert TOOL_PERMISSION_REQUESTED == "tool.permission_requested"
    assert TOOL_PERMISSION_DENIED == "tool.permission_denied"
    assert USAGE_TOKENS == "usage.tokens"


# ---------------------------------------------------------------------------
# Envelope structure
# ---------------------------------------------------------------------------


def _fixed_ts() -> datetime:
    """A deterministic timestamp for envelope assertions."""
    return datetime(2026, 5, 16, 0, 0, 0, 123_000, tzinfo=timezone.utc)


def test_make_event_envelope_has_ts_run_id_iter_type() -> None:
    e = make_event(
        type=WRAPPER_ITERATION_START,
        run_id="01HXR0000000000000000000AA",
        iter=3,
        ts=_fixed_ts(),
    )
    assert e["ts"] == "2026-05-16T00:00:00.123Z"
    assert e["run_id"] == "01HXR0000000000000000000AA"
    assert e["iter"] == 3
    assert e["type"] == WRAPPER_ITERATION_START


def test_make_event_iter_may_be_none_for_run_scope_events() -> None:
    e = make_event(
        type=WRAPPER_RUN_START,
        run_id="01HXR0000000000000000000AA",
        iter=None,
        ts=_fixed_ts(),
    )
    assert e["iter"] is None
    assert e["type"] == WRAPPER_RUN_START


def test_make_event_payload_kwargs_merge_into_event() -> None:
    e = make_event(
        type=WRAPPER_AFK_READY_COLLECTED,
        run_id="rid",
        iter=1,
        ts=_fixed_ts(),
        pool=[42, 43, 44],
        count=3,
    )
    assert e["pool"] == [42, 43, 44]
    assert e["count"] == 3


def test_make_event_python_blocks_payload_collision_with_envelope() -> None:
    """Caller bug: passing ``ts=`` or ``run_id=`` twice would silently lose
    data. Python's keyword-arg machinery catches this natively with
    ``TypeError: got multiple values for keyword argument 'run_id'``."""
    with pytest.raises(TypeError, match="multiple values"):
        make_event(
            type=WRAPPER_RUN_START,
            run_id="a",
            iter=None,
            ts=_fixed_ts(),
            **{"run_id": "b"},
        )


def test_make_event_ts_default_is_current_utc_with_millisecond_precision() -> None:
    """``make_event(ts=None)`` uses ``datetime.now(UTC)`` and formats it
    to millisecond precision."""
    e = make_event(type=WRAPPER_RUN_END, run_id="r", iter=None)
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", e["ts"]
    ), f"ts={e['ts']!r} does not match millisecond-precision ISO-8601 UTC"


def test_make_event_ts_format_handles_microseconds_correctly() -> None:
    """Millisecond truncation: microseconds=123456 must become ``.123`` not
    ``.123456`` or ``.124`` (no rounding)."""
    ts = datetime(2026, 1, 2, 3, 4, 5, 123_456, tzinfo=timezone.utc)
    e = make_event(type=WRAPPER_RUN_END, run_id="r", iter=None, ts=ts)
    assert e["ts"] == "2026-01-02T03:04:05.123Z"


def test_make_event_ts_naive_datetime_assumed_utc() -> None:
    """A naive datetime is treated as UTC rather than crashing."""
    naive = datetime(2026, 5, 16, 1, 2, 3, 456_000)
    e = make_event(type=WRAPPER_RUN_END, run_id="r", iter=None, ts=naive)
    assert e["ts"] == "2026-05-16T01:02:03.456Z"


def test_make_event_ts_aware_non_utc_converted_to_utc() -> None:
    """A non-UTC aware datetime is converted to UTC before formatting."""
    eastern = timezone(timedelta(hours=-5))
    ts = datetime(2026, 5, 15, 20, 0, 0, 0, tzinfo=eastern)
    e = make_event(type=WRAPPER_RUN_END, run_id="r", iter=None, ts=ts)
    assert e["ts"] == "2026-05-16T01:00:00.000Z"


# ---------------------------------------------------------------------------
# to_jsonl_line — serialisation + determinism
# ---------------------------------------------------------------------------


def test_to_jsonl_line_ends_with_newline() -> None:
    line = to_jsonl_line(
        make_event(type=WRAPPER_RUN_START, run_id="r", iter=None, ts=_fixed_ts())
    )
    assert line.endswith("\n")
    assert line.count("\n") == 1


def test_to_jsonl_line_is_valid_json() -> None:
    line = to_jsonl_line(
        make_event(
            type=WRAPPER_ITERATION_START,
            run_id="r",
            iter=1,
            ts=_fixed_ts(),
            issue=42,
        )
    )
    parsed = json.loads(line)
    assert parsed["type"] == WRAPPER_ITERATION_START
    assert parsed["iter"] == 1
    assert parsed["issue"] == 42


def test_to_jsonl_line_emits_envelope_keys_first_in_canonical_order() -> None:
    """``ts`` → ``run_id`` → ``iter`` → ``type`` then payload alphabetically.
    Determinism keeps diffs stable and grep patterns reliable."""
    e = make_event(
        type=WRAPPER_AUTO_CLOSE,
        run_id="r",
        iter=2,
        ts=_fixed_ts(),
        zeta=1,
        alpha=2,
        mu=3,
    )
    line = to_jsonl_line(e)
    # Strip the trailing newline + parse keys positionally from the raw text.
    raw = line.rstrip("\n")
    keys_in_order = [m.group(1) for m in re.finditer(r'"([^"]+)":', raw)]
    assert keys_in_order[:4] == ["ts", "run_id", "iter", "type"]
    # The remaining payload keys must be alphabetised.
    payload_keys = keys_in_order[4:]
    assert payload_keys == sorted(payload_keys)


def test_to_jsonl_line_diff_stable_across_two_calls_with_same_input() -> None:
    e = make_event(
        type=WRAPPER_AUTO_CLOSE,
        run_id="r",
        iter=2,
        ts=_fixed_ts(),
        b=1,
        a=2,
    )
    assert to_jsonl_line(e) == to_jsonl_line(e)


def test_to_jsonl_line_emits_unicode_unicode_escaped_off() -> None:
    """``ensure_ascii=False`` so non-ASCII characters round-trip cleanly."""
    e = make_event(
        type=ASSISTANT_MESSAGE,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        content="café ✻",
    )
    line = to_jsonl_line(e)
    assert "café" in line
    assert "✻" in line


def test_to_jsonl_line_iter_null_in_run_scope_event() -> None:
    line = to_jsonl_line(
        make_event(type=WRAPPER_RUN_START, run_id="r", iter=None, ts=_fixed_ts())
    )
    parsed = json.loads(line)
    assert parsed["iter"] is None


# ---------------------------------------------------------------------------
# scrub — secret redaction (regex patterns)
# ---------------------------------------------------------------------------


def test_scrub_redacts_ghp_token() -> None:
    token = "ghp_" + "A" * 36  # 40 chars total
    e = make_event(
        type=ASSISTANT_MESSAGE,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        content=f"my token is {token} please don't leak",
    )
    out = scrub(e)
    assert token not in out["content"]
    assert REDACTED_SECRET in out["content"]


def test_scrub_does_not_redact_short_ghp_lookalike() -> None:
    """A token shorter than 40 chars total isn't a real PAT — leave it alone
    so we don't corrupt unrelated text that happens to contain ``ghp_``."""
    short = "ghp_" + "A" * 10  # well under minimum
    e = make_event(
        type=ASSISTANT_MESSAGE,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        content=f"see ref {short}",
    )
    out = scrub(e)
    assert short in out["content"]


def test_scrub_redacts_gho_token() -> None:
    token = "gho_" + "B" * 40
    e = make_event(
        type=ASSISTANT_MESSAGE,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        content=token,
    )
    out = scrub(e)
    assert token not in out["content"]
    assert out["content"] == REDACTED_SECRET


def test_scrub_redacts_jwt_shaped_string() -> None:
    """Three base64url segments separated by dots, header starting with eyJ."""
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"  # header (36 chars)
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"  # claims (46 chars)
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"  # sig (43 chars)
    )
    e = make_event(
        type=ASSISTANT_MESSAGE,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        content=f"Bearer {jwt}",
    )
    out = scrub(e)
    assert jwt not in out["content"]
    assert REDACTED_SECRET in out["content"]


def test_scrub_redacts_aws_access_key_id() -> None:
    key = "AKIA" + "ABCDEFGHIJKLMNOP"  # 16 uppercase-alnum chars
    e = make_event(
        type=ASSISTANT_MESSAGE,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        content=f"export AWS_ACCESS_KEY_ID={key}",
    )
    out = scrub(e)
    assert key not in out["content"]
    assert REDACTED_SECRET in out["content"]


def test_scrub_recurses_into_nested_payload() -> None:
    """Secret regexes apply to every string leaf, regardless of nesting."""
    token = "ghp_" + "C" * 36
    e = make_event(
        type=ASSISTANT_MESSAGE,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        nested={"a": {"b": [{"c": f"key={token}"}]}},
    )
    out = scrub(e)
    leaked = json.dumps(out)
    assert token not in leaked
    assert REDACTED_SECRET in out["nested"]["a"]["b"][0]["c"]


def test_scrub_serialised_via_to_jsonl_line_does_not_leak_secrets() -> None:
    """End-to-end: secrets do not survive into the JSONL line either."""
    token = "ghp_" + "D" * 36
    line = to_jsonl_line(
        make_event(
            type=ASSISTANT_MESSAGE,
            run_id="r",
            iter=1,
            ts=_fixed_ts(),
            content=token,
        )
    )
    assert token not in line
    assert REDACTED_SECRET in line


# ---------------------------------------------------------------------------
# scrub — tool-call rules
# ---------------------------------------------------------------------------


def test_scrub_strips_content_field_from_edit_file() -> None:
    e = make_event(
        type=TOOL_CALL,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        tool_call_id="t1",
        tool_name="edit_file",
        arguments={
            "path": "src/auth.py",
            "old_str": "OLD",
            "new_str": "NEW SECRET CONTENT",
        },
    )
    out = scrub(e)
    args = out["arguments"]
    assert isinstance(args, dict)
    assert "old_str" not in args
    assert "new_str" not in args
    assert args["path"] == "src/auth.py"


def test_scrub_strips_content_field_from_create() -> None:
    e = make_event(
        type=TOOL_CALL,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        tool_call_id="t1",
        tool_name="create",
        arguments={
            "path": "secrets.env",
            "file_text": "AWS_SECRET=xyz",
        },
    )
    out = scrub(e)
    args = out["arguments"]
    assert isinstance(args, dict)
    assert "file_text" not in args
    assert args["path"] == "secrets.env"


def test_scrub_preserves_content_for_non_file_writing_tools() -> None:
    """``content`` is only suspect on file-writing tools. A ``bash`` tool's
    ``content``-named arg (rare but possible) is not auto-stripped."""
    e = make_event(
        type=TOOL_CALL,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        tool_call_id="t1",
        tool_name="grep",
        arguments={"pattern": "TODO", "content": "n/a"},
    )
    out = scrub(e)
    args = out["arguments"]
    assert isinstance(args, dict)
    assert args["content"] == "n/a"


def test_scrub_truncates_overlong_tool_args() -> None:
    """Args whose JSON-serialised length exceeds 200 chars are replaced
    with the truncation sentinel; the original length is preserved."""
    long_value = "x" * 300
    e = make_event(
        type=TOOL_CALL,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        tool_call_id="t1",
        tool_name="bash",
        arguments={"command": long_value},
    )
    out = scrub(e)
    args = out["arguments"]
    assert isinstance(args, str)
    m = re.fullmatch(r"<truncated: (\d+) chars>", args)
    assert m, f"truncation sentinel mismatch: {args!r}"
    assert int(m.group(1)) > MAX_TOOL_ARGS_CHARS


def test_scrub_keeps_short_tool_args_unchanged() -> None:
    e = make_event(
        type=TOOL_CALL,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        tool_call_id="t1",
        tool_name="bash",
        arguments={"command": "ls"},
    )
    out = scrub(e)
    assert out["arguments"] == {"command": "ls"}


def test_scrub_replaces_gh_issue_close_comment_body_double_quoted() -> None:
    body = "Implemented in abc123. Wrapped up nicely."
    e = make_event(
        type=TOOL_CALL,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        tool_call_id="t1",
        tool_name="bash",
        arguments={"command": f'gh issue close 42 --comment "{body}"'},
    )
    out = scrub(e)
    cmd = out["arguments"]["command"]
    assert body not in cmd
    assert f"<comment: {len(body)} chars>" in cmd


def test_scrub_replaces_gh_issue_close_comment_body_single_quoted() -> None:
    body = "Closed with the auth fix."
    e = make_event(
        type=TOOL_CALL,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        tool_call_id="t1",
        tool_name="bash",
        arguments={"command": f"gh issue close 42 --comment '{body}'"},
    )
    out = scrub(e)
    cmd = out["arguments"]["command"]
    assert body not in cmd
    assert f"<comment: {len(body)} chars>" in cmd


def test_scrub_replaces_gh_issue_comment_body() -> None:
    """The ``gh issue comment`` form is also covered (used for partial-progress
    notes per the autonomous-loop prompt)."""
    body = "Refs #42. Hit a blocker on subprocess parsing."
    e = make_event(
        type=TOOL_CALL,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        tool_call_id="t1",
        tool_name="bash",
        arguments={"command": f'gh issue comment 42 --body "{body}"'},
    )
    out = scrub(e)
    cmd = out["arguments"]["command"]
    # --body form is not in the spec's list — only --comment is — so we
    # don't redact it. This test pins that scope decision; if scope widens
    # to --body, this assertion flips.
    assert body in cmd


def test_scrub_does_not_touch_unrelated_comment_flag() -> None:
    """``--comment`` flags on tools other than ``gh issue (close|comment)`` are
    left alone — we don't want false positives on third-party CLIs."""
    body = "code review comment"
    cmd_in = f'foo bar --comment "{body}"'
    e = make_event(
        type=TOOL_CALL,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        tool_call_id="t1",
        tool_name="bash",
        arguments={"command": cmd_in},
    )
    out = scrub(e)
    assert out["arguments"]["command"] == cmd_in


def test_scrub_is_idempotent() -> None:
    """Running scrub twice must produce the same dict (modulo identity).
    Justifies persist (#7) calling scrub then to_jsonl_line (which also
    scrubs) without double-mutating the truncation sentinel."""
    long_value = "y" * 500
    e = make_event(
        type=TOOL_CALL,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        tool_call_id="t1",
        tool_name="bash",
        arguments={
            "command": f'gh issue close 42 --comment "this is a long comment"',
            "stdin": long_value,
        },
    )
    once = scrub(e)
    twice = scrub(once)
    assert once == twice


def test_scrub_does_not_mutate_input() -> None:
    e = make_event(
        type=TOOL_CALL,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        tool_call_id="t1",
        tool_name="edit_file",
        arguments={"path": "x.py", "new_str": "secret"},
    )
    original = json.dumps(e, sort_keys=True)
    _ = scrub(e)
    assert json.dumps(e, sort_keys=True) == original


def test_scrub_handles_non_dict_arguments() -> None:
    """SDK gives ``arguments: Any``; tests should not crash on lists / strs."""
    e = make_event(
        type=TOOL_CALL,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        tool_call_id="t1",
        tool_name="bash",
        arguments="ls -la",
    )
    out = scrub(e)
    # Short string — not truncated.
    assert out["arguments"] == "ls -la"


def test_scrub_passes_through_unknown_event_types() -> None:
    """Tool-call-specific rules only fire on ``type == 'tool.call'``.
    Other events get only the secret-redaction pass."""
    e = make_event(
        type=ASSISTANT_MESSAGE,
        run_id="r",
        iter=1,
        ts=_fixed_ts(),
        content="hi",
    )
    out = scrub(e)
    assert out == e


# ---------------------------------------------------------------------------
# map_sdk_event — one example per supported SDK event
# ---------------------------------------------------------------------------


def _wrap_sdk(event_type: SessionEventType, data: Any) -> SessionEvent:
    return SessionEvent(
        data=data,
        id=uuid4(),
        timestamp=_fixed_ts(),
        type=event_type,
    )


def test_map_sdk_event_session_start_returns_session_created() -> None:
    sdk = _wrap_sdk(
        SessionEventType.SESSION_START,
        SessionStartData(
            copilot_version="1.0.0",
            producer="cli",
            session_id="sess-123",
            start_time=_fixed_ts(),
            version=1.0,
            selected_model="claude-opus-4.7-xhigh",
        ),
    )
    out = map_sdk_event(sdk)
    assert out is not None
    assert out["type"] == SESSION_CREATED
    assert out["session_id"] == "sess-123"
    assert out["model"] == "claude-opus-4.7-xhigh"


def test_map_sdk_event_session_idle_returns_session_idle_with_aborted() -> None:
    sdk = _wrap_sdk(
        SessionEventType.SESSION_IDLE,
        SessionIdleData(aborted=False),
    )
    out = map_sdk_event(sdk)
    assert out is not None
    assert out["type"] == SESSION_IDLE
    assert out["aborted"] is False


def test_map_sdk_event_session_idle_aborted_true() -> None:
    sdk = _wrap_sdk(
        SessionEventType.SESSION_IDLE,
        SessionIdleData(aborted=True),
    )
    out = map_sdk_event(sdk)
    assert out is not None
    assert out["aborted"] is True


def test_map_sdk_event_session_shutdown_returns_session_deleted() -> None:
    sdk = _wrap_sdk(
        SessionEventType.SESSION_SHUTDOWN,
        SessionShutdownData(
            code_changes=ShutdownCodeChanges(
                files_modified=[],
                lines_added=0,
                lines_removed=0,
            ),
            model_metrics={},
            session_start_time=0,
            shutdown_type=ShutdownType.ROUTINE,
            total_api_duration=timedelta(0),
        ),
    )
    out = map_sdk_event(sdk)
    assert out is not None
    assert out["type"] == SESSION_DELETED
    assert out["shutdown_type"] == "routine"


def test_map_sdk_event_assistant_message_returns_assistant_message() -> None:
    sdk = _wrap_sdk(
        SessionEventType.ASSISTANT_MESSAGE,
        AssistantMessageData(content="Done.", message_id="msg-1"),
    )
    out = map_sdk_event(sdk)
    assert out is not None
    assert out["type"] == ASSISTANT_MESSAGE
    assert out["content"] == "Done."
    assert out["message_id"] == "msg-1"


def test_map_sdk_event_assistant_reasoning_returns_assistant_reasoning() -> None:
    sdk = _wrap_sdk(
        SessionEventType.ASSISTANT_REASONING,
        AssistantReasoningData(content="thinking...", reasoning_id="r-1"),
    )
    out = map_sdk_event(sdk)
    assert out is not None
    assert out["type"] == ASSISTANT_REASONING
    assert out["content"] == "thinking..."


def test_map_sdk_event_reasoning_delta_returns_none() -> None:
    """Deltas are UX-only, not replay artefacts."""
    sdk = _wrap_sdk(
        SessionEventType.ASSISTANT_REASONING_DELTA,
        AssistantReasoningDeltaData(delta_content="...", reasoning_id="r-1"),
    )
    assert map_sdk_event(sdk) is None


def test_map_sdk_event_tool_execution_start_returns_tool_call() -> None:
    sdk = _wrap_sdk(
        SessionEventType.TOOL_EXECUTION_START,
        ToolExecutionStartData(
            tool_call_id="tc-1",
            tool_name="bash",
            arguments={"command": "echo hi"},
        ),
    )
    out = map_sdk_event(sdk)
    assert out is not None
    assert out["type"] == TOOL_CALL
    assert out["tool_call_id"] == "tc-1"
    assert out["tool_name"] == "bash"
    assert out["arguments"] == {"command": "echo hi"}


def test_map_sdk_event_tool_execution_complete_success_logs_result_size() -> None:
    """Result content is logged by length, not by value — file reads etc.
    can be arbitrarily large and contain user data."""
    sdk = _wrap_sdk(
        SessionEventType.TOOL_EXECUTION_COMPLETE,
        ToolExecutionCompleteData(
            success=True,
            tool_call_id="tc-1",
            result=ToolExecutionCompleteResult(content="x" * 5000),
        ),
    )
    out = map_sdk_event(sdk)
    assert out is not None
    assert out["type"] == TOOL_RESULT
    assert out["success"] is True
    assert out["tool_call_id"] == "tc-1"
    assert out["result_size_chars"] == 5000
    # The content itself MUST NOT appear in the mapped event.
    assert "content" not in out
    assert json.dumps(out).count("x") < 100  # no body smuggling


def test_map_sdk_event_tool_execution_complete_failure_carries_error() -> None:
    sdk = _wrap_sdk(
        SessionEventType.TOOL_EXECUTION_COMPLETE,
        ToolExecutionCompleteData(
            success=False,
            tool_call_id="tc-1",
            error=ToolExecutionCompleteError(message="exit 1", code="non_zero"),
        ),
    )
    out = map_sdk_event(sdk)
    assert out is not None
    assert out["success"] is False
    assert out["error"] == {"message": "exit 1", "code": "non_zero"}


def test_map_sdk_event_assistant_usage_returns_usage_tokens() -> None:
    sdk = _wrap_sdk(
        SessionEventType.ASSISTANT_USAGE,
        AssistantUsageData(
            model="claude-opus-4.7-xhigh",
            input_tokens=1000.0,
            output_tokens=200.0,
        ),
    )
    out = map_sdk_event(sdk)
    assert out is not None
    assert out["type"] == USAGE_TOKENS
    assert out["model"] == "claude-opus-4.7-xhigh"
    assert out["input"] == 1000
    assert out["output"] == 200


def test_map_sdk_event_permission_requested_returns_none() -> None:
    """The session-module permission handler emits the decision event;
    the lifecycle event itself doesn't generate JSONL output."""
    sdk = _wrap_sdk(
        SessionEventType.PERMISSION_REQUESTED,
        _minimal_permission_requested_data(),
    )
    assert map_sdk_event(sdk) is None


def test_map_sdk_event_permission_completed_returns_none() -> None:
    sdk = _wrap_sdk(
        SessionEventType.PERMISSION_COMPLETED,
        PermissionCompletedData(
            request_id="req-1",
            result=PermissionApproved(),
        ),
    )
    assert map_sdk_event(sdk) is None


def test_map_sdk_event_user_input_requested_returns_none() -> None:
    """ask_user lifecycle — session module emits wrapper.ask_user.attempted
    when needed; the raw SDK lifecycle event does not get JSONL'd here."""
    sdk = _wrap_sdk(
        SessionEventType.USER_INPUT_REQUESTED,
        UserInputRequestedData(
            question="continue?",
            request_id="req-1",
        ),
    )
    assert map_sdk_event(sdk) is None


def test_map_sdk_event_abort_returns_none() -> None:
    """Captured indirectly via session.idle's ``aborted`` field."""
    sdk = _wrap_sdk(SessionEventType.ABORT, AbortData(reason="user_cancelled"))
    assert map_sdk_event(sdk) is None


def test_map_sdk_event_unknown_type_returns_none() -> None:
    sdk = _wrap_sdk(
        SessionEventType.SESSION_BACKGROUND_TASKS_CHANGED,
        # The data class for this event has no required fields.
        # We construct it via from_dict to be safe.
        type(
            "FakeData",
            (),
            {"to_dict": lambda self: {}},
        )(),
    )
    assert map_sdk_event(sdk) is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_permission_requested_data() -> Any:
    """A stand-in PermissionRequestedData that's just enough to satisfy
    :func:`SessionEvent`'s typed fields without dragging the SDK's full
    permission-request constructor into the test."""
    from copilot.generated.session_events import (
        PermissionRequestedData,
        PermissionRequestRead,
    )

    return PermissionRequestedData(
        permission_request=PermissionRequestRead(intention="read", path="/tmp/x"),
        request_id="req-1",
    )


# ---------------------------------------------------------------------------
# Module imports — AST-enforced allowlist
# ---------------------------------------------------------------------------


def test_events_module_imports_are_constrained() -> None:
    """``events.py`` MUST NOT import third-party code outside the Copilot SDK's
    typed event package. Enforced via AST inspection so any stray import —
    third-party, peer-module from ralph_afk, or relative — fails loudly."""
    source = Path(events_module.__file__).read_text()
    tree = ast.parse(source)
    allow = {
        "__future__",
        "json",
        "re",
        "datetime",
        "typing",
        "copilot.generated.session_events",
    }
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                seen.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, (
                f"events.py contains a relative import (level={node.level})"
            )
            assert node.module is not None, "from-import with no module name"
            seen.add(node.module)
    leaked = seen - allow
    assert not leaked, f"events.py imports non-allowlisted modules: {leaked}"
