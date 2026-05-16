"""Unit tests for :mod:`ralph_afk.telemetry.otel`.

Covers the seam in isolation — the integration with
:mod:`ralph_afk.loop` is exercised by
``tests/test_iteration_end_to_end.py::test_loop_emits_otel_span_tree_when_enabled``.

These tests use the in-memory :class:`InMemorySpanExporter` to capture
emitted spans without paying for a real OTLP exporter. They are
parametrised across the four activation states:

* both env vars unset → disabled
* ``RALPH_OTEL_ENABLED=1`` alone → enabled, empty SDK config
* ``OTEL_EXPORTER_OTLP_ENDPOINT=http://...`` alone → enabled, endpoint in SDK config
* both set → enabled

Each test resets the module-level cache via :func:`reset_for_tests` so
sticky-enable state doesn't leak between cases.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any, Iterator

import pytest

from ralph_afk.telemetry import otel


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_otel(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ensure each test starts with a clean telemetry cache.

    Tests that set env vars must also call :func:`reset_for_tests` so the
    sticky-enable decision is recomputed. This fixture handles both
    halves: clears the cache before the test, clears again after.
    """
    otel.reset_for_tests()
    # Default: ensure we start with both env vars cleared. Individual
    # tests opt back in via monkeypatch.setenv.
    monkeypatch.delenv("RALPH_OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    yield
    otel.reset_for_tests()


@pytest.fixture
def in_memory_exporter(fresh_otel: None, monkeypatch: pytest.MonkeyPatch):
    """Install an :class:`InMemorySpanExporter` as the global provider.

    Requires the ``[otel]`` extra to be installed. Skips the test
    otherwise so the test suite remains green on the base install.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
        from opentelemetry.util._once import Once
    except ImportError:
        pytest.skip("opentelemetry not installed (run with --extra otel)")

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # `trace.set_tracer_provider` is "set-once" in OTel. We bypass the
    # idempotency guard by clobbering the internal globals — this is
    # the pattern OTel's own test suite uses (and is documented as
    # acceptable for test fixtures).
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider, raising=False)
    monkeypatch.setattr(
        trace, "_TRACER_PROVIDER_SET_ONCE", Once(), raising=False
    )

    yield exporter


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------


def test_is_enabled_returns_false_when_both_env_vars_unset(
    fresh_otel: None,
) -> None:
    """Disabled posture: no env vars → no tracing."""
    assert otel.is_enabled() is False


def test_is_enabled_returns_true_when_ralph_otel_enabled_is_truthy(
    fresh_otel: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RALPH_OTEL_ENABLED=1 → enabled, no endpoint required."""
    monkeypatch.setenv("RALPH_OTEL_ENABLED", "1")
    otel.reset_for_tests()
    assert otel.is_enabled() is True


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on", "  1  "])
def test_is_enabled_accepts_documented_truthy_values(
    fresh_otel: None,
    monkeypatch: pytest.MonkeyPatch,
    truthy: str,
) -> None:
    """The documented truthy spellings all enable OTel."""
    monkeypatch.setenv("RALPH_OTEL_ENABLED", truthy)
    otel.reset_for_tests()
    assert otel.is_enabled() is True


@pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off", "  "])
def test_is_enabled_rejects_falsy_values(
    fresh_otel: None,
    monkeypatch: pytest.MonkeyPatch,
    falsy: str,
) -> None:
    """Falsy / empty RALPH_OTEL_ENABLED is treated as disabled."""
    monkeypatch.setenv("RALPH_OTEL_ENABLED", falsy)
    otel.reset_for_tests()
    assert otel.is_enabled() is False


def test_is_enabled_returns_true_when_otlp_endpoint_set(
    fresh_otel: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OTEL_EXPORTER_OTLP_ENDPOINT=<non-empty> → enabled."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    otel.reset_for_tests()
    assert otel.is_enabled() is True


def test_is_enabled_ignores_empty_otlp_endpoint(
    fresh_otel: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty / whitespace-only endpoint env var is not "set"."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "   ")
    otel.reset_for_tests()
    assert otel.is_enabled() is False


def test_is_enabled_is_sticky_across_env_changes(
    fresh_otel: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once decided, mid-process env-var mutation does not flip the cache.

    Without ``reset_for_tests``, the first call's decision wins. This
    protects long-running iterations from a half-emitted span tree if
    something exotic mutates the environment.
    """
    # First call: enabled.
    monkeypatch.setenv("RALPH_OTEL_ENABLED", "1")
    otel.reset_for_tests()
    assert otel.is_enabled() is True

    # Mutate env mid-process: cache should NOT flip.
    monkeypatch.delenv("RALPH_OTEL_ENABLED", raising=False)
    assert otel.is_enabled() is True, (
        "Sticky-enable broken: cache should survive env-var unset"
    )


# ---------------------------------------------------------------------------
# span() — disabled posture
# ---------------------------------------------------------------------------


def test_span_yields_noop_when_disabled(fresh_otel: None) -> None:
    """Disabled posture: ``span()`` yields an object with the same surface."""
    with otel.span("ralph_afk.test", iter=1, issue=42) as sp:
        # Both .set_attribute and .set_attributes must be no-ops.
        sp.set_attribute("k", "v")
        sp.set_attributes({"a": 1, "b": "two"})
        sp.add_event("an_event", {"attr": 1})

    # Same shape on a second call — no state leakage.
    with otel.span("ralph_afk.other") as sp2:
        sp2.set_attribute("k", "v")


def test_span_does_not_import_opentelemetry_when_disabled(
    fresh_otel: None,
) -> None:
    """Critical: disabled posture must not import the OTel package.

    If `opentelemetry` shows up in sys.modules after a span() call with
    disabled OTel, the base install (`uv sync` without `--extra otel`)
    would import-error at first use. This test catches that regression
    in-process; the full subprocess assertion lives in test_smoke.py.
    """
    # Capture initial state — opentelemetry may have been imported by
    # a *previous* test that ran with the in_memory_exporter fixture
    # (which always imports opentelemetry). In that case the import
    # has already happened and we can't rewind it. So this test only
    # has signal if opentelemetry isn't already in sys.modules.
    if "opentelemetry" in sys.modules:
        pytest.skip(
            "opentelemetry already imported by a prior test fixture "
            "— cannot verify in-process import absence; the subprocess "
            "smoke test covers this contract end-to-end"
        )

    with otel.span("ralph_afk.test", iter=1):
        pass

    assert "opentelemetry" not in sys.modules, (
        "span() in disabled posture must not import opentelemetry"
    )


# ---------------------------------------------------------------------------
# span() — enabled posture (with in-memory exporter)
# ---------------------------------------------------------------------------


def test_span_emits_real_span_when_enabled(
    in_memory_exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled posture: ``span()`` emits a real recorded span."""
    monkeypatch.setenv("RALPH_OTEL_ENABLED", "1")
    otel.reset_for_tests()

    with otel.span("ralph_afk.test", iter=3) as sp:
        sp.set_attribute("issue", 42)

    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    emitted = spans[0]
    assert emitted.name == "ralph_afk.test"
    # Initial attribute from kwargs and late-bound attribute via
    # set_attribute() both appear on the same span.
    assert emitted.attributes.get("iter") == 3
    assert emitted.attributes.get("issue") == 42


def test_span_skips_none_attributes(
    in_memory_exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``span(..., x=None)`` does not set ``x`` on the span.

    Call sites pass conditional values directly (e.g. ``issue=first_ref``
    where ``first_ref`` may be None on the empty-pool path). Skipping
    None preserves the "attribute is absent" semantic instead of
    rendering it as a string ``"None"`` in trace viewers.
    """
    monkeypatch.setenv("RALPH_OTEL_ENABLED", "1")
    otel.reset_for_tests()

    with otel.span("ralph_afk.test", iter=1, missing=None):
        pass

    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    assert "iter" in spans[0].attributes
    assert "missing" not in spans[0].attributes


def test_span_nesting_produces_parent_child_relationship(
    in_memory_exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested ``span()`` calls produce a child span linked to the parent."""
    monkeypatch.setenv("RALPH_OTEL_ENABLED", "1")
    otel.reset_for_tests()

    with otel.span("ralph_afk.outer"):
        with otel.span("ralph_afk.inner"):
            pass

    spans = in_memory_exporter.get_finished_spans()
    # SimpleSpanProcessor finishes in LIFO; sort by name for stable lookup.
    by_name = {s.name: s for s in spans}
    assert "ralph_afk.outer" in by_name
    assert "ralph_afk.inner" in by_name
    outer = by_name["ralph_afk.outer"]
    inner = by_name["ralph_afk.inner"]
    assert inner.parent is not None, "inner span must have a parent"
    assert inner.parent.span_id == outer.context.span_id, (
        "inner.parent should point at outer span"
    )


def test_span_async_friendly_propagation(
    in_memory_exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spans propagate through asyncio context via contextvars.

    The loop wraps an ``async with IterationSession(...)`` block with a
    sync ``with telemetry.span("ralph_afk.session"):``. This works
    because OTel uses :mod:`contextvars` under the hood, which IS
    propagated through ``asyncio.run`` / ``await`` boundaries. This
    test pins that property so a future OTel SDK change to non-ContextVar
    context doesn't silently break the runner.
    """
    import asyncio

    monkeypatch.setenv("RALPH_OTEL_ENABLED", "1")
    otel.reset_for_tests()

    async def inner_work() -> None:
        with otel.span("ralph_afk.async_inner"):
            await asyncio.sleep(0)

    async def main() -> None:
        with otel.span("ralph_afk.async_outer"):
            await inner_work()

    asyncio.run(main())

    spans = in_memory_exporter.get_finished_spans()
    by_name = {s.name: s for s in spans}
    inner = by_name["ralph_afk.async_inner"]
    outer = by_name["ralph_afk.async_outer"]
    assert inner.parent is not None
    assert inner.parent.span_id == outer.context.span_id


# ---------------------------------------------------------------------------
# build_sdk_telemetry_config
# ---------------------------------------------------------------------------


def test_build_sdk_telemetry_config_returns_none_when_disabled(
    fresh_otel: None,
) -> None:
    """Disabled posture: no SubprocessConfig.telemetry → SDK skips telemetry."""
    assert otel.build_sdk_telemetry_config() is None


def test_build_sdk_telemetry_config_returns_empty_dict_when_enabled_without_endpoint(
    fresh_otel: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RALPH_OTEL_ENABLED=1 alone → SDK gets ``{}`` (enables telemetry env vars).

    Per the SDK contract in ``copilot/client.py``:
    ``if telemetry is not None: env["COPILOT_OTEL_ENABLED"] = "true"``.
    So an empty dict is the canonical "enable telemetry, no overrides"
    signal — the spawned subprocess will inherit ambient OTEL_* env
    vars from its parent.
    """
    monkeypatch.setenv("RALPH_OTEL_ENABLED", "1")
    otel.reset_for_tests()
    cfg = otel.build_sdk_telemetry_config()
    assert cfg == {}, (
        f"expected empty dict to enable SDK telemetry; got {cfg!r}"
    )


def test_build_sdk_telemetry_config_returns_endpoint_when_set(
    fresh_otel: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endpoint set → it gets forwarded to the SDK config under ``otlp_endpoint``."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    otel.reset_for_tests()
    cfg = otel.build_sdk_telemetry_config()
    assert cfg == {"otlp_endpoint": "http://collector:4318"}


# ---------------------------------------------------------------------------
# force_flush
# ---------------------------------------------------------------------------


def test_force_flush_is_safe_no_op_when_disabled(fresh_otel: None) -> None:
    """force_flush() in disabled posture must not raise and must not import opentelemetry."""
    # If opentelemetry wasn't already imported, ensure force_flush doesn't import it.
    pre_imported = "opentelemetry" in sys.modules

    otel.force_flush()  # must not raise

    if not pre_imported:
        assert "opentelemetry" not in sys.modules, (
            "force_flush() in disabled posture must not import opentelemetry"
        )


def test_force_flush_calls_provider_force_flush_when_enabled(
    in_memory_exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled posture: force_flush() drains buffered spans before return."""
    monkeypatch.setenv("RALPH_OTEL_ENABLED", "1")
    otel.reset_for_tests()

    with otel.span("ralph_afk.flush_test"):
        pass

    # SimpleSpanProcessor exports synchronously, so the span is already
    # present. But force_flush must still succeed without raising.
    otel.force_flush(timeout_millis=1_000)

    spans = in_memory_exporter.get_finished_spans()
    assert any(s.name == "ralph_afk.flush_test" for s in spans)


# ---------------------------------------------------------------------------
# Graceful degradation when extras are missing
# ---------------------------------------------------------------------------


def test_init_degrades_gracefully_when_opentelemetry_unavailable(
    fresh_otel: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Operator sets RALPH_OTEL_ENABLED=1 but [otel] extra is not installed.

    Expected: a one-time WARNING is logged, ``is_enabled()`` returns False,
    ``span()`` yields a no-op. The runner does not crash.
    """
    monkeypatch.setenv("RALPH_OTEL_ENABLED", "1")
    otel.reset_for_tests()

    # Simulate "opentelemetry not installed" by injecting a sys.modules
    # entry that raises ImportError when touched.
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    # Even though env says "enable", the missing import degrades to no-op.
    with caplog.at_level("WARNING", logger="ralph_afk.telemetry"):
        assert otel.is_enabled() is False
        # span() still works (yields no-op).
        with otel.span("ralph_afk.test", iter=1) as sp:
            sp.set_attribute("k", "v")

    # The graceful-degradation warning must be emitted.
    warning_msgs = [
        rec.message for rec in caplog.records
        if rec.levelname == "WARNING"
    ]
    assert any(
        "OTel was requested" in m and "[otel] extra is not installed" in m
        for m in warning_msgs
    ), f"expected the [otel]-install warning; got: {warning_msgs!r}"


# ---------------------------------------------------------------------------
# reset_for_tests
# ---------------------------------------------------------------------------


def test_reset_for_tests_clears_cached_state(
    fresh_otel: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reset_for_tests() makes is_enabled() recompute on next call."""
    # First: disabled.
    assert otel.is_enabled() is False

    # Flip env, reset, re-compute → enabled.
    monkeypatch.setenv("RALPH_OTEL_ENABLED", "1")
    otel.reset_for_tests()
    assert otel.is_enabled() is True


# ---------------------------------------------------------------------------
# Module shape guards
# ---------------------------------------------------------------------------


def test_module_has_expected_public_surface() -> None:
    """``__all__`` carries exactly the documented public symbols."""
    assert set(otel.__all__) == {
        "is_enabled",
        "span",
        "build_sdk_telemetry_config",
        "force_flush",
        "reset_for_tests",
    }


def test_module_does_not_import_opentelemetry_at_module_load() -> None:
    """Even loading the module must not trigger opentelemetry import.

    This is the contract that lets the runner stay usable on the base
    install (no ``[otel]`` extra). The module exposes :func:`span` and
    friends as importable callables that lazy-import :mod:`opentelemetry`
    only when actually called with OTel enabled.
    """
    # Reload the telemetry module in a clean state to verify the assertion.
    if "opentelemetry" in sys.modules:
        pytest.skip(
            "opentelemetry already in sys.modules (likely from an earlier "
            "test using --extra otel); can't verify the module-load "
            "contract here — covered by the subprocess smoke test"
        )

    # Force-reimport our module — the import itself must not pull OTel in.
    importlib.reload(otel)
    assert "opentelemetry" not in sys.modules, (
        "importing ralph_afk.telemetry.otel must not import opentelemetry"
    )
