"""Tests for ``ralph_afk.interactive.models`` (issue #24 — picker row model).

The picker's row content, ordering, cursor default, effort logic, and cell
formatting live in a **deep + pure** module (stdlib + :mod:`ralph_afk.config`
only — no Textual, no SDK) so they are unit-testable without a TTY and without a
live ``list_models()`` call. These tests pin:

* the projection from duck-typed SDK ``ModelInfo`` objects to
  :class:`~ralph_afk.interactive.models.ModelChoice` rows;
* policy-disabled models becoming non-selectable;
* the per-model supported-effort filter (intersected with the kit's
  sendable :data:`~ralph_afk.config.REASONING_EFFORTS`) and the auto-skip
  signal (empty -> stage 2 is skipped);
* the pre-highlight cursor default (env ``MODEL`` / kit default);
* the cell formatters; and
* the module's import-guard (no Textual / SDK).
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from ralph_afk.config import REASONING_EFFORTS
from ralph_afk.interactive import models as models_module
from ralph_afk.interactive.models import (
    ModelChoice,
    default_cursor_index,
    format_context_window,
    format_multiplier,
    format_reasoning,
    to_model_choices,
)


# ---------------------------------------------------------------------------
# Fake duck-typed SDK ModelInfo builder (mirrors copilot.ModelInfo shape)
# ---------------------------------------------------------------------------


def _model(
    id: str,
    *,
    name: str | None = None,
    multiplier: float | None = 1.0,
    context: int | None = 200_000,
    efforts: list[str] | None = None,
    default_effort: str | None = None,
    policy_state: str | None = "enabled",
    with_billing: bool = True,
    with_policy: bool = True,
) -> SimpleNamespace:
    """Build an object shaped like the SDK's ``ModelInfo`` (duck-typed)."""
    supports = SimpleNamespace(vision=False, reasoning_effort=bool(efforts))
    limits = SimpleNamespace(
        max_context_window_tokens=context, max_prompt_tokens=None, vision=None
    )
    capabilities = SimpleNamespace(supports=supports, limits=limits)
    billing = (
        SimpleNamespace(multiplier=multiplier, token_prices=None)
        if with_billing
        else None
    )
    policy = (
        SimpleNamespace(state=policy_state, terms="") if with_policy else None
    )
    return SimpleNamespace(
        id=id,
        name=name if name is not None else id.upper(),
        capabilities=capabilities,
        policy=policy,
        billing=billing,
        supported_reasoning_efforts=efforts,
        default_reasoning_effort=default_effort,
    )


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def test_projection_maps_identity_and_billing_and_context() -> None:
    choices = to_model_choices(
        [_model("gpt-5.4", name="GPT 5.4", multiplier=0.33, context=128_000)]
    )
    assert len(choices) == 1
    ch = choices[0]
    assert isinstance(ch, ModelChoice)
    assert ch.id == "gpt-5.4"
    assert ch.name == "GPT 5.4"
    assert ch.multiplier == 0.33
    assert ch.context_window == 128_000


def test_projection_missing_billing_and_policy_is_selectable_no_multiplier() -> None:
    choices = to_model_choices(
        [_model("m", with_billing=False, with_policy=False)]
    )
    ch = choices[0]
    assert ch.multiplier is None
    # No policy block at all -> not disabled -> selectable.
    assert ch.selectable is True
    assert ch.policy_state is None


def test_disabled_policy_is_not_selectable() -> None:
    choices = to_model_choices([_model("m", policy_state="disabled")])
    assert choices[0].selectable is False
    assert choices[0].policy_state == "disabled"


def test_unconfigured_policy_is_selectable() -> None:
    choices = to_model_choices([_model("m", policy_state="unconfigured")])
    assert choices[0].selectable is True


# ---------------------------------------------------------------------------
# Effort filtering + auto-skip signal
# ---------------------------------------------------------------------------


def test_supported_efforts_filtered_to_sendable_set_preserving_order() -> None:
    # "minimal" is not in the kit's sendable REASONING_EFFORTS -> dropped.
    ch = to_model_choices(
        [_model("m", efforts=["minimal", "low", "high", "max"], default_effort="high")]
    )[0]
    assert ch.supported_efforts == ("low", "high", "max")
    assert all(e in REASONING_EFFORTS for e in ch.supported_efforts)
    assert ch.supports_reasoning is True


