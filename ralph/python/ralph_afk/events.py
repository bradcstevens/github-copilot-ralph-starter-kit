"""``ralph_afk.events`` — JSONL event envelope, scrubber, SDK mapping.

This module is **deep and pure**: no I/O, no clock side effects except
:func:`make_event`'s default ``ts``, no third-party imports outside the
Copilot SDK's typed event package. It is the canonical source of the
JSONL envelope shape — both wrapper-level events and SDK-derived events
flow through here on their way to disk. The file writing itself lives in
``ralph_afk.persist`` (issue #7).

Every JSONL event line shares this envelope::

    {"ts": "2026-05-16T00:00:00.000Z",
     "run_id": "01HXR...",
     "iter": 3,
     "type": "...",
     ...payload...}

Public surface:

* :func:`make_event` — construct an envelope-conformant event dict.
* :func:`to_jsonl_line` — serialise a single event to one ``\\n``-terminated
  JSON line, with the scrubber pipeline applied first.
* :func:`scrub` — return a scrubbed copy of an event dict. Idempotent.
* :func:`map_sdk_event` — translate a typed SDK :class:`SessionEvent` to
  a JSONL payload dict, or ``None`` for events with no replay equivalent
  (streaming deltas, permission lifecycle events handled by the session
  module, etc.).
* Event-type constants (string literals) for every event the wrapper and
  the SDK-mapping path emit. The string literals — not just the constant
  names — are the contract that downstream tooling (renderer, run-summary,
  external log consumers) reads.

Design notes:

* **Determinism.** :func:`to_jsonl_line` emits keys in a stable order
  (envelope keys first in the documented sequence, then payload keys
  sorted alphabetically) so log diffs across runs are stable and grep
  patterns over multi-day logs remain reliable.
* **Scrubber is the last gate.** Every event written through
  :func:`to_jsonl_line` is scrubbed regardless of how it was constructed;
  callers cannot accidentally bypass it.
* **Idempotent scrubbing.** Running :func:`scrub` twice produces the
  same output. This matters because the persist module (#7) is documented
  as routing events through ``events.scrub`` *then* ``events.to_jsonl_line``;
  the second pass inside :func:`to_jsonl_line` is a no-op.
* **Truncation is value-replacement, not slicing.** Over-length tool args
  become the literal string ``"<truncated: N chars>"`` (with ``N`` = the
  original JSON-serialised length). Slicing would leave half-tokens in
  scrollback and break secret regexes that depend on whole-token matches.
* **stdlib + SDK typed events only.** Enforced by
  ``tests/test_events.py::test_events_module_imports_are_constrained``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from copilot.generated.session_events import SessionEvent, SessionEventType

__all__ = [
    # Wrapper event-type constants
    "WRAPPER_RUN_START",
    "WRAPPER_RUN_END",
    "WRAPPER_ITERATION_START",
    "WRAPPER_ITERATION_END",
    "WRAPPER_AFK_READY_COLLECTED",
    "WRAPPER_CHECKPOINT_RECORDED",
    "WRAPPER_COMMIT_RECORDED",
    "WRAPPER_AUTO_CLOSE",
    "WRAPPER_PR_ADVANCED",
    "WRAPPER_STRIKE",
    "WRAPPER_ASK_USER_ATTEMPTED",
    # SDK-mapped event-type constants
    "SESSION_CREATED",
    "SESSION_IDLE",
    "SESSION_DELETED",
    "ASSISTANT_MESSAGE",
    "ASSISTANT_REASONING",
    "TOOL_CALL",
    "TOOL_RESULT",
    "TOOL_PERMISSION_REQUESTED",
    "TOOL_PERMISSION_DENIED",
    "USAGE_TOKENS",
    # Functions
    "make_event",
    "to_jsonl_line",
    "scrub",
    "map_sdk_event",
    # Sentinels / placeholders (exported for tests + renderer)
    "REDACTED_SECRET",
    "MAX_TOOL_ARGS_CHARS",
]

# ---------------------------------------------------------------------------
# Event-type string literals
# ---------------------------------------------------------------------------
# Wrapper-emitted events. The wrapper constructs these directly via
# :func:`make_event`; they have no SDK equivalent.
WRAPPER_RUN_START = "wrapper.run.start"
WRAPPER_RUN_END = "wrapper.run.end"
WRAPPER_ITERATION_START = "wrapper.iteration.start"
WRAPPER_ITERATION_END = "wrapper.iteration.end"
WRAPPER_AFK_READY_COLLECTED = "wrapper.afk_ready.collected"
WRAPPER_CHECKPOINT_RECORDED = "wrapper.checkpoint.recorded"
WRAPPER_COMMIT_RECORDED = "wrapper.commit.recorded"
WRAPPER_AUTO_CLOSE = "wrapper.auto_close"
WRAPPER_PR_ADVANCED = "wrapper.pr.advanced"
WRAPPER_STRIKE = "wrapper.strike"
WRAPPER_ASK_USER_ATTEMPTED = "wrapper.ask_user.attempted"

# SDK-mapped events. :func:`map_sdk_event` translates SDK :class:`SessionEvent`
# instances to payload dicts using these type literals.
SESSION_CREATED = "session.created"
SESSION_IDLE = "session.idle"
SESSION_DELETED = "session.deleted"
ASSISTANT_MESSAGE = "assistant.message"
ASSISTANT_REASONING = "assistant.reasoning"
TOOL_CALL = "tool.call"
TOOL_RESULT = "tool.result"
TOOL_PERMISSION_REQUESTED = "tool.permission_requested"
TOOL_PERMISSION_DENIED = "tool.permission_denied"
USAGE_TOKENS = "usage.tokens"

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

# Envelope keys, in the order :func:`to_jsonl_line` emits them. Keeping
# this declarative (rather than a hardcoded if/elif chain inside
# :func:`to_jsonl_line`) makes the contract auditable from one place.
_ENVELOPE_KEY_ORDER: tuple[str, ...] = ("ts", "run_id", "iter", "type")

# Replacement placeholders. Kept as constants so tests can grep for them
# without hardcoding the format string in two places.
REDACTED_SECRET = "<redacted-secret>"
_TRUNCATED_TEMPLATE = "<truncated: {n} chars>"
_COMMENT_TEMPLATE = "<comment: {n} chars>"

# Tool-args truncation threshold (issue #5 acceptance criterion).
MAX_TOOL_ARGS_CHARS = 200

# File-writing tools whose content fields must never reach the JSONL log.
# Covers both the CLI tool names exposed to the agent (``edit``, ``create``)
# and the issue spec's literal wording (``edit_file``, ``create_file``).
# Pre-existing aliases (``Write``, ``Edit``) are included to keep the scrub
# robust against minor SDK / agent renames.
_FILE_WRITING_TOOLS: frozenset[str] = frozenset(
    {
        "edit",
        "edit_file",
        "create",
        "create_file",
        "Write",
        "Edit",
    }
)

# Field names whose values represent file content (i.e. potentially huge,
# user-data-bearing). Stripped entirely from tool.call events for tools in
# :data:`_FILE_WRITING_TOOLS`.
_FILE_CONTENT_FIELDS: frozenset[str] = frozenset(
    {
        "content",
        "file_text",
        "old_str",
        "new_str",
        "old_string",
        "new_string",
    }
)

# ---------------------------------------------------------------------------
# Compiled secret-redaction regexes
# ---------------------------------------------------------------------------
#
# Pre-compiled at import time so :func:`scrub` is O(events) not
# O(events × regex-compile). Patterns are deliberately conservative —
# false positives on real conversation text would corrupt replay logs, so
# every pattern targets the canonical issued-token shape.

# GitHub fine-grained / classic personal access tokens are ``ghp_`` plus
# 36+ alphanumeric chars (40 total minimum). The issue spec wording
# ("≥40 char") matches.
_RE_GHP_TOKEN = re.compile(r"ghp_[A-Za-z0-9]{36,}")

# GitHub OAuth tokens follow the same shape with the ``gho_`` prefix.
_RE_GHO_TOKEN = re.compile(r"gho_[A-Za-z0-9]{36,}")

# JWT-shaped strings: three base64url segments separated by dots, all
# starting with the canonical ``eyJ`` header prefix (base64url of ``{"``
# — every standards-compliant JWT begins this way). Each segment must be
# at least 20 chars to avoid false positives on dotted identifiers.
_RE_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{17,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")

# AWS classic access-key IDs are ``AKIA`` plus 16 uppercase-alnum chars.
_RE_AWS_KEY = re.compile(r"AKIA[0-9A-Z]{16}")

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    _RE_GHP_TOKEN,
    _RE_GHO_TOKEN,
    _RE_JWT,
    _RE_AWS_KEY,
)

# `gh issue close N --comment "<body>"` — match the comment value and
# replace it with a length-aware sentinel. Handles both single- and
# double-quoted comment bodies, plus the ``--comment=value`` form. The
# pattern is intentionally scoped to ``gh issue (close|comment)`` so we
# do not corrupt unrelated ``--comment`` flags from other tools.
_RE_GH_COMMENT = re.compile(
    r"(gh\s+issue\s+(?:close|comment)\b[^\n]*?--comment(?:-file)?[=\s]+)"
    r"(\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<bare>\S+))",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def make_event(
    type: str,
    run_id: str,
    iter: int | None,
    *,
    ts: datetime | None = None,
    **payload: Any,
) -> dict[str, Any]:
    """Construct an envelope-conformant event dict.

    Args:
        type: The event-type literal. Use one of the module-level
            constants (``WRAPPER_*`` or the SDK-mapped names) so misspellings
            fail at import time, not at log-replay time.
        run_id: 26-character ULID identifying the ``ralph-afk`` invocation.
            Constructed by the persist factory in issue #7.
        iter: Iteration number (1-based) for iteration-scope events; ``None``
            for run-scope events such as :data:`WRAPPER_RUN_START` /
            :data:`WRAPPER_RUN_END`.
        ts: Wall-clock timestamp; defaults to :func:`datetime.now` in UTC.
            Tests inject explicit values; SDK-derived events should pass
            ``sdk_event.timestamp`` so the JSONL timestamp matches the SDK's
            own record.
        **payload: Arbitrary payload fields. Envelope keys (``ts``,
            ``run_id``, ``iter``, ``type``) cannot appear here: Python's
            keyword-argument machinery raises :class:`TypeError`
            ("multiple values for keyword argument") if a caller tries
            (e.g. ``make_event(type="x", **{"type": "y"})``), so the
            collision check is automatic.

    Returns:
        A new dict carrying the envelope keys plus ``payload``.
    """
    if ts is None:
        ts = datetime.now(timezone.utc)
    return {
        "ts": _format_ts(ts),
        "run_id": run_id,
        "iter": iter,
        "type": type,
        **payload,
    }


def to_jsonl_line(event: dict[str, Any]) -> str:
    """Serialise an event to one ``\\n``-terminated JSON line.

    The scrubber pipeline (:func:`scrub`) is always applied before
    serialisation — callers cannot bypass it by constructing an event by
    hand and writing it directly. Keys are emitted in a deterministic
    order: envelope keys in the canonical sequence (``ts``, ``run_id``,
    ``iter``, ``type``), then payload keys sorted alphabetically.

    Args:
        event: An event dict, typically from :func:`make_event`.

    Returns:
        The serialised JSON line, ending in ``"\\n"``.
    """
    scrubbed = scrub(event)
    ordered: dict[str, Any] = {}
    for k in _ENVELOPE_KEY_ORDER:
        if k in scrubbed:
            ordered[k] = scrubbed[k]
    for k in sorted(scrubbed.keys()):
        if k in _ENVELOPE_KEY_ORDER:
            continue
        ordered[k] = scrubbed[k]
    return json.dumps(ordered, ensure_ascii=False, default=_json_default) + "\n"


def scrub(event: dict[str, Any]) -> dict[str, Any]:
    """Return a scrubbed copy of ``event``.

    Applies, in order:

    1. **Tool-call rules** (only when ``event["type"] == "tool.call"``):

       * For tools in :data:`_FILE_WRITING_TOOLS` (``edit``, ``edit_file``,
         ``create``, ``create_file``, ``Write``, ``Edit``), drop every
         field in :data:`_FILE_CONTENT_FIELDS` from ``arguments`` — paths
         survive, content does not.
       * Any string field inside ``arguments`` named ``command`` is
         further scanned for ``gh issue close --comment "<body>"`` /
         ``gh issue comment ... --comment "<body>"`` patterns; the body
         is replaced with the literal ``<comment: N chars>``.
       * If the JSON-serialised ``arguments`` exceeds
         :data:`MAX_TOOL_ARGS_CHARS`, the entire ``arguments`` field is
         replaced with ``<truncated: N chars>`` where ``N`` is the
         original serialised length. This is *value replacement*, not
         slicing, so half-tokens cannot leak past the boundary.

    2. **Secret redaction** on every string leaf in the event:
       ``ghp_*`` / ``gho_*`` GitHub tokens, JWT-shaped strings, and
       AWS access-key IDs are replaced with :data:`REDACTED_SECRET`.

    Idempotent — applying :func:`scrub` to its own output is a no-op,
    so the persist module (#7) can safely call ``scrub`` and then call
    :func:`to_jsonl_line` (which scrubs again).

    Args:
        event: An event dict. Not mutated.

    Returns:
        A new dict with the rules applied.
    """
    out = dict(event)
    if out.get("type") == TOOL_CALL:
        out = _scrub_tool_call(out)
    return _walk_strings(out, _redact_secrets)


def map_sdk_event(sdk_event: SessionEvent) -> dict[str, Any] | None:
    """Translate a typed SDK :class:`SessionEvent` to a JSONL payload dict.

    The returned dict carries the ``type`` literal plus payload keys, but
    no envelope keys — callers compose with :func:`make_event` to fill
    ``run_id`` / ``iter``, passing the SDK event's own ``timestamp`` as
    ``ts`` so the JSONL record matches the SDK's authoritative wall-clock.

    Returns ``None`` for SDK events that have no JSONL equivalent:

    * Streaming deltas (``assistant.reasoning_delta``,
      ``assistant.message_delta``, ``assistant.streaming_delta``) — these
      are renderer concern; the *final* :data:`ASSISTANT_REASONING` /
      :data:`ASSISTANT_MESSAGE` events carry the replay-grade content.
    * Permission lifecycle (``permission.requested`` /
      ``permission.completed``) — the session module's permission handler
      emits the decision event (:data:`TOOL_PERMISSION_REQUESTED` on
      approve, :data:`TOOL_PERMISSION_DENIED` on deny) so we do not
      double-log.
    * ``user_input.requested`` — handled by the session module, which
      emits :data:`WRAPPER_ASK_USER_ATTEMPTED` instead.
    * ``abort`` — captured indirectly via the paired ``session.idle``
      event's ``aborted`` field.
    * Every other SDK event type the runner does not subscribe to.

    Args:
        sdk_event: A :class:`SessionEvent` from the SDK's event stream.

    Returns:
        A payload dict carrying ``type`` plus event-specific keys, or
        ``None`` if the SDK event has no JSONL equivalent.
    """
    et = sdk_event.type
    data: Any = sdk_event.data

    if et is SessionEventType.SESSION_START:
        return {
            "type": SESSION_CREATED,
            "session_id": data.session_id,
            "model": data.selected_model,
        }
    if et is SessionEventType.SESSION_IDLE:
        return {
            "type": SESSION_IDLE,
            "aborted": bool(data.aborted) if data.aborted is not None else False,
        }
    if et is SessionEventType.SESSION_SHUTDOWN:
        payload: dict[str, Any] = {
            "type": SESSION_DELETED,
            "shutdown_type": _enum_value(data.shutdown_type),
        }
        if data.error_reason is not None:
            payload["error_reason"] = data.error_reason
        return payload
    if et is SessionEventType.ASSISTANT_MESSAGE:
        return {
            "type": ASSISTANT_MESSAGE,
            "content": data.content,
            "message_id": data.message_id,
        }
    if et is SessionEventType.ASSISTANT_REASONING:
        return {
            "type": ASSISTANT_REASONING,
            "content": data.content,
            "reasoning_id": data.reasoning_id,
        }
    if et in (
        SessionEventType.ASSISTANT_REASONING_DELTA,
        SessionEventType.ASSISTANT_MESSAGE_DELTA,
        SessionEventType.ASSISTANT_STREAMING_DELTA,
    ):
        return None
    if et is SessionEventType.TOOL_EXECUTION_START:
        return {
            "type": TOOL_CALL,
            "tool_call_id": data.tool_call_id,
            "tool_name": data.tool_name,
            "arguments": data.arguments,
        }
    if et is SessionEventType.TOOL_EXECUTION_COMPLETE:
        result: dict[str, Any] = {
            "type": TOOL_RESULT,
            "tool_call_id": data.tool_call_id,
            "success": data.success,
        }
        if data.error is not None:
            result["error"] = {
                "message": data.error.message,
                "code": data.error.code,
            }
        if data.result is not None and data.result.content is not None:
            # Log result size, not the result content itself — file reads,
            # bash output, etc. can be arbitrarily large and contain user
            # data we have no business writing to disk.
            result["result_size_chars"] = len(data.result.content)
        return result
    if et is SessionEventType.ASSISTANT_USAGE:
        return {
            "type": USAGE_TOKENS,
            "model": data.model,
            "input": int(data.input_tokens) if data.input_tokens is not None else 0,
            "output": int(data.output_tokens) if data.output_tokens is not None else 0,
        }
    if et in (
        SessionEventType.PERMISSION_REQUESTED,
        SessionEventType.PERMISSION_COMPLETED,
        SessionEventType.USER_INPUT_REQUESTED,
        SessionEventType.ABORT,
    ):
        return None
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_ts(dt: datetime) -> str:
    """Format ``dt`` as ISO-8601 UTC with millisecond precision.

    The PRD spec is ``YYYY-MM-DDTHH:MM:SS.sssZ`` (trailing ``Z``, not
    ``+00:00``; three fractional digits, not six). :meth:`datetime.isoformat`
    gives microseconds by default, so we format manually.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    millis = dt.microsecond // 1000
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{millis:03d}Z"


def _json_default(obj: Any) -> Any:
    """Fallback serializer for ``json.dumps`` defaults.

    Handles the SDK's typed objects (which expose :meth:`to_dict`) and
    :class:`Enum` instances. Falls back to ``str(obj)`` for everything
    else so we never crash a write because of an unexpected type — a
    misformatted event in the log is recoverable; a crashed iteration
    is not.
    """
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    if hasattr(obj, "value"):  # Enum-shaped
        return obj.value
    if isinstance(obj, datetime):
        return _format_ts(obj)
    return str(obj)


def _enum_value(obj: Any) -> Any:
    """Extract the ``.value`` from an Enum-like instance, leaving other
    types untouched. Used by :func:`map_sdk_event` to flatten typed enums
    (e.g. :class:`ShutdownType`) without dragging Enum identity into the
    JSONL log."""
    if obj is None:
        return None
    return getattr(obj, "value", obj)


def _walk_strings(
    node: Any, fn: "callable[[str], str]"
) -> Any:
    """Apply ``fn`` to every string leaf in ``node`` recursively.

    Tuples are coerced to lists (JSON has no tuple type and we never want
    to silently produce a different type than the caller passed in).
    """
    if isinstance(node, str):
        return fn(node)
    if isinstance(node, dict):
        return {k: _walk_strings(v, fn) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk_strings(item, fn) for item in node]
    if isinstance(node, tuple):
        return [_walk_strings(item, fn) for item in node]
    return node


def _redact_secrets(s: str) -> str:
    """Replace every known secret pattern in ``s`` with :data:`REDACTED_SECRET`."""
    out = s
    for pat in _SECRET_PATTERNS:
        out = pat.sub(REDACTED_SECRET, out)
    return out


def _redact_gh_comment(command: str) -> str:
    """Replace ``gh issue close --comment "<body>"`` bodies with a
    length-aware sentinel.

    Handles double-quoted, single-quoted, and bare values. Both
    ``--comment`` and ``--comment-file`` flags are matched; the
    ``--comment-file`` body is the filename, which we still scrub by
    length because filenames inside heredocs can themselves leak secrets.
    """

    def _replace(m: re.Match[str]) -> str:
        prefix = m.group(1)
        body = (
            m.group("dq")
            if m.group("dq") is not None
            else m.group("sq")
            if m.group("sq") is not None
            else m.group("bare")
        )
        placeholder = _COMMENT_TEMPLATE.format(n=len(body))
        if m.group("dq") is not None:
            return f'{prefix}"{placeholder}"'
        if m.group("sq") is not None:
            return f"{prefix}'{placeholder}'"
        return f"{prefix}{placeholder}"

    return _RE_GH_COMMENT.sub(_replace, command)


def _scrub_tool_call(event: dict[str, Any]) -> dict[str, Any]:
    """Apply tool-call-specific scrub rules.

    Idempotent: a second pass over the output is a no-op because content
    fields have already been removed, ``gh issue close`` comments have
    already been replaced with a short sentinel, and the args-truncation
    sentinel is well under :data:`MAX_TOOL_ARGS_CHARS`.
    """
    out = dict(event)
    tool_name = out.get("tool_name", "")
    args = out.get("arguments")

    if isinstance(args, dict):
        new_args: dict[str, Any] = dict(args)

        if tool_name in _FILE_WRITING_TOOLS:
            for field in _FILE_CONTENT_FIELDS:
                new_args.pop(field, None)

        command = new_args.get("command")
        if isinstance(command, str):
            new_args["command"] = _redact_gh_comment(command)

        args = new_args
        out["arguments"] = args

    # Final truncation gate — applies whether ``args`` is a dict, list, str,
    # or scalar. Replaces the entire field; never slices.
    if args is not None and not _is_already_truncated_sentinel(args):
        try:
            serialised = json.dumps(args, sort_keys=True, default=_json_default)
        except (TypeError, ValueError):
            serialised = str(args)
        if len(serialised) > MAX_TOOL_ARGS_CHARS:
            out["arguments"] = _TRUNCATED_TEMPLATE.format(n=len(serialised))

    return out


def _is_already_truncated_sentinel(value: Any) -> bool:
    """Detect ``arguments`` already replaced by the truncation sentinel.

    Used to keep :func:`scrub` idempotent: the second invocation must not
    re-truncate (which would change ``N`` to a smaller number).
    """
    if not isinstance(value, str):
        return False
    return bool(re.fullmatch(r"<truncated: \d+ chars>", value))
