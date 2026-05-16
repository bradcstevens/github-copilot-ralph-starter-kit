"""Tests for :mod:`ralph_afk.config`.

* :class:`RunConfig` is a frozen dataclass with sensible defaults.
* ``__post_init__`` validation rejects malformed configs eagerly.
* :class:`RunConfig` structurally satisfies
  :class:`ralph_afk.session.SessionConfig` (the runtime-checkable
  Protocol used by :class:`~ralph_afk.session.IterationSession`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph_afk.config import RunConfig
from ralph_afk.session import SessionConfig


def test_run_config_defaults_are_safe() -> None:
    """A default :class:`RunConfig` constructs and exposes the expected fields."""
    cfg = RunConfig()
    assert cfg.model is None
    assert cfg.issue_source == "github"
    assert cfg.max_iterations == 0
    assert cfg.max_nmt_strikes == 3
    assert cfg.deny_tools == frozenset()
    assert cfg.deny_skills == frozenset()
    assert cfg.verbosity == 0
    assert cfg.render_reasoning is True
    assert cfg.otel_enabled is False
    assert cfg.pricing_file is None


def test_run_config_is_frozen() -> None:
    """Reassignment after construction is rejected (frozen dataclass)."""
    cfg = RunConfig()
    with pytest.raises(Exception):
        cfg.verbosity = 2  # type: ignore[misc]


def test_run_config_satisfies_session_config_protocol() -> None:
    """A :class:`RunConfig` is structurally a :class:`SessionConfig`.

    The Protocol is :func:`runtime_checkable`, so this is a real
    ``isinstance`` check, not just a type-checker promise. The loop
    slice depends on this — :class:`~ralph_afk.session.IterationSession`
    takes a ``config: SessionConfig`` parameter, and the loop passes a
    bare :class:`RunConfig` to it.
    """
    cfg = RunConfig(
        deny_tools=frozenset({"a"}),
        deny_skills=frozenset({"b"}),
        verbosity=2,
        render_reasoning=False,
    )
    assert isinstance(cfg, SessionConfig)


@pytest.mark.parametrize(
    "field,value",
    [
        ("issue_source", "gitlab"),
        ("max_iterations", -1),
        ("max_nmt_strikes", 0),
        ("verbosity", 4),
        ("verbosity", -1),
    ],
)
def test_run_config_validation_rejects_invalid_values(field: str, value: object) -> None:
    """``__post_init__`` validates the load-bearing knobs."""
    kwargs: dict[str, object] = {field: value}
    with pytest.raises(ValueError):
        RunConfig(**kwargs)  # type: ignore[arg-type]


def test_run_config_accepts_explicit_pricing_path() -> None:
    """Pricing-file overrides are preserved verbatim (no I/O at construction)."""
    p = Path("/nowhere/pricing.toml")
    cfg = RunConfig(pricing_file=p)
    assert cfg.pricing_file == p