def test_no_efforts_means_empty_and_unsupported() -> None:
    ch = to_model_choices([_model("m", efforts=None)])[0]
    assert ch.supported_efforts == ()
    assert ch.supports_reasoning is False
    assert ch.default_effort is None


def test_all_efforts_out_of_set_collapses_to_empty() -> None:
    ch = to_model_choices([_model("m", efforts=["minimal", "none"])])[0]
    assert ch.supported_efforts == ()
    assert ch.supports_reasoning is False


def test_default_effort_kept_when_in_set_else_first() -> None:
    in_set = to_model_choices(
        [_model("a", efforts=["low", "high"], default_effort="high")]
    )[0]
    assert in_set.default_effort == "high"
    # default outside the filtered set falls back to the first supported.
    fallback = to_model_choices(
        [_model("b", efforts=["low", "high"], default_effort="minimal")]
    )[0]
    assert fallback.default_effort == "low"


# ---------------------------------------------------------------------------
# Cursor default (pre-highlight)
# ---------------------------------------------------------------------------


def test_cursor_default_prefers_matching_id() -> None:
    choices = to_model_choices(
        [_model("a"), _model("claude-opus-4.8"), _model("c")]
    )
    assert default_cursor_index(choices, preferred="claude-opus-4.8") == 1


def test_cursor_default_unknown_preferred_falls_to_first_selectable() -> None:
    choices = to_model_choices(
        [_model("a", policy_state="disabled"), _model("b"), _model("c")]
    )
    assert default_cursor_index(choices, preferred="nope") == 1


def test_cursor_default_none_preferred_falls_to_first_selectable() -> None:
    choices = to_model_choices([_model("a", policy_state="disabled"), _model("b")])
    assert default_cursor_index(choices, preferred=None) == 1


def test_cursor_default_all_disabled_returns_zero() -> None:
    choices = to_model_choices(
        [_model("a", policy_state="disabled"), _model("b", policy_state="disabled")]
    )
    assert default_cursor_index(choices, preferred=None) == 0


def test_cursor_default_preferred_even_if_disabled() -> None:
    # The env/default is pre-highlighted even when policy-disabled (the picker
    # still blocks *selecting* it).
    choices = to_model_choices(
        [_model("a"), _model("dis", policy_state="disabled")]
    )
    assert default_cursor_index(choices, preferred="dis") == 1


def test_cursor_default_empty_choices_returns_zero() -> None:
    assert default_cursor_index([], preferred="x") == 0


# ---------------------------------------------------------------------------
# Cell formatters
# ---------------------------------------------------------------------------


def test_format_multiplier() -> None:
    assert format_multiplier(None) == "—"
    assert format_multiplier(1.0) == "1×"
    assert format_multiplier(0.33) == "0.33×"
    assert format_multiplier(10.0) == "10×"


def test_format_context_window() -> None:
    assert format_context_window(None) == "—"
    assert format_context_window(200_000) == "200K"
    assert format_context_window(1_000_000) == "1M"
    assert format_context_window(128_000) == "128K"
    assert format_context_window(500) == "500"


def test_format_reasoning() -> None:
    no = to_model_choices([_model("m", efforts=None)])[0]
    assert format_reasoning(no) == "no"
    yes = to_model_choices(
        [_model("m", efforts=["low", "high"], default_effort="high")]
    )[0]
    assert format_reasoning(yes) == "yes (default: high)"


# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------


def test_models_module_imports_are_constrained() -> None:
    """``models.py`` is deep + pure: stdlib + ``ralph_afk.config`` only.

    The picker's row model must stay unit-testable without a TTY and must never
    import Textual or the SDK (ADR-0001 import-guard convention).
    """
    source = Path(models_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allow = {"__future__", "dataclasses", "typing", "ralph_afk.config"}
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                seen.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "models.py must use absolute imports only"
            assert node.module is not None
            seen.add(node.module)
    leaked = seen - allow
    assert not leaked, f"models.py imports non-allowlisted modules: {leaked}"
    assert "textual" not in seen, "models.py must not import Textual"
    assert "copilot" not in seen, "models.py must not import the SDK"
