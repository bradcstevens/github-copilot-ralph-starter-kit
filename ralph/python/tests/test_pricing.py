"""Tests for ``ralph_afk.pricing`` (issue #4).

Covers the deep, pure pricing module:

* Resolution order in :func:`load_pricing` (explicit path beats env var beats
  packaged default).
* Parse-failure error messages name the offending file and field.
* Decimal-not-float precision (the canonical ``0.1 + 0.2 == 0.3`` trap).
* :func:`estimate_cost` and :func:`context_utilisation` return ``None`` for
  unknown models — not zero, not a guess.
* Packaged ``pricing.toml`` carries the date comment, the premium-request
  warning, and the ``RALPH_PRICING_FILE`` override note required by the PRD.
* The module imports nothing outside stdlib (enforced via AST).
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path

import pytest

from ralph_afk import pricing as pricing_module
from ralph_afk.pricing import (
    ModelPricing,
    Pricing,
    PricingError,
    context_utilisation,
    estimate_cost,
    load_pricing,
)


# ---------------------------------------------------------------------------
# load_pricing — resolution order and parsing
# ---------------------------------------------------------------------------


def test_load_pricing_packaged_default_contains_kit_default_model():
    """The packaged pricing.toml must include ``claude-opus-4.7-xhigh``."""
    p = load_pricing()
    assert "claude-opus-4.7-xhigh" in p.models


def test_load_pricing_packaged_default_has_at_least_three_models():
    """Packaged file: kit default plus at least two others (per PRD/issue)."""
    p = load_pricing()
    assert len(p.models) >= 3


def test_load_pricing_explicit_path(tmp_path: Path) -> None:
    custom = tmp_path / "custom.toml"
    custom.write_text(
        '[models."x"]\n'
        "input_per_mtok = 1.00\n"
        "output_per_mtok = 2.00\n"
        "context_window = 1000\n"
    )
    p = load_pricing(custom)
    assert list(p.models) == ["x"]
    assert p.models["x"].context_window == 1000
    assert p.models["x"].input_per_mtok == Decimal("1.00")
    assert p.models["x"].output_per_mtok == Decimal("2.00")


def test_load_pricing_env_var_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "env.toml"
    custom.write_text(
        '[models."from-env"]\n'
        "input_per_mtok = 99.99\n"
        "output_per_mtok = 0.01\n"
        "context_window = 12345\n"
    )
    monkeypatch.setenv("RALPH_PRICING_FILE", str(custom))
    p = load_pricing()
    assert list(p.models) == ["from-env"]
    assert p.models["from-env"].input_per_mtok == Decimal("99.99")


def test_load_pricing_explicit_path_beats_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "env.toml"
    env_file.write_text(
        '[models."from-env"]\n'
        "input_per_mtok = 1\noutput_per_mtok = 2\ncontext_window = 100\n"
    )
    explicit_file = tmp_path / "explicit.toml"
    explicit_file.write_text(
        '[models."explicit"]\n'
        "input_per_mtok = 3\noutput_per_mtok = 4\ncontext_window = 200\n"
    )
    monkeypatch.setenv("RALPH_PRICING_FILE", str(env_file))
    p = load_pricing(explicit_file)
    assert list(p.models) == ["explicit"]


def test_load_pricing_env_var_empty_string_falls_through_to_packaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset env var is conventional in tests; verify empty-string behaves the same."""
    monkeypatch.setenv("RALPH_PRICING_FILE", "")
    p = load_pricing()
    assert "claude-opus-4.7-xhigh" in p.models


