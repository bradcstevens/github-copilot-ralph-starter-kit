"""Tests for ``ralph_afk.usage`` (issue #39 — the ``UsageTally`` value object).

``UsageTally`` is the single code representation of **Consumption** (see
``CONTEXT.md``): the tokens-in / tokens-out and the model they were billed
against, plus the one shared rule every Cost figure derives from (first non-None
model wins; tokens sum). Today that rule is duplicated across the **Summary**'s
per-**Iteration** accrual (``RunSummary.record_usage``) and the **Queue**'s
per-**Active-issue** accrual (``LiveRunState._accrue_usage``); this module is the
home the two sinks converge on next.

Covered here:

* :meth:`UsageTally.add` — first-non-None-model-wins (a later ``None`` *and* a
  later different non-None model both leave an established model untouched) and
  token summation.
* :meth:`UsageTally.merge` — composes two tallies via the same rule.
* :attr:`UsageTally.total_tokens` — ``tokens_in + tokens_out``.
* :meth:`UsageTally.cost` — a :class:`~decimal.Decimal` for a known model,
  ``None`` for an unknown model, and ``None`` for a ``None`` model (the guard).
* The module imports only stdlib + ``ralph_afk.pricing`` (enforced via AST).
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

from ralph_afk import usage as usage_module
from ralph_afk.pricing import ModelPricing, Pricing, estimate_cost
from ralph_afk.usage import UsageTally


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _pricing() -> Pricing:
    """A tiny two-model pricing table with round prices for exact assertions."""
    return Pricing(
        models={
            "known-model": ModelPricing(
                input_per_mtok=Decimal("10"),
                output_per_mtok=Decimal("30"),
                context_window=200_000,
            ),
        }
    )


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_are_empty() -> None:
    tally = UsageTally()
    assert tally.model is None
    assert tally.tokens_in == 0
    assert tally.tokens_out == 0
    assert tally.total_tokens == 0


# ---------------------------------------------------------------------------
# add — first non-None model wins + token summation
# ---------------------------------------------------------------------------


def test_add_first_non_none_model_wins() -> None:
    """The first non-None model an ``add`` supplies becomes the tally's model."""
    tally = UsageTally()
    # A leading None sample must not establish a model.
    tally.add(None, 5, 7)
    assert tally.model is None
    # The first non-None model wins.
    tally.add("first-model", 1, 2)
    assert tally.model == "first-model"


def test_add_sums_tokens() -> None:
    """Every ``add`` accumulates tokens-in / tokens-out."""
    tally = UsageTally()
    tally.add("m", 10, 20)
    tally.add("m", 3, 4)
    assert tally.tokens_in == 13
    assert tally.tokens_out == 24
    assert tally.total_tokens == 37


def test_add_later_none_model_never_overwrites() -> None:
    """A later ``None`` model must not clear an established model (tokens still sum)."""
    tally = UsageTally()
    tally.add("established", 1, 1)
    tally.add(None, 4, 6)
    assert tally.model == "established"
    assert tally.tokens_in == 5
    assert tally.tokens_out == 7


def test_add_later_non_none_model_never_overwrites() -> None:
    """Once established, even a different non-None model does not overwrite it.

    Mirrors both existing sinks' ``if self.model is None and model is not None``
    guard, so an iteration's recorded model stays stable across samples.
    """
    tally = UsageTally()
    tally.add("first-model", 2, 2)
    tally.add("second-model", 3, 3)
    assert tally.model == "first-model"
    assert tally.tokens_in == 5
    assert tally.tokens_out == 5


# ---------------------------------------------------------------------------
# merge — composes two tallies via the same rule
# ---------------------------------------------------------------------------


def test_merge_takes_other_model_when_self_has_none() -> None:
    tally = UsageTally(model=None, tokens_in=5, tokens_out=3)
    other = UsageTally(model="other-model", tokens_in=2, tokens_out=4)
    tally.merge(other)
    assert tally.model == "other-model"
    assert tally.tokens_in == 7
    assert tally.tokens_out == 7


def test_merge_keeps_self_model_and_sums_tokens() -> None:
    """When both tallies name a model, self's model wins (first-non-None rule)."""
    tally = UsageTally(model="self-model", tokens_in=1, tokens_out=1)
    other = UsageTally(model="other-model", tokens_in=9, tokens_out=8)
    tally.merge(other)
    assert tally.model == "self-model"
    assert tally.tokens_in == 10
    assert tally.tokens_out == 9


def test_merge_does_not_mutate_other() -> None:
    tally = UsageTally()
    other = UsageTally(model="other-model", tokens_in=2, tokens_out=3)
    tally.merge(other)
    assert other.model == "other-model"
    assert other.tokens_in == 2
    assert other.tokens_out == 3


# ---------------------------------------------------------------------------
# total_tokens
# ---------------------------------------------------------------------------


def test_total_tokens_is_sum_of_in_and_out() -> None:
    tally = UsageTally(model="m", tokens_in=1200, tokens_out=800)
    assert tally.total_tokens == 2000


# ---------------------------------------------------------------------------
# cost — Decimal for a known model, None otherwise
# ---------------------------------------------------------------------------


def test_cost_known_model_returns_decimal() -> None:
    pricing = _pricing()
    tally = UsageTally(model="known-model", tokens_in=1000, tokens_out=2000)
    # 1000 * 10 / 1e6  +  2000 * 30 / 1e6  =  0.01 + 0.06
    assert tally.cost(pricing) == Decimal("0.07")


def test_cost_delegates_to_estimate_cost() -> None:
    """``cost`` is exactly ``estimate_cost`` for an established model."""
    pricing = _pricing()
    tally = UsageTally(model="known-model", tokens_in=1234, tokens_out=5678)
    assert tally.cost(pricing) == estimate_cost(
        "known-model", 1234, 5678, pricing
    )


def test_cost_unknown_model_returns_none() -> None:
    """An unknown model yields ``None`` (not zero) so callers render the em dash."""
    pricing = _pricing()
    tally = UsageTally(model="not-in-table", tokens_in=1000, tokens_out=2000)
    assert tally.cost(pricing) is None


def test_cost_none_model_returns_none() -> None:
    """A ``None`` model short-circuits before ``estimate_cost`` — no crash, no cost."""
    pricing = _pricing()
    tally = UsageTally(model=None, tokens_in=1000, tokens_out=2000)
    assert tally.cost(pricing) is None


# ---------------------------------------------------------------------------
# Module purity — stdlib + ralph_afk.pricing only (enforced structurally)
# ---------------------------------------------------------------------------


def test_usage_module_imports_only_stdlib_and_pricing() -> None:
    """``usage.py`` MUST import only stdlib + ``ralph_afk.pricing``.

    Preserves the repo's import-guard posture (ADR-0001): the Consumption value
    object stays a pure leaf — no Textual, no SDK, no other first-party module,
    and it reaches pricing through ``ralph_afk.pricing`` (itself stdlib-only),
    never a heavier peer.
    """
    source = Path(usage_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allow = {
        "__future__",
        "dataclasses",
        "decimal",
        "ralph_afk.pricing",
    }
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                seen.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "usage.py must use absolute imports only"
            assert node.module is not None, "from-import with no module name"
            seen.add(node.module)
    leaked = seen - allow
    assert not leaked, f"usage.py imports non-allowlisted modules: {leaked}"
    assert "textual" not in seen, "UsageTally must not import Textual"
