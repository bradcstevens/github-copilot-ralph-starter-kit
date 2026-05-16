"""``ralph_afk.telemetry.otel`` — the OpenTelemetry seam.

A **single switch** module that the rest of ``ralph_afk`` uses for span
emission. Call sites do not branch on whether OTel is enabled — they
just call :func:`span` and let this module decide whether to emit a real
:class:`opentelemetry.trace.Span` or a no-op object.

Activation
----------

OTel is enabled iff **either** of the following is true at process start:

* ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var is set to a non-empty string —
  the standard OTel-ecosystem hook. Operators who already run an OTel
  collector enable Ralph's tracing the same way they enable any other
  Python OTel client.
* ``RALPH_OTEL_ENABLED`` env var is set to a truthy value (``"1"``,
  ``"true"``, ``"yes"``, ``"on"``). Useful for test rigs that install
  an :class:`InMemorySpanExporter` and want to verify the span tree
  shape without paying for a network exporter — and useful for getting
  the SDK subprocess to emit telemetry when only an OTLP collector
  side-car (not a parent endpoint) is configured.

Both unset → no ``opentelemetry`` module is ever imported. Enforced by
``tests/test_smoke.py::test_disabled_otel_does_not_import_opentelemetry``
(subprocess assertion).

Sticky enable
-------------

The enable decision is computed once at first :func:`_init` call and
cached. This protects against mid-run env-var mutations from breaking a
half-emitted span tree (e.g. a test resetting ``RALPH_OTEL_ENABLED``
between iterations of the same loop would otherwise leave the
``ralph_afk.run`` root span open while subsequent ``span()`` calls
became no-ops). :func:`reset_for_tests` exists to clear the cache between
test invocations that intentionally toggle activation state.

Span tree contract
------------------

The runner emits this tree::

    ralph_afk.run                  (root, per ralph-afk invocation)
    └─ ralph_afk.iteration          (attrs: iter, issue, issues)
       ├─ ralph_afk.collect_issues
       ├─ ralph_afk.session         (wraps the SDK session lifecycle)
       │  └─ (SDK-emitted spans nest here automatically when telemetry is on)
       └─ ralph_afk.enforce_closures

SDK-emitted spans nest under ``ralph_afk.session`` because the Copilot
Python SDK's :class:`SubprocessConfig` carries a
:class:`~copilot.client.TelemetryConfig` that, when populated, both (a)
configures the spawned SDK subprocess to emit its own OTel spans via the
configured exporter, and (b) propagates the active W3C trace context
through JSON-RPC ``traceparent`` headers (see
``copilot/client.py``). The runner does not duplicate any span the SDK
already emits — we only wrap entry points where we have business-level
knowledge the SDK doesn't (iteration boundaries, source collection,
closure enforcement).

Call-site usage
---------------

::

    from ralph_afk.telemetry import otel as telemetry

    with telemetry.span("ralph_afk.iteration", iter=iter_num) as sp:
        ...
        # Late-bound attrs work on both real spans and no-op spans:
        sp.set_attribute("issue", first_ref)

    config = telemetry.build_sdk_telemetry_config()
    # Pass to SubprocessConfig.telemetry — None when disabled.

    telemetry.force_flush()
    # Flush the configured processors. No-op when disabled.

Graceful degradation
--------------------

When OTel is enabled but the ``[otel]`` extra is not installed (i.e.
``import opentelemetry`` fails), :func:`_init` catches the
:class:`ImportError`, logs a warning via the ``ralph_afk.telemetry``
logger, and degrades to the disabled posture. Operators with an ambient
``OTEL_EXPORTER_OTLP_ENDPOINT`` env var (set by their shell rc) won't
have their ``uv sync`` install fail — they'll see a one-time warning and
the runner continues without tracing.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Iterator, Optional

__all__ = [
    "is_enabled",
    "span",
    "build_sdk_telemetry_config",
    "force_flush",
    "reset_for_tests",
]


# Module-level state. Kept as a dict (not module-level globals) so
# `reset_for_tests` can clear every cache entry in one operation and so
# the test surface doesn't have to track individual variable names.
_state: dict[str, Any] = {
    "initialised": False,
    "enabled": False,
    "tracer": None,
    "provider": None,
}

_logger = logging.getLogger("ralph_afk.telemetry")


_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def _is_truthy(value: Optional[str]) -> bool:
    """Match the conventional truthy-env-var spelling used elsewhere in the kit."""
    if value is None:
        return False
    return value.strip().lower() in _TRUTHY


def _detect_enabled() -> bool:
    """Compute the enable decision from env vars. Pure; no I/O, no imports."""
    if _is_truthy(os.environ.get("RALPH_OTEL_ENABLED")):
        return True
    endpoint = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    return bool(endpoint)


def _build_tracer() -> Any:
    """Lazily import opentelemetry and build the runner's tracer.

    Called only when OTel is enabled. Raises :class:`ImportError` when
    the ``[otel]`` extra is not installed; the caller (:func:`_init`)
    catches and degrades to no-op posture.

    Behaviour:

    * If a :class:`TracerProvider` has already been installed globally
      (e.g. a test pre-installed one with an
      :class:`InMemorySpanExporter`), reuse it. We need this so tests
      can capture spans without re-installing a provider on every
      invocation.
    * Otherwise, install a fresh :class:`TracerProvider`. If
      ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, attach an
      :class:`OTLPSpanExporter` via :class:`BatchSpanProcessor`. If only
      ``RALPH_OTEL_ENABLED=1`` is set, install a provider with no
      exporters — spans are recorded but never sent anywhere (matches
      "in-process debugging" intent and keeps the SDK subprocess
      telemetry working without forcing an exporter on the parent).
    """
    # Lazy import: only fires when OTel is enabled. The ImportError
    # bubbles to _init() which handles the missing-extras path.
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    existing = trace.get_tracer_provider()
    # In OTel, before any provider is set, get_tracer_provider() returns
    # a `ProxyTracerProvider`. We treat anything that isn't a real
    # `TracerProvider` as "no provider yet" so we install one.
    if isinstance(existing, TracerProvider):
        _state["provider"] = existing
    else:
        provider = TracerProvider()
        endpoint = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
        if endpoint:
            # Lazy: only import the OTLP exporter if we actually need it.
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
            )
        trace.set_tracer_provider(provider)
        _state["provider"] = provider

    return trace.get_tracer("ralph_afk")


def _init() -> None:
    """One-time initialisation; idempotent."""
    if _state["initialised"]:
        return
    enabled = _detect_enabled()
    if enabled:
        try:
            _state["tracer"] = _build_tracer()
            _state["enabled"] = True
        except ImportError as exc:
            # Operator wanted OTel but the [otel] extra isn't installed.
            # Don't crash — degrade gracefully with a one-time warning.
            _logger.warning(
                "OTel was requested (RALPH_OTEL_ENABLED or "
                "OTEL_EXPORTER_OTLP_ENDPOINT is set) but the "
                "[otel] extra is not installed: %s. "
                "Install with `uv sync --project ralph/python --extra otel` "
                "(or `pip install 'ralph-afk[otel]'`) to enable tracing.",
                exc,
            )
            _state["tracer"] = None
            _state["enabled"] = False
        except Exception as exc:
            # Defence-in-depth: any other failure during tracer construction
            # (e.g. exporter rejected the endpoint URL) must not break the
            # runner. Log + degrade.
            _logger.warning(
                "OTel tracer construction failed (%s: %s); "
                "continuing without tracing",
                type(exc).__name__,
                exc,
            )
            _state["tracer"] = None
            _state["enabled"] = False
    else:
        _state["tracer"] = None
        _state["enabled"] = False
    _state["initialised"] = True


def is_enabled() -> bool:
    """Return ``True`` iff OTel is active for this process."""
    _init()
    return bool(_state["enabled"])


# ---------------------------------------------------------------------------
# No-op span — supports the same set_attribute / add_event surface call
# sites use, so call sites don't have to branch on whether OTel is enabled.
# ---------------------------------------------------------------------------


class _NoOpSpan:
    """Stand-in :class:`Span`-shaped object yielded when OTel is disabled.

    Implements only the slice of :class:`opentelemetry.trace.Span`'s API
    the runner uses: :meth:`set_attribute`, :meth:`set_attributes`, and
    :meth:`add_event`. Any unused methods are deliberately absent so
    accidental drift (e.g. a call site using a Span method we don't
    no-op) trips an :class:`AttributeError` in tests immediately.
    """

    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: D401
        """No-op."""
        return None

    def set_attributes(self, attrs: dict[str, Any]) -> None:  # noqa: D401
        """No-op."""
        return None

    def add_event(self, name: str, attributes: Optional[dict[str, Any]] = None) -> None:  # noqa: D401
        """No-op."""
        return None


_NOOP_SPAN = _NoOpSpan()


@contextlib.contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    """Open a span, or yield a no-op when OTel is disabled.

    The context manager always yields **an object with**
    :meth:`set_attribute`, :meth:`set_attributes`, and :meth:`add_event`
    — call sites never need to check whether OTel is on.

    Args:
        name: The span name. Convention: dotted, lowercase, prefixed with
            ``ralph_afk.`` (matches the contract in the module docstring).
        **attrs: Optional initial attributes. ``None`` values are skipped
            so call sites can pass conditional values directly.

    Yields:
        The real :class:`Span` (when enabled) or a :class:`_NoOpSpan`
        (when disabled). Both support :meth:`set_attribute`.

    Example::

        with telemetry.span("ralph_afk.iteration", iter=1) as sp:
            ...
            sp.set_attribute("issue", first_ref)
    """
    _init()
    if not _state["enabled"]:
        yield _NOOP_SPAN
        return
    tracer = _state["tracer"]
    if tracer is None:
        # Belt-and-braces: enabled flag says True but tracer is None.
        # Shouldn't happen but defensive-program it.
        yield _NOOP_SPAN
        return
    with tracer.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                try:
                    sp.set_attribute(k, v)
                except Exception as exc:  # pragma: no cover - defensive
                    _logger.debug(
                        "set_attribute(%r, ...) failed: %s",
                        k, exc,
                    )
        yield sp


def build_sdk_telemetry_config() -> Optional[dict[str, Any]]:
    """Return a :class:`TelemetryConfig`-shaped dict for the SDK, or None.

    Returned dict is passed verbatim to :class:`SubprocessConfig.telemetry`
    on :class:`copilot.CopilotClient` construction. When OTel is disabled,
    returns ``None`` so the SDK skips its telemetry env-var setup
    entirely (the spawned subprocess does not see any
    ``COPILOT_OTEL_ENABLED`` / ``OTEL_*`` env vars from us).

    When OTel is enabled, returns a dict that may be empty:

    * If ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, the dict carries an
      ``"otlp_endpoint"`` key. The SDK uses this to configure the
      subprocess's exporter.
    * If only ``RALPH_OTEL_ENABLED=1`` is set (no endpoint), the dict
      is **empty** ``{}``. An empty dict is *not* falsy at the SDK seam
      (``SubprocessConfig.telemetry is not None`` is what triggers the
      SDK to set ``COPILOT_OTEL_ENABLED=true`` on the subprocess) — see
      :mod:`copilot.client`'s subprocess setup at the
      ``if telemetry is not None`` branch.

    This is the deliberate divergence from the disabled posture: an
    empty dict means "the SDK should emit telemetry; pick up the env
    vars from its parent process". A None means "skip telemetry
    entirely".
    """
    _init()
    if not _state["enabled"]:
        return None
    cfg: dict[str, Any] = {}
    endpoint = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    if endpoint:
        cfg["otlp_endpoint"] = endpoint
    return cfg


def force_flush(timeout_millis: int = 30_000) -> None:
    """Flush all configured span processors.

    No-op when OTel is disabled, when we never installed a provider
    ourselves, or when the configured provider is the OTel API's
    :class:`ProxyTracerProvider` placeholder. Called from the runner's
    outer ``finally`` block (in :func:`ralph_afk.loop.run`) so that
    spans buffered by :class:`BatchSpanProcessor` are exported before
    the Python process exits.

    Args:
        timeout_millis: Max wait time in milliseconds. Defaults to 30s
            — long enough for a network OTLP export to complete on a
            slow link but short enough to avoid hanging the runner.
    """
    _init()
    if not _state["enabled"]:
        return
    provider = _state.get("provider")
    if provider is None:
        return
    flush_fn = getattr(provider, "force_flush", None)
    if flush_fn is None:
        return
    try:
        flush_fn(timeout_millis)
    except Exception as exc:  # pragma: no cover - defensive
        _logger.warning(
            "TracerProvider.force_flush failed: %s: %s",
            type(exc).__name__,
            exc,
        )


def reset_for_tests() -> None:
    """Clear the cached enable decision + tracer.

    Intended for test fixtures that need to toggle ``RALPH_OTEL_ENABLED``
    or ``OTEL_EXPORTER_OTLP_ENDPOINT`` between test cases without
    inheriting a previous test's sticky decision. Production code MUST
    NOT call this.
    """
    _state["initialised"] = False
    _state["enabled"] = False
    _state["tracer"] = None
    _state["provider"] = None