def test_load_pricing_malformed_toml_raises_with_path(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = not = valid = toml")
    with pytest.raises(PricingError) as excinfo:
        load_pricing(bad)
    assert str(bad) in str(excinfo.value)


def test_load_pricing_missing_input_field_raises_with_field_name(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "missing.toml"
    bad.write_text(
        '[models."x"]\n'
        "output_per_mtok = 2.00\n"
        "context_window = 100\n"
    )
    with pytest.raises(PricingError) as excinfo:
        load_pricing(bad)
    msg = str(excinfo.value)
    assert "input_per_mtok" in msg
    assert str(bad) in msg


def test_load_pricing_missing_output_field_raises(tmp_path: Path) -> None:
    bad = tmp_path / "missing_out.toml"
    bad.write_text(
        '[models."x"]\n'
        "input_per_mtok = 1.00\n"
        "context_window = 100\n"
    )
    with pytest.raises(PricingError) as excinfo:
        load_pricing(bad)
    assert "output_per_mtok" in str(excinfo.value)


def test_load_pricing_missing_context_window_raises(tmp_path: Path) -> None:
    bad = tmp_path / "missing_ctx.toml"
    bad.write_text(
        '[models."x"]\n'
        "input_per_mtok = 1.00\n"
        "output_per_mtok = 2.00\n"
    )
    with pytest.raises(PricingError) as excinfo:
        load_pricing(bad)
    assert "context_window" in str(excinfo.value)


def test_load_pricing_missing_models_table_raises(tmp_path: Path) -> None:
    bad = tmp_path / "no_models.toml"
    bad.write_text("not_models = 'oops'\n")
    with pytest.raises(PricingError) as excinfo:
        load_pricing(bad)
    assert "models" in str(excinfo.value)


def test_load_pricing_nonexistent_path_raises(tmp_path: Path) -> None:
    with pytest.raises(PricingError):
        load_pricing(tmp_path / "does-not-exist.toml")


def test_load_pricing_model_entry_not_a_table_raises(tmp_path: Path) -> None:
    """``[models.x] = "scalar"`` is a parse-shape error; surface it clearly."""
    bad = tmp_path / "scalar_entry.toml"
    bad.write_text("[models]\nx = 'oops'\n")
    with pytest.raises(PricingError) as excinfo:
        load_pricing(bad)
    assert "x" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Decimal precision — float drift must NEVER leak into prices
# ---------------------------------------------------------------------------


def test_load_pricing_stores_decimal_not_float(tmp_path: Path) -> None:
    """The canonical float-precision trap: 0.1 + 0.2 must equal Decimal('0.3')."""
    custom = tmp_path / "p.toml"
    custom.write_text(
        '[models."m"]\n'
        "input_per_mtok = 0.1\n"
        "output_per_mtok = 0.2\n"
        "context_window = 1000\n"
    )
    p = load_pricing(custom)
    assert isinstance(p.models["m"].input_per_mtok, Decimal)
    assert isinstance(p.models["m"].output_per_mtok, Decimal)
    assert p.models["m"].input_per_mtok == Decimal("0.1")
    assert p.models["m"].output_per_mtok == Decimal("0.2")
    # Float would give 0.30000000000000004; Decimal gives 0.3 exactly.
    total = p.models["m"].input_per_mtok + p.models["m"].output_per_mtok
    assert total == Decimal("0.3")


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


def _pricing_with(models: dict[str, ModelPricing]) -> Pricing:
    return Pricing(models=models)


def test_estimate_cost_known_model_exact_decimal() -> None:
    pricing = _pricing_with(
        {
            "m": ModelPricing(
                input_per_mtok=Decimal("15.00"),
                output_per_mtok=Decimal("75.00"),
                context_window=200_000,
            )
        }
    )
    # 1M input @ $15/MTok = $15; 2M output @ $75/MTok = $150; total $165.
    cost = estimate_cost("m", 1_000_000, 2_000_000, pricing)
    assert cost == Decimal("165")
    assert isinstance(cost, Decimal)


def test_estimate_cost_partial_million_tokens() -> None:
    pricing = _pricing_with(
        {
            "m": ModelPricing(
                input_per_mtok=Decimal("3.00"),
                output_per_mtok=Decimal("9.00"),
                context_window=100_000,
            )
        }
    )
    # 100k @ $3/MTok = $0.30; 50k @ $9/MTok = $0.45; total $0.75.
    cost = estimate_cost("m", 100_000, 50_000, pricing)
    assert cost == Decimal("0.75")


def test_estimate_cost_zero_tokens_is_zero_decimal() -> None:
    pricing = _pricing_with(
        {"m": ModelPricing(Decimal("1.00"), Decimal("1.00"), 100)}
    )
    assert estimate_cost("m", 0, 0, pricing) == Decimal("0")


def test_estimate_cost_unknown_model_returns_none_not_zero() -> None:
    """Unknown model: must return ``None`` so the renderer can show ``—`` (em dash)."""
    pricing = _pricing_with(
        {"known": ModelPricing(Decimal("1"), Decimal("1"), 100)}
    )
    result = estimate_cost("unknown-model", 1_000, 1_000, pricing)
    assert result is None  # noqa: E711 — explicit identity, not falsiness
    assert result != Decimal("0")


# ---------------------------------------------------------------------------
# context_utilisation
# ---------------------------------------------------------------------------


def test_context_utilisation_known_model_returns_used_window_fraction() -> None:
    pricing = _pricing_with(
        {"m": ModelPricing(Decimal("0"), Decimal("0"), 200_000)}
    )
    result = context_utilisation("m", 100_000, pricing)
    assert result is not None
    used, window, fraction = result
    assert used == 100_000
    assert window == 200_000
    assert fraction == pytest.approx(0.5)


def test_context_utilisation_unknown_model_returns_none() -> None:
    pricing = _pricing_with({"known": ModelPricing(Decimal("0"), Decimal("0"), 100)})
    assert context_utilisation("unknown", 50, pricing) is None


def test_context_utilisation_zero_cumulative_tokens() -> None:
    pricing = _pricing_with({"m": ModelPricing(Decimal("0"), Decimal("0"), 100)})
    result = context_utilisation("m", 0, pricing)
    assert result is not None
    used, window, fraction = result
    assert used == 0
    assert window == 100
    assert fraction == 0.0


def test_context_utilisation_over_window_returns_fraction_greater_than_one() -> None:
    """Callers may threshold on ``fraction >= 0.5``; over-1.0 must round-trip."""
    pricing = _pricing_with({"m": ModelPricing(Decimal("0"), Decimal("0"), 100)})
    result = context_utilisation("m", 150, pricing)
    assert result is not None
    used, window, fraction = result
    assert used == 150
    assert window == 100
    assert fraction == pytest.approx(1.5)


def test_context_utilisation_at_smart_zone_threshold() -> None:
    """The PRD calls out ~100k tokens on a 200k window as the Smart Zone Ceiling."""
    pricing = _pricing_with({"m": ModelPricing(Decimal("0"), Decimal("0"), 200_000)})
    result = context_utilisation("m", 100_000, pricing)
    assert result is not None
    _, _, fraction = result
    assert fraction >= 0.5  # renderer-visible threshold from the PRD


# ---------------------------------------------------------------------------
# Packaged pricing.toml — preamble & schema invariants
# ---------------------------------------------------------------------------


def _packaged_pricing_toml_text() -> str:
    return (Path(pricing_module.__file__).parent / "pricing.toml").read_text()


def test_packaged_pricing_toml_has_iso_date_comment() -> None:
    text = _packaged_pricing_toml_text()
    assert re.search(r"^#.*\b\d{4}-\d{2}-\d{2}\b", text, re.MULTILINE), (
        "pricing.toml preamble must contain an ISO YYYY-MM-DD date comment"
    )


def test_packaged_pricing_toml_warns_about_premium_request_billing() -> None:
    """The PRD wording: 'PROVIDER LIST PRICES, not GitHub Copilot's premium-request billing.'"""
    text = _packaged_pricing_toml_text()
    assert "PROVIDER LIST PRICES" in text
    assert "premium-request" in text


def test_packaged_pricing_toml_documents_env_var_override() -> None:
    text = _packaged_pricing_toml_text()
    assert "RALPH_PRICING_FILE" in text


def test_packaged_pricing_kit_default_model_has_realistic_values() -> None:
    """Sanity-bound: an accidental units mistake (dollars-per-token, bytes-not-tokens)
    trips this without requiring a brittle exact-value match against drifting list prices.
    """
    p = load_pricing()
    m = p.models["claude-opus-4.7-xhigh"]
    assert Decimal("0.01") < m.input_per_mtok < Decimal("1000")
    assert Decimal("0.01") < m.output_per_mtok < Decimal("1000")
    # Universally true for current LLM provider list pricing.
    assert m.input_per_mtok < m.output_per_mtok
    # Window is in tokens, not bytes or characters.
    assert 1_000 <= m.context_window <= 10_000_000


# ---------------------------------------------------------------------------
# Module purity — stdlib-only imports (enforced structurally)
# ---------------------------------------------------------------------------


def test_pricing_module_imports_only_stdlib() -> None:
    """``pricing.py`` MUST NOT import third-party or peer-module code.

    Enforced via AST inspection with an explicit stdlib allowlist so any stray
    import — third-party, peer-module, or relative — fails loudly.
    """
    source = Path(pricing_module.__file__).read_text()
    tree = ast.parse(source)
    stdlib_allow = {
        "__future__",
        "os",
        "tomllib",
        "dataclasses",
        "decimal",
        "importlib",
        "importlib.resources",
        "pathlib",
        "typing",
    }
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                seen.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, (
                f"pricing.py contains a relative import (level={node.level})"
            )
            assert node.module is not None, "from-import with no module name"
            seen.add(node.module)
    leaked = seen - stdlib_allow
    assert not leaked, f"pricing.py imports non-stdlib modules: {leaked}"
