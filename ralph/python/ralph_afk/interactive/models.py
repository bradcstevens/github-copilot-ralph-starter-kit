"""``ralph_afk.interactive.models`` — the pure picker row model (issue #24).

The startup **model + reasoning-effort picker** (decisions D2a-D2d) projects a
live ``list_models()`` result into the rows the Textual picker renders. That
projection — the columns, the policy-disabled gate, the per-model effort filter,
the pre-highlight cursor default, and the cell formatting — lives here as a
**deep + pure** module: stdlib + :mod:`ralph_afk.config` only, **no Textual** and
**no SDK**. So the whole content/ordering of the picker is unit-testable without
a TTY and without a live backend, honouring the repo's import-guard convention
(ADR-0001; mirrors :mod:`ralph_afk.interactive.state`). The Textual presentation
lives in :mod:`ralph_afk.interactive.picker_app`; the live fetch + fallback
orchestration in :mod:`ralph_afk.interactive.picker`.

The SDK's ``copilot.ModelInfo`` objects are consumed by **duck typing**
(attribute access only) — importing ``copilot`` would pull the SDK into this
pure layer, so instead the fetch layer hands these objects in at runtime and
this module only reads ``.id`` / ``.name`` / ``.policy`` / ``.billing`` /
``.capabilities`` / ``.supported_reasoning_efforts`` /
``.default_reasoning_effort``. This mirrors how :mod:`ralph_afk.interactive.state`
folds wrapper events from plain dicts rather than importing the SDK's event
package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ralph_afk.config import REASONING_EFFORTS

__all__ = [
    "ModelChoice",
    "Selection",
    "POLICY_DISABLED",
    "to_model_choices",
    "default_cursor_index",
    "format_multiplier",
    "format_context_window",
    "format_reasoning",
]

#: The :class:`copilot.ModelPolicy` ``state`` value that means a model is barred
#: by org/account policy. Such models are shown greyed-out and **non-selectable**
#: in the picker (the other states — ``"enabled"`` / ``"unconfigured"`` — are
#: selectable).
POLICY_DISABLED = "disabled"


@dataclass(frozen=True)
class Selection:
    """The picker's outcome: the chosen model id + (optional) reasoning effort.

    ``effort`` is ``None`` when the chosen model supports no reasoning effort
    (stage 2 is auto-skipped) — the run then sends no effort, mirroring the
    config's capability gate.
    """

    model: str
    effort: str | None


@dataclass(frozen=True)
class ModelChoice:
    """One selectable (or greyed-out) row in the picker's stage-1 model list.

    Attributes:
        id: Model id sent to the run (e.g. ``"claude-opus-4.8"``).
        name: Human-readable display name.
        multiplier: Premium (billing) multiplier, or ``None`` when the backend
            reports no billing block.
        context_window: Max context-window tokens, or ``None`` when unreported.
        supports_reasoning: Whether the model offers any *sendable* reasoning
            effort (i.e. :attr:`supported_efforts` is non-empty). When ``False``
            the picker auto-skips stage 2.
        default_effort: The model's default reasoning effort (always one of
            :attr:`supported_efforts` when reasoning is supported), or ``None``.
        supported_efforts: The model's live supported reasoning efforts,
            **filtered** to the kit's sendable :data:`~ralph_afk.config.REASONING_EFFORTS`
            and order-preserved, so anything the picker offers can be baked into
            the frozen ``RunConfig`` without tripping its capability gate.
        selectable: ``False`` when policy-disabled (greyed-out, Enter is a
            no-op); ``True`` otherwise.
        policy_state: The raw policy state (``"enabled"`` / ``"disabled"`` /
            ``"unconfigured"``), or ``None`` when the backend reports no policy.
    """

    id: str
    name: str
    multiplier: float | None
    context_window: int | None
    supports_reasoning: bool
    default_effort: str | None
    supported_efforts: tuple[str, ...]
    selectable: bool
    policy_state: str | None


def _supported_efforts(model: Any) -> tuple[str, ...]:
    """Live supported efforts ∩ the kit's sendable set, order preserved."""
    raw = getattr(model, "supported_reasoning_efforts", None) or ()
    return tuple(effort for effort in raw if effort in REASONING_EFFORTS)


def _default_effort(model: Any, supported: tuple[str, ...]) -> str | None:
    """The model's default effort if still sendable, else the first supported."""
    if not supported:
        return None
    live_default = getattr(model, "default_reasoning_effort", None)
    if live_default in supported:
        return live_default
    return supported[0]


def to_model_choices(models: Sequence[Any]) -> list[ModelChoice]:
    """Project duck-typed SDK ``ModelInfo`` objects into picker rows.

    Reads only attributes (no SDK import), defensively tolerating absent
    ``billing`` / ``policy`` blocks. Order is preserved from ``models`` (the
    backend's own ordering), which the picker then renders top-to-bottom.
    """
    choices: list[ModelChoice] = []
    for model in models:
        billing = getattr(model, "billing", None)
        multiplier = getattr(billing, "multiplier", None) if billing else None

        capabilities = getattr(model, "capabilities", None)
        limits = getattr(capabilities, "limits", None)
        context_window = getattr(limits, "max_context_window_tokens", None)

        policy = getattr(model, "policy", None)
        policy_state = getattr(policy, "state", None) if policy else None

        supported = _supported_efforts(model)
        choices.append(
            ModelChoice(
                id=str(getattr(model, "id")),
                name=str(getattr(model, "name")),
                multiplier=multiplier,
                context_window=context_window,
                supports_reasoning=bool(supported),
                default_effort=_default_effort(model, supported),
                supported_efforts=supported,
                selectable=policy_state != POLICY_DISABLED,
                policy_state=policy_state,
            )
        )
    return choices


def default_cursor_index(
    choices: Sequence[ModelChoice], *, preferred: str | None
) -> int:
    """The row to pre-highlight: the env ``MODEL`` / kit default, else first usable.

    The ``preferred`` id (the env-or-default model already resolved into the
    ``RunConfig``) is pre-highlighted when present — even if it is policy-disabled
    (the picker still blocks *selecting* it), so the operator sees their
    configured model. Otherwise the cursor lands on the first **selectable** row,
    falling back to ``0`` when nothing is selectable (or the list is empty).
    """
    for index, choice in enumerate(choices):
        if choice.id == preferred:
            return index
    for index, choice in enumerate(choices):
        if choice.selectable:
            return index
    return 0


def format_multiplier(multiplier: float | None) -> str:
    """Render the premium (billing) multiplier cell (``None`` -> ``"—"``)."""
    if multiplier is None:
        return "—"
    return f"{multiplier:g}×"


def format_context_window(tokens: int | None) -> str:
    """Render the context-window cell as a compact ``K`` / ``M`` token count."""
    if tokens is None:
        return "—"
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:g}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:g}K"
    return str(tokens)


def format_reasoning(choice: ModelChoice) -> str:
    """Render the reasoning-support cell (``"no"`` / ``"yes (default: <effort>)"``)."""
    if not choice.supported_efforts:
        return "no"
    if choice.default_effort is not None:
        return f"yes (default: {choice.default_effort})"
    return "yes"
