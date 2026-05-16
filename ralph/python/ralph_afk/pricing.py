"""``ralph_afk.pricing`` — provider-list-price cost estimation and context utilisation.

This module is **deep and pure**: stdlib-only, no I/O outside :func:`load_pricing`,
and prices stored as :class:`decimal.Decimal` so tests can assert exact equality
without float drift. The TOML schema and the packaged data live alongside this
file as ``pricing.toml``.

Cost figures produced here are **always estimates** based on provider list
prices. GitHub Copilot CLI bills on a premium-request quota that the SDK does
not expose — the renderer surfaces every cost figure with that caveat plus the
pricing-table date label.

Public surface:

* :func:`load_pricing` — resolve explicit path → ``RALPH_PRICING_FILE`` env var →
  packaged ``pricing.toml``.
* :func:`estimate_cost` — token counts → USD :class:`~decimal.Decimal`; ``None``
  for unknown model so callers render ``—`` rather than zero.
* :func:`context_utilisation` — cumulative tokens → ``(used, window, fraction)``;
  ``None`` for unknown model.
* :class:`Pricing`, :class:`ModelPricing` — frozen value objects.
* :exc:`PricingError` — raised by :func:`load_pricing` on parse failure.

Design notes:

* **No third-party imports.** The cross-runner spec (PRD #1) keeps this module
  on stdlib so it can be unit-tested in isolation and so the base install stays
  light. Enforced by ``tests/test_pricing.py::test_pricing_module_imports_only_stdlib``.
* **Decimal not float.** TOML scalars are parsed as Python ``float``/``int`` by
  ``tomllib``; we route every numeric through ``str()`` then :class:`Decimal` so
  the canonical ``0.1 + 0.2`` trap does not leak into per-iteration cost rollups.
* **Unknown-model semantics.** Both :func:`estimate_cost` and
  :func:`context_utilisation` return ``None`` (not zero, not a fallback guess)
  so the renderer can show ``—`` (em dash) for models that drift out of the
  pricing table without silently understating cost.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path
from typing import Mapping


class PricingError(ValueError):
    """Raised by :func:`load_pricing` when ``pricing.toml`` cannot be parsed.

    Subclasses :class:`ValueError` so callers that catch ``ValueError`` still
    work, but using a named class keeps the failure type visible in tracebacks
    and tests.
    """


@dataclass(frozen=True)
class ModelPricing:
    """Per-model pricing entry. Prices in USD per million tokens.

    Both prices are :class:`~decimal.Decimal` so arithmetic in
    :func:`estimate_cost` is exact. ``context_window`` is an integer (tokens).
    """

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    context_window: int


@dataclass(frozen=True)
class Pricing:
    """Resolved pricing table keyed by model name."""

    models: Mapping[str, ModelPricing]

    def get(self, model: str) -> ModelPricing | None:
        """Return the entry for ``model`` or ``None`` if absent."""
        return self.models.get(model)


_REQUIRED_FIELDS: tuple[str, ...] = (
    "input_per_mtok",
    "output_per_mtok",
    "context_window",
)
_ENV_OVERRIDE = "RALPH_PRICING_FILE"
_MTOK: Decimal = Decimal(1_000_000)


def _packaged_path() -> Path:
    """Resolve the packaged ``pricing.toml`` to a real filesystem path.

    ``importlib.resources.files()`` returns a :class:`~importlib.resources.abc.Traversable`;
    we convert to :class:`~pathlib.Path` so error messages and ``open()`` calls
    work uniformly with caller-supplied paths.
    """
    return Path(str(files("ralph_afk") / "pricing.toml"))


def load_pricing(path: Path | None = None) -> Pricing:
    """Load a pricing table.

    Resolution order:

    1. The explicit ``path`` argument, if provided.
    2. The ``RALPH_PRICING_FILE`` environment variable, if set and non-empty.
    3. The packaged ``ralph_afk/pricing.toml``.

    Raises :exc:`PricingError` with the offending path (and, where applicable,
    the offending model name or field) on any parse or schema failure.
    """
    if path is None:
        env_override = os.environ.get(_ENV_OVERRIDE) or ""
        path = Path(env_override) if env_override else _packaged_path()

    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise PricingError(f"Pricing file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise PricingError(
            f"Pricing file {path} is not valid TOML: {exc}"
        ) from exc

    models_raw = raw.get("models")
    if not isinstance(models_raw, dict):
        raise PricingError(
            f"Pricing file {path} is missing required top-level [models] table"
        )

    models: dict[str, ModelPricing] = {}
    for model_name, entry in models_raw.items():
        if not isinstance(entry, dict):
            raise PricingError(
                f"Pricing file {path}: [models.{model_name!r}] must be a table"
            )
        for field in _REQUIRED_FIELDS:
            if field not in entry:
                raise PricingError(
                    f"Pricing file {path}: [models.{model_name!r}] "
                    f"is missing required field {field!r}"
                )
        try:
            models[model_name] = ModelPricing(
                input_per_mtok=Decimal(str(entry["input_per_mtok"])),
                output_per_mtok=Decimal(str(entry["output_per_mtok"])),
                context_window=int(entry["context_window"]),
            )
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise PricingError(
                f"Pricing file {path}: [models.{model_name!r}] "
                f"has invalid numeric value: {exc}"
            ) from exc

    return Pricing(models=models)


def estimate_cost(
    model: str,
    tokens_in: int,
    tokens_out: int,
    pricing: Pricing,
) -> Decimal | None:
    """Estimate USD cost for a single iteration.

    Returns ``None`` (not zero) for unknown models so the renderer surfaces
    ``—`` rather than silently understating cost.

    Arithmetic stays in :class:`~decimal.Decimal` end-to-end.
    """
    entry = pricing.get(model)
    if entry is None:
        return None
    in_cost = (Decimal(tokens_in) * entry.input_per_mtok) / _MTOK
    out_cost = (Decimal(tokens_out) * entry.output_per_mtok) / _MTOK
    return in_cost + out_cost


def context_utilisation(
    model: str,
    cumulative_tokens: int,
    pricing: Pricing,
) -> tuple[int, int, float] | None:
    """Return ``(used, window, fraction)`` or ``None`` for unknown model.

    ``fraction`` is a plain :class:`float` because callers only consume it for
    thresholding (the renderer highlights at ``fraction >= 0.5`` — the PRD's
    "Smart Zone Ceiling" cue near ~100k tokens on a 200k window).

    A zero-window pricing entry (defensive, not expected) yields ``fraction=0.0``
    rather than a :exc:`ZeroDivisionError`.
    """
    entry = pricing.get(model)
    if entry is None:
        return None
    used = cumulative_tokens
    window = entry.context_window
    fraction = (used / window) if window > 0 else 0.0
    return (used, window, fraction)
