"""``ralph_afk.interactive.picker`` — startup picker orchestration (issue #24).

The one-time **model + reasoning-effort picker** (decisions D2a-D2d) runs on the
interactive path *before* the loop starts. This module is the orchestration seam
the CLI calls:

1. fetch the live model list with a short-lived **throwaway client** (connect ->
   ``list_models()`` -> stop), then
2. show the two-stage Textual picker (model, then — unless the model supports no
   reasoning effort — effort), and
3. return the chosen ``(model, effort)`` for the CLI to bake into the frozen
   :class:`RunConfig`.

**Fallback (the run always proceeds).** Any failure — offline, unauthed, a
``list_models()`` error, or an empty list — is caught, a warning is emitted, and
the env/default ``(model, effort)`` already resolved into the ``RunConfig`` (via
the static :data:`~ralph_afk.config.MODEL_REASONING_EFFORTS` matrix +
env/default in :mod:`ralph_afk.cli`) is returned unchanged. Quitting the picker
(``q`` / ``Ctrl+C``) likewise keeps the env/default, silently (a deliberate
choice, not an error).

**Import discipline.** Neither Textual nor the SDK is imported at module top: the
Textual picker app (:mod:`ralph_afk.interactive.picker_app`) is imported lazily
inside the default ``run_app``, and the SDK ``CopilotClient`` lazily inside the
default fetch. Together with the injectable ``fetch`` / ``run_app`` seams this
keeps :func:`resolve_run_model` — and crucially its fallback path — importable
and unit-testable without the optional ``[tui]`` extra and without a live
backend (mirrors how :mod:`ralph_afk.interactive.driver` keeps Textual out of
:mod:`ralph_afk.loop`).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Sequence

from ralph_afk.config import RunConfig
from ralph_afk.interactive.models import (
    ModelChoice,
    Selection,
    default_cursor_index,
    to_model_choices,
)

__all__ = ["resolve_run_model", "fetch_live_models"]

#: Builds the short-lived SDK client used only to list models. Injected so tests
#: can supply a fake async-context-manager client without spawning the CLI
#: server. The default constructs a bare :class:`copilot.CopilotClient` (the run
#: loop owns its own, separate, telemetry-configured client).
ClientFactory = Callable[[], Any]

#: Async ``() -> list[ModelInfo]`` model fetch, injected so the orchestrator's
#: fallback + success paths are unit-testable without a live backend.
ModelFetcher = Callable[[], Awaitable[Sequence[Any]]]

#: Async picker runner: ``(choices, *, cursor) -> Selection | None`` (``None`` =
#: the user quit). Injected so the orchestration is testable without Textual; the
#: default lazily runs the real :class:`~ralph_afk.interactive.picker_app.ModelPickerApp`.
PickerRunner = Callable[..., Awaitable["Selection | None"]]


async def fetch_live_models(*, client_factory: ClientFactory | None = None) -> Sequence[Any]:
    """List models via a throwaway client (connect -> list -> stop).

    The client is entered as an async context manager so ``start()`` and
    ``stop()`` bracket the single ``list_models()`` call — the run loop later
    builds and owns its *own* client, so this one is discarded immediately.
    """
    if client_factory is None:
        from copilot import CopilotClient

        client_factory = CopilotClient
    client = client_factory()
    async with client:
        return await client.list_models()


async def _run_picker_app(
    choices: Sequence[ModelChoice], *, cursor: int
) -> "Selection | None":
    """Default ``run_app``: run the real Textual picker (lazy Textual import)."""
    from ralph_afk.interactive.picker_app import ModelPickerApp

    app = ModelPickerApp(choices, cursor=cursor)
    return await app.run_async()


async def resolve_run_model(
    config: RunConfig,
    *,
    warn: Callable[[str], None],
    fetch: ModelFetcher = fetch_live_models,
    run_app: PickerRunner = _run_picker_app,
) -> tuple[str | None, str | None]:
    """Resolve the run's ``(model, effort)`` via the live picker, with fallback.

    Args:
        config: The env/default-resolved config; its ``model`` /
            ``reasoning_effort`` are the fallback (and the pre-highlight cursor
            target) when the live picker can't run or is quit.
        warn: Non-fatal warning sink (the kit's stderr ``ralph-afk: warning:``).
        fetch: Live model fetch (default: :func:`fetch_live_models`).
        run_app: Picker runner (default: the real Textual app).

    Returns:
        ``(model, effort)`` — the picker's selection, or the env/default on any
        failure / empty list / quit.
    """
    try:
        models = await fetch()
    except Exception as exc:  # offline / unauthed / list_models error
        warn(_fallback_message(config, f"{type(exc).__name__}: {exc}"))
        return config.model, config.reasoning_effort

    choices = to_model_choices(models)
    if not choices:
        warn(_fallback_message(config, "the live model list was empty"))
        return config.model, config.reasoning_effort

    cursor = default_cursor_index(choices, preferred=config.model)
    selection = await run_app(choices, cursor=cursor)
    if selection is None:
        # The user quit the picker without choosing -> keep env/default.
        return config.model, config.reasoning_effort
    return selection.model, selection.effort


def _fallback_message(config: RunConfig, reason: str) -> str:
    """Phrase the 'using env/default instead' warning for a picker fallback."""
    target = config.model or "the SDK default model"
    if config.reasoning_effort:
        target = f"{target} ({config.reasoning_effort})"
    return (
        f"could not load the live model list ({reason}); using {target}. "
        "Set MODEL / REASONING_EFFORT (or fix Copilot auth) to change it."
    )
