"""``ralph_afk.telemetry`` — opt-in OpenTelemetry tracing.

This subpackage contains the no-op-by-default OTel seam that the rest of
the runner uses for span emission. The single public module is
:mod:`ralph_afk.telemetry.otel`. See its docstring for the activation
contract and the call-site usage pattern.

The subpackage exists as a directory (rather than a top-level
``telemetry.py``) so issue #12's contract — that no ``opentelemetry-*``
package is imported when OTel is disabled — can be cleanly enforced by
the lazy imports inside :mod:`ralph_afk.telemetry.otel`. Operators who
install with ``uv sync`` (no ``--extra otel``) see exactly zero
``opentelemetry`` modules in ``sys.modules`` after a ``ralph-afk``
invocation that doesn't touch the wiring.
"""
