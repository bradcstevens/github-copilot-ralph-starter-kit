"""``ralph_afk.session`` — per-iteration SDK Session orchestrator.

This module owns the **per-iteration** SDK :class:`copilot.CopilotSession`
lifecycle and the **permission posture**. It does **not** construct the
parent :class:`copilot.CopilotClient`: that one is long-running (one per
``ralph-afk`` invocation) and owned by ``ralph_afk.loop`` (issue #10),
which passes it down to :class:`IterationSession`.

A fresh ``CopilotSession`` is created per iteration so the Memento Model
is preserved at the model-context level — each iteration starts with a
clean conversation buffer. The session is bound to its
:class:`EventLogWriter` and :class:`Renderer` for the duration of the
iteration; both are owned by the caller (loop).

Public surface
--------------

* :class:`IterationSession` — async context manager. ``__aenter__``
  creates the SDK session, registers the permission handler, subscribes
  to the event stream via ``create_session(on_event=...)``, and returns
  the SDK session so the caller can ``await session.send_and_wait(prompt)``.
  ``__aexit__`` cleanly disconnects.
* :func:`build_permission_handler` — factory that returns a sync
  :class:`PermissionHandlerFn` closing over the deny lists and a
  ``record_event`` callback. The handler approves every request by
  default; denies tools in ``deny_tools``; denies ``skill`` tool calls
  whose ``arguments.skill`` is in ``deny_skills``; and always denies
  ``ask_user`` (emitting :data:`WRAPPER_ASK_USER_ATTEMPTED` so the
  operator can spot un-triaged issues).
* :data:`ASK_USER_TOOL_NAME`, :data:`SKILL_TOOL_NAME` — string literals
  the permission handler dispatches on. Exposed so tests and callers
  reference one canonical source.

Permission posture
------------------

Mirrors the PRD's "approve-all by default, opt-in deny-list" model:

================  ==================================================
Posture           Behaviour
================  ==================================================
Default           Every request approved (``approve-once``); a
                  :data:`TOOL_PERMISSION_REQUESTED` JSONL event is
                  emitted with tool name and scrubbed arguments.
``--deny-tool``   The named tool is rejected with reason
                  ``"tool_in_deny_list"``; a
                  :data:`TOOL_PERMISSION_DENIED` event is emitted.
``--deny-skill``  ``skill`` tool requests whose ``arguments.skill`` is
                  in the deny list are rejected with reason
                  ``"skill_in_deny_list"``; the skill name is included
                  in the emitted event.
``ask_user``      Always rejected. The wrapper emits
                  :data:`WRAPPER_ASK_USER_ATTEMPTED` (**not**
                  :data:`TOOL_PERMISSION_DENIED`) so the operator can
                  distinguish "agent needed input" from "operator
                  explicitly denied".
================  ==================================================

The :class:`IterationSession` **never registers** an
``on_user_input_request`` handler. With no handler, the SDK does not
enable the ``ask_user`` tool in the first place. The two
defence-in-depth paths exist because:

1. If the SDK ever broadcast a :data:`USER_INPUT_REQUESTED` event
   anyway (e.g. due to a custom-tool registration that re-exposed
   ``ask_user``), the event subscriber translates it to
   :data:`WRAPPER_ASK_USER_ATTEMPTED`.
2. If the agent attempts ``ask_user`` via the regular tool/permission
   pathway (e.g. by name-spoofing), the permission handler catches it
   on ``tool_name``.

Design notes
------------

* **No coupling to peer modules.** The session module knows about the
  SDK, the events module, the persist module (for the writer **type**),
  and the renderer (for fan-out). It explicitly does **not** import
  ``ralph_afk.gh`` / ``ralph_afk.git`` / ``ralph_afk.loop`` / ``ralph_afk.cli``
  / ``ralph_afk.config`` / ``ralph_afk.wrapper`` / ``ralph_afk.pricing``.
  Enforced by ``tests/test_session.py::test_session_module_imports_are_constrained``.
* **CopilotClient is not constructed here.** ``ralph_afk.loop`` owns the
  one-per-invocation client; ``IterationSession`` only consumes it.
  Enforced by an AST scan in
  ``tests/test_session.py::test_session_module_does_not_construct_copilot_client``.
* **``_record`` scrubs once.** Both the JSONL writer's internal scrub
  AND the renderer's downstream consumers would otherwise see un-scrubbed
  envelopes. The single :func:`ralph_afk.events.scrub` call at the
  fan-out point makes the renderer safe and trips the writer's
  redundant-but-idempotent scrub.
* **Recording failures cannot alter permission decisions.** Inside the
  permission handler we route every ``record_event`` call through
  :func:`_safe_record` which swallows exceptions. The SDK's permission
  bus interprets a raised handler exception as ``user-not-available``,
  which would silently demote our intended ``approve-once`` to a
  rejection — so logging errors must never propagate from the handler.
* **Permission timestamp is decision-time, not SDK-broadcast time.**
  The permission handler receives a :class:`PermissionRequest` but not
  the SDK :class:`SessionEvent` that broadcast it, so the synthesised
  ``ts`` field is the moment we made the call. This is "good enough"
  for replay; sub-second causal ordering is best read from the SDK's
  own :data:`PERMISSION_REQUESTED` event (which we drop here per
  :func:`events.map_sdk_event`'s spec).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from copilot import CopilotClient, CopilotSession
from copilot.generated.rpc import (
    PermissionDecisionApproveOnce,
    PermissionDecisionReject,
)
from copilot.generated.session_events import (
    PermissionRequest,
    SessionEvent,
    SessionEventType,
)
from copilot.session import PermissionRequestResult

from ralph_afk import events
from ralph_afk.persist import EventLogWriter
from ralph_afk.ui.renderer import Renderer

__all__ = [
    "IterationSession",
    "build_permission_handler",
    "PermissionHandlerFn",
    "SessionConfig",
    "ASK_USER_TOOL_NAME",
    "SKILL_TOOL_NAME",
]


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Tool-name literal the agent uses to invoke the disabled-in-AFK
# user-input tool. Centralised here so any future SDK rename is a
# one-line change.
ASK_USER_TOOL_NAME: str = "ask_user"

# Tool-name literal the agent uses to invoke a skill. The renderer's
# skill-detection uses the same literal (``ralph_afk.ui.renderer``).
SKILL_TOOL_NAME: str = "skill"

# Reasons attached to ``tool.permission_denied`` events so log
# consumers can distinguish the two deny pathways without re-parsing
# the deny lists.
_REASON_TOOL_DENY: str = "tool_in_deny_list"
_REASON_SKILL_DENY: str = "skill_in_deny_list"


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# Sync-only permission handler. The SDK's underlying type allows either
# sync return or awaitable; we keep ours strictly sync because the
# decision logic is non-blocking and tests can invoke the handler
# directly without an event loop.
PermissionHandlerFn = Callable[
    [PermissionRequest, dict[str, str]], PermissionRequestResult
]


@runtime_checkable
class SessionConfig(Protocol):
    """Shape of the configuration :class:`IterationSession` reads.

    :class:`ralph_afk.config.RunConfig` (issue #10) satisfies this
    structurally — we don't import it here to keep the dependency
    direction one-way (loop → session, never the reverse).

    Attributes:
        deny_tools: Tool names that should be denied at the SDK
            permission gate. Empty by default (parity with ``copilot --yolo``).
        deny_skills: ``skill``-tool ``arguments.skill`` values that
            should be denied. Empty by default.
        verbosity: 0-3 verbosity level; consumed by the renderer.
            Unused inside the session module itself but kept on the
            protocol so a single object can be passed around.
        render_reasoning: Whether reasoning events are rendered.
            Same caveat as ``verbosity``.
    """

    deny_tools: frozenset[str]
    deny_skills: frozenset[str]
    verbosity: int
    render_reasoning: bool


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _scrub_permission_args(args: Any, tool_name: str) -> Any:
    """Apply ``tool.call``-shaped scrubbing to permission-request args.

    :func:`ralph_afk.events.scrub` only applies the load-bearing
    tool-args rules (file-content stripping, >200-char truncation,
    ``gh issue close --comment`` body replacement) when
    ``event["type"] == TOOL_CALL``. Permission events have a different
    type literal, so we wrap the args in a temporary ``TOOL_CALL``
    envelope, run them through the scrubber, and extract them back out.

    Args:
        args: The raw ``tool_args`` from a :class:`PermissionRequest`.
            May be a dict, str, list, or ``None``.
        tool_name: The tool name; used so ``edit`` / ``create`` content
            fields are stripped.

    Returns:
        Scrubbed args ready for inclusion in a JSONL envelope.
    """
    fake: dict[str, Any] = {
        "type": events.TOOL_CALL,
        "tool_name": tool_name,
        "arguments": args,
    }
    return events.scrub(fake).get("arguments")


def _safe_record(
    record_event: Callable[[dict[str, Any]], None], envelope: dict[str, Any]
) -> None:
    """Best-effort fan-out wrapper used inside the permission handler.

    Any exception from ``record_event`` is swallowed. This is **load-
    bearing**: the SDK's permission bus turns a raised handler exception
    into a ``user-not-available`` result, which would silently demote
    our intended approve/reject into a third state. A logging error
    must never alter a permission decision.
    """
    try:
        record_event(envelope)
    except Exception:
        # Intentional broad except — see docstring.
        pass


def _request_identity(req: PermissionRequest) -> tuple[str, Any, str | None]:
    """Extract ``(tool_name, tool_args, tool_call_id)`` from a request.

    SDK 1.0 replaced the single flat ``PermissionRequest`` dataclass with
    a discriminated union of per-category variants
    (``PermissionRequestShell``, ``PermissionRequestWrite``,
    ``PermissionRequestCustomTool``, ``PermissionRequestMcp``,
    ``PermissionRequestHook``, …). The fields the deny-list logic needs
    are spread unevenly across those variants:

    * ``tool_call_id`` — present on every variant.
    * ``tool_name`` — present only on the ``Mcp`` / ``CustomTool`` /
      ``Hook`` variants. The built-in tool variants (shell, write, read,
      url, memory) are identified by their *type*, not a name string, so
      they expose **no** ``tool_name``.
    * tool arguments — the ``Hook`` variant calls the field ``tool_args``;
      ``Mcp`` / ``CustomTool`` call it ``args``; the built-in variants
      carry neither (their parameters live in variant-specific fields
      like ``commands`` or ``diff``).

    Reading defensively via :func:`getattr` keeps the two operationally
    important deny pathways intact across the union:

    * **skill deny** — the ``skill`` meta-tool surfaces as a
      ``CustomTool`` with ``tool_name == "skill"`` and an ``args`` dict,
      both of which this extractor recovers.
    * **named-tool deny** — any tool that carries a ``tool_name``.

    **Known degradation:** ``--deny-tool``/``RALPH_DENY_TOOLS`` entries
    that name a *built-in* tool (e.g. ``bash``) no longer match, because
    those requests arrive as nameless variants. Approve-all remains the
    documented default (the loop is ``--yolo``-equivalent) and the deny
    lists are opt-in, so this is an accepted behaviour change rather than
    a fragile attempt to re-derive synthetic built-in tool names.
    """
    tool_name = getattr(req, "tool_name", None) or ""
    tool_args = getattr(req, "tool_args", None)
    if tool_args is None:
        tool_args = getattr(req, "args", None)
    tool_call_id = getattr(req, "tool_call_id", None)
    return tool_name, tool_args, tool_call_id


# ---------------------------------------------------------------------------
# Permission handler factory
# ---------------------------------------------------------------------------


def build_permission_handler(
    *,
    deny_tools: frozenset[str] = frozenset(),
    deny_skills: frozenset[str] = frozenset(),
    record_event: Callable[[dict[str, Any]], None],
    run_id: str,
    iter_provider: Callable[[], int | None],
) -> PermissionHandlerFn:
    """Return a sync permission handler with the configured deny policy.

    The handler is closed over ``record_event`` and called once per
    SDK permission request. It returns a :class:`PermissionRequestResult`
    and synchronously emits a JSONL-envelope-shaped event capturing the
    decision.

    The fan-out target is supplied by the caller (``IterationSession``
    wires it to a writer + renderer pair), so this factory remains
    decoupled from concrete I/O.

    Args:
        deny_tools: Tool names to reject. Empty set means "approve all".
        deny_skills: ``skill``-tool ``arguments.skill`` values to reject.
        record_event: Callback invoked with the envelope for every
            permission decision. Recording failures are swallowed
            (see :func:`_safe_record`) so a bad writer cannot demote
            an approve to ``user-not-available``.
        run_id: 26-char ULID for the run; flows into every envelope's
            ``run_id`` field.
        iter_provider: Callable returning the current iteration number.
            A callable (not a snapshot int) so a single handler can
            survive multiple iterations if the loop ever recycles them
            — not the current usage, but a future-proof seam.

    Returns:
        A sync permission handler conforming to
        :data:`PermissionHandlerFn`.
    """

    def handler(
        req: PermissionRequest, _invocation: dict[str, str]
    ) -> PermissionRequestResult:
        tool_name, tool_args, tool_call_id = _request_identity(req)
        iter_num = iter_provider()
        scrubbed_args = _scrub_permission_args(tool_args, tool_name)

        # 1) ask_user — always deny; emit wrapper.ask_user.attempted
        #    (not tool.permission_denied) so the operator can
        #    distinguish "agent needs input" from "operator denied".
        if tool_name == ASK_USER_TOOL_NAME:
            envelope = events.make_event(
                type=events.WRAPPER_ASK_USER_ATTEMPTED,
                run_id=run_id,
                iter=iter_num,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=scrubbed_args,
            )
            _safe_record(record_event, envelope)
            return PermissionDecisionReject()

        # 2) explicit tool deny list
        if tool_name in deny_tools:
            envelope = events.make_event(
                type=events.TOOL_PERMISSION_DENIED,
                run_id=run_id,
                iter=iter_num,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=scrubbed_args,
                reason=_REASON_TOOL_DENY,
            )
            _safe_record(record_event, envelope)
            return PermissionDecisionReject()

        # 3) skill deny list — only applies when tool_name == "skill"
        #    and the skill argument is in the deny set.
        if tool_name == SKILL_TOOL_NAME and isinstance(tool_args, dict):
            skill_name = tool_args.get("skill")
            if isinstance(skill_name, str) and skill_name in deny_skills:
                envelope = events.make_event(
                    type=events.TOOL_PERMISSION_DENIED,
                    run_id=run_id,
                    iter=iter_num,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    arguments=scrubbed_args,
                    reason=_REASON_SKILL_DENY,
                    skill=skill_name,
                )
                _safe_record(record_event, envelope)
                return PermissionDecisionReject()

        # 4) default — approve and audit-log
        envelope = events.make_event(
            type=events.TOOL_PERMISSION_REQUESTED,
            run_id=run_id,
            iter=iter_num,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=scrubbed_args,
        )
        _safe_record(record_event, envelope)
        return PermissionDecisionApproveOnce()

    return handler


# ---------------------------------------------------------------------------
# IterationSession — per-iteration SDK Session orchestrator
# ---------------------------------------------------------------------------


class IterationSession:
    """Async context manager owning one iteration's SDK Session.

    The parent :class:`CopilotClient` is **not** constructed here; the
    loop slice (issue #10) constructs it once per ``ralph-afk``
    invocation and passes it to each per-iteration ``IterationSession``.

    Lifecycle::

        async with IterationSession(
            client,
            config=run_config,
            event_log=writer,
            renderer=renderer,
            run_id=run_id,
            iter_num=3,
            model="claude-opus-4.8",
            reasoning_effort="max",
        ) as session:
            await session.send_and_wait(prompt)

    On entry: builds the permission handler, calls
    :meth:`CopilotClient.create_session` with the handler **and** the
    SDK's ``on_event`` parameter (so early events like ``SESSION_START``
    aren't missed), and returns the :class:`CopilotSession`.

    On exit: :meth:`CopilotSession.disconnect` is awaited regardless of
    whether the body raised. Disconnect is idempotent on the SDK side
    and additionally clears all in-memory handlers, so no explicit
    unsubscribe is needed.

    Attributes:
        client: The long-running :class:`CopilotClient`. Reused across
            iterations.
        config: A :class:`SessionConfig`-conforming object (typically
            ``ralph_afk.config.RunConfig``).
        event_log: The :class:`EventLogWriter` for replay-grade JSONL.
        renderer: The :class:`Renderer` for live terminal output.
        run_id: 26-char ULID for the run.
        iter_num: 1-based iteration index.
        model: Optional model override; forwarded to the SDK. A bare base
            model id (model id and reasoning effort are separate axes;
            :mod:`ralph_afk.cli` strips any ``-<effort>`` suffix before
            this point).
        reasoning_effort: Optional reasoning-effort override forwarded to
            the SDK as ``create_session(reasoning_effort=...)``. ``None``
            means *do not send* the ``reasoningEffort`` field — the
            service then applies its own default. :mod:`ralph_afk.cli`
            resolves and per-model-gates the value (a reasoning-incapable
            model such as ``claude-haiku-4.5`` is sent ``None`` because
            the CLI hard-rejects ``session.create`` otherwise).
    """

    def __init__(
        self,
        client: CopilotClient,
        *,
        config: SessionConfig,
        event_log: EventLogWriter,
        renderer: Renderer,
        run_id: str,
        iter_num: int,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._event_log = event_log
        self._renderer = renderer
        self._run_id = run_id
        self._iter_num = iter_num
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._sdk_session: CopilotSession | None = None

    @property
    def sdk_session(self) -> CopilotSession:
        """The active SDK :class:`CopilotSession`.

        Raises:
            RuntimeError: If accessed before ``__aenter__`` or after
                ``__aexit__``.
        """
        if self._sdk_session is None:
            raise RuntimeError(
                "IterationSession is not active; access sdk_session only "
                "inside `async with IterationSession(...) as session:`."
            )
        return self._sdk_session

    async def __aenter__(self) -> CopilotSession:
        """Create the SDK session, register handlers, return the session.

        The permission handler is registered via the ``on_permission_request``
        kwarg. Event subscription uses ``on_event=`` (passed directly to
        ``create_session``), not a post-create ``session.on(...)`` call,
        so early events such as :data:`SessionEventType.SESSION_START`
        are delivered.
        """
        handler = build_permission_handler(
            deny_tools=self._config.deny_tools,
            deny_skills=self._config.deny_skills,
            record_event=self._record,
            run_id=self._run_id,
            iter_provider=lambda: self._iter_num,
        )
        session = await self._client.create_session(
            on_permission_request=handler,
            on_event=self._on_sdk_event,
            model=self._model,
            reasoning_effort=self._reasoning_effort,
            # NB: on_user_input_request is intentionally NOT set.
            # Leaving it None tells the SDK to not enable ask_user; the
            # permission handler is the second line of defence.
        )
        self._sdk_session = session
        return session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Disconnect the SDK session cleanly.

        ``disconnect()`` is idempotent on the SDK and clears all handler
        registrations internally, so we don't need to manually
        unsubscribe. Disconnect-time exceptions are swallowed so a
        crashed SDK doesn't mask a body-level exception (the original
        ``exc_val`` propagates naturally because we don't return True).
        """
        session = self._sdk_session
        self._sdk_session = None
        if session is None:
            return
        try:
            await session.disconnect()
        except Exception:
            # See docstring — swallowing keeps body-level exception
            # propagation intact and avoids confusing tracebacks.
            pass

    # -- event fan-out -----------------------------------------------------

    def _record(self, envelope: dict[str, Any]) -> None:
        """Scrub once, then fan out to JSONL writer + renderer.

        Both downstream calls are guarded so a writer or renderer
        failure cannot crash the SDK callback dispatch (or, when called
        from the permission handler, alter the permission decision).
        """
        scrubbed = events.scrub(envelope)
        try:
            self._event_log.write(scrubbed)
        except Exception:
            pass
        try:
            self._renderer.render(scrubbed)
        except Exception:
            pass

    def _on_sdk_event(self, sdk_event: SessionEvent) -> None:
        """Route an SDK event to ``_record``.

        :data:`SessionEventType.USER_INPUT_REQUESTED` is the one event
        we synthesise into a wrapper-level
        :data:`WRAPPER_ASK_USER_ATTEMPTED` envelope (defence in depth;
        we never register ``on_user_input_request`` so the SDK
        shouldn't enable ``ask_user`` anyway, but the handler is here
        in case a future SDK release or custom-tool registration
        re-exposes the path).

        Every other event goes through :func:`events.map_sdk_event`;
        a ``None`` return drops the event (streaming deltas,
        permission lifecycle events, etc.).

        Text streaming deltas (``assistant.reasoning_delta`` /
        ``assistant.message_delta``) are intercepted *before*
        :func:`events.map_sdk_event` and forwarded straight to the renderer
        for live terminal output. They are deliberately NOT routed through
        :meth:`_record`, so they never reach the JSONL writer — the
        replay-grade log carries only the final, scrubbed
        :data:`ASSISTANT_REASONING` / :data:`ASSISTANT_MESSAGE` events.
        (``assistant.streaming_delta`` carries only a byte count, no text, so
        it falls through to the drop path.) Renderer failures are swallowed:
        a broken renderer must not crash SDK event dispatch.
        """
        if sdk_event.type is SessionEventType.ASSISTANT_REASONING_DELTA:
            try:
                delta: Any = getattr(sdk_event.data, "delta_content", "") or ""
                self._renderer.stream_reasoning(delta)
            except Exception:
                pass
            return
        if sdk_event.type is SessionEventType.ASSISTANT_MESSAGE_DELTA:
            try:
                delta = getattr(sdk_event.data, "delta_content", "") or ""
                self._renderer.stream_message(delta)
            except Exception:
                pass
            return

        if sdk_event.type is SessionEventType.USER_INPUT_REQUESTED:
            data = sdk_event.data
            question = getattr(data, "question", "") or ""
            request_id = getattr(data, "request_id", "") or ""
            envelope = events.make_event(
                type=events.WRAPPER_ASK_USER_ATTEMPTED,
                run_id=self._run_id,
                iter=self._iter_num,
                ts=sdk_event.timestamp,
                question=question,
                request_id=request_id,
            )
            self._record(envelope)
            return

        payload = events.map_sdk_event(sdk_event)
        if payload is None:
            return
        envelope = events.make_event(
            type=payload["type"],
            run_id=self._run_id,
            iter=self._iter_num,
            ts=sdk_event.timestamp,
            **{k: v for k, v in payload.items() if k != "type"},
        )
        self._record(envelope)
